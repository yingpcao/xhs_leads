"""单 worker 控制（P0-10）：进程锁 + 租约 + UI 写入、worker 消费的控制请求。

`docs/DATA_MODEL.md` 第 2 节要求：判断"正在运行"必须同时核查 OS 进程锁与未过期租约。
但只靠"锁 + 租约"两个布尔值还不够安全，本模块按复核意见（2026-10-24 18:23）固定四条：

1. **不因租约过期就接管还活着的持有者**。原 worker 可能只是卡在长操作里没来得及心跳，
   接管它会启动第二个 worker 并发访问平台。锁指向的进程只要**能被确认存活**（或根本确认不了），
   默认一律拒绝启动。
2. **同 PID 不等于同一实例**。PID 会被复用，所有权看每次 `WorkerControl` 生成的实例令牌；
   `heartbeat/release` 只认令牌相同的持有者，被接管后旧实例既续不了租也删不掉新锁。
3. **建锁要原子且内容完整**。先写临时文件再 `os.link` 到目标（目标已存在即失败），
   不留"空锁文件已创建、内容还没写完"的窗口——那种半成品锁会被别人当损坏锁删掉。
4. **跨 run 也只有一个 worker**。除 per-run 锁外还要拿住 `root/worker-global.lock`，
   否则同一台机器上"每 run 各起一个 worker"会叠成并发访问。
5. **所有权变更由 OS 互斥保护，且互斥一直持到进程退出**（复核 2026-10-24 19:57）。
   每条锁配一个 `*.guard` 文件：进程活着就持有它的字节区间排他锁，进程被杀时由 OS 自动释放；
   这个文件**只创建、永不删除**，所以不存在"probe→unlink→create"里按旧快照删掉胜者锁的窗口。
   拿不到互斥就是"有 worker 正在跑"，`force=True` 也**不许**绕过——审计事件不实现单 worker 保障。
   锁文件本身（`worker.lock`）退化为"归属记录"：只有持互斥者能改写它，删除它也只发生在临界区内。

控制请求只由界面写入，worker 在安全点消费；`extend` 是追加额度，绝不重置总量。
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import ControlActionType, PauseReason, RunStatus, TerminalOutcome
from xhs_discovery.models.run import ControlRequest, RunTermination
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage.db import transaction
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository

DEFAULT_LEASE_SECONDS = 60
LOCK_FILENAME = "worker.lock"
GUARD_SUFFIX = ".guard"
_STILL_ACTIVE = 259
_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000

_LIVE_STATUSES = (RunStatus.queued.value, RunStatus.running.value, RunStatus.paused.value)

# 锁的四种可判定状态：absent/dead 才允许新 worker 进来；live 与 unknown 都不行
LOCK_ABSENT = "absent"
LOCK_LIVE = "live"
LOCK_DEAD = "dead"
LOCK_UNKNOWN = "unknown"
LOCK_INVALID = "invalid"
_UNPROVEN = (LOCK_UNKNOWN, LOCK_INVALID)


class WorkerAlreadyRunning(RuntimeError):
    """本机/本 run 已有 worker（或无法证明它已退出），拒绝启动第二个。"""


def _lock_region(fh) -> None:
    """非阻塞地拿到整文件的排他字节锁；失败即抛 `OSError`，由调用方判"别人正持着"。"""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_region(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class OsGuard:
    """持有到退出的 OS 互斥：文件只创建不删除，进程死了由系统释放，无需谁去清理它。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            _lock_region(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            _unlock_region(fh)
            fh.close()


def probe_pid(pid: int | None) -> str:
    """三态判定：`live`/`dead`/`unknown`。查不到、被拒绝一律 `unknown`，绝不当死进程。"""
    if pid is None or pid <= 0:
        return LOCK_INVALID
    if os.name == "nt":
        return _windows_pid_state(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LOCK_DEAD
    except PermissionError:
        return LOCK_LIVE
    except OSError:
        return LOCK_UNKNOWN
    return LOCK_LIVE


def _windows_pid_state(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED(5)：进程在，只是没权限看；其余才是真的没有这个进程
        return LOCK_UNKNOWN if kernel32.GetLastError() == 5 else LOCK_DEAD
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return LOCK_UNKNOWN
        return LOCK_LIVE if exit_code.value == _STILL_ACTIVE else LOCK_DEAD
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """只在能被确认时返回 True；`unknown` 不等于活着，调用方要用 `probe_pid` 区分。"""
    return probe_pid(pid) == LOCK_LIVE


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("租约时间戳必须带时区")
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lease_expired(row: dict, *, now: datetime) -> bool:
    expires = _parse(row.get("expires_at"))
    return expires is None or expires <= now


@dataclass(frozen=True)
class LockEvidence:
    """一把文件锁的完整证据：在不在、谁的、进程能不能确认。"""

    path: Path
    present: bool
    state: str
    pid: int | None
    token: str | None
    run_id: str | None

    @property
    def alive(self) -> bool:
        return self.state == LOCK_LIVE

    @property
    def unproven(self) -> bool:
        """确认不了死活，或者锁内容根本读不出来：都不得当成"没人占着"。"""
        return self.state in _UNPROVEN

    @property
    def clear(self) -> bool:
        return self.state in (LOCK_ABSENT, LOCK_DEAD)

    def evidence_text(self, label: str) -> str:
        if self.state == LOCK_ABSENT:
            return f"无{label}"
        if self.state == LOCK_INVALID:
            extra = f"（写着 pid={self.pid} run={self.run_id}）" if self.pid is not None else ""
            return f"{label}{extra} 内容不全或归属对不上：无效{label}"
        who = self.pid if self.pid is not None else "?"
        if self.state == LOCK_LIVE:
            return f"{label}被 pid={who} 持有且进程确认存活"
        if self.state == LOCK_DEAD:
            return f"遗留{label}（pid={who} 已不存在）"
        return f"{label}写着 pid={who}，但系统查不到它的死活"


@dataclass(frozen=True)
class WorkerStatus:
    run_id: str
    lock_present: bool
    lock_pid: int | None
    lock_state: str
    lock_alive: bool
    lock_token: str | None
    lease_present: bool
    lease_active: bool
    lease_pid: int | None
    lease_token: str | None
    running: bool
    occupied: bool
    mine: bool
    global_lock: LockEvidence
    detail: str

    @property
    def lock_clear(self) -> bool:
        """这把 run 锁可以安全替换：要么没有，要么主人已确认退出。"""
        return self.lock_state in (LOCK_ABSENT, LOCK_DEAD)


def lock_path(paths: RuntimePaths, run_id: str) -> Path:
    return paths.run_dir(run_id) / LOCK_FILENAME


def guard_of(path: Path) -> Path:
    """归属记录文件对应的 OS 互斥文件；它永不删除，所以清理它不构成竞争窗口。"""
    return path.with_name(path.name + GUARD_SUFFIX)


def global_lock_path(paths: RuntimePaths) -> Path:
    return paths.global_lock_path


def read_lock(path: Path) -> dict | None:
    """锁文件只是提示，解析失败就当作无效锁，不据此判定"有人在跑"。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if str(value or "").isdigit() else None


def probe_lock(path: Path, *, expect_run_id: str | None = None) -> LockEvidence:
    """读锁文件并判定状态；内容残缺/归属不符都归为 `invalid`，不是 `absent`。"""
    if not path.exists():
        return LockEvidence(path, False, LOCK_ABSENT, None, None, None)
    payload = read_lock(path)
    if payload is None:
        return LockEvidence(path, True, LOCK_INVALID, None, None, None)
    pid = _int_or_none(payload.get("pid"))
    token = payload.get("token")
    owner_run = payload.get("run_id")
    if pid is None or not isinstance(token, str) or not token:
        return LockEvidence(path, True, LOCK_INVALID, pid, None,
                            owner_run if isinstance(owner_run, str) else None)
    if expect_run_id is not None and owner_run != expect_run_id:
        return LockEvidence(path, True, LOCK_INVALID, pid, token, owner_run)
    return LockEvidence(path, True, probe_pid(pid), pid, token, owner_run)  # type: ignore[arg-type]


class WorkerControl:
    """一个 run 一个 worker 的门面：全局锁、run 锁、租约、心跳、接管与状态判定。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: RuntimePaths,
        run_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        pid: int | None = None,
        owner_token: str | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds 至少为 1")
        self.conn = conn
        self.paths = paths
        self.run_id = run_id
        self.lease_seconds = lease_seconds
        self.pid = os.getpid() if pid is None else pid
        # 实例令牌：同一 pid 的第二个 WorkerControl 不是同一个持有者
        self.owner_token = owner_token or secrets.token_hex(16)
        self.repo = Repository(conn, migrate=False)
        # OS 互斥：拿到后一直持到 release/进程退出，所有权变更全在临界区里做
        self._global_guard = OsGuard(guard_of(global_lock_path(paths)))
        self._run_guard = OsGuard(guard_of(lock_path(paths, run_id)))

    # -------------------------------------------------- 判定

    def _lease(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM worker_leases WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def status(self, *, now: datetime | None = None) -> WorkerStatus:
        now = now or utc_now()
        lock = probe_lock(lock_path(self.paths, self.run_id), expect_run_id=self.run_id)
        global_lock = probe_lock(global_lock_path(self.paths))
        lease = self._lease()
        has_lease = lease is not None and lease.get("released_at") is None
        active = bool(has_lease and not lease_expired(lease or {}, now=now))
        lease_pid = _int_or_none(lease.get("owner_pid")) if lease else None
        lease_token = lease.get("owner_token") if lease else None
        # 租约未过期、且租约主人确认还活着 → 即使锁文件丢了也不能再起第二个
        lease_owner_alive = active and probe_pid(lease_pid) == LOCK_LIVE
        running = lock.alive and active
        mine = lock.token == self.owner_token or (lease_token == self.owner_token and bool(has_lease))
        occupied = (not mine) and (
            not lock.clear
            or lease_owner_alive
            or (global_lock.present and not global_lock.clear and global_lock.token != self.owner_token)
        )
        parts = [
            f"进程锁：{lock.evidence_text('锁')}",
            f"租约：{'租约未过期' if active else ('租约已过期' if has_lease else '无租约')}"
            + (f"（pid={lease_pid} 确认存活）" if lease_owner_alive else ""),
            f"全局锁：{global_lock.evidence_text('全局锁')}",
        ]
        verdict = "正在运行" if running else ("疑似有 worker 未被排除" if occupied else "未在运行")
        detail = " + ".join(parts) + f"：判定为{verdict}"
        return WorkerStatus(
            run_id=self.run_id,
            lock_present=lock.present,
            lock_pid=lock.pid,
            lock_state=lock.state,
            lock_alive=lock.alive,
            lock_token=lock.token,
            lease_present=lease is not None,
            lease_active=active,
            lease_pid=lease_pid,
            lease_token=lease_token,
            running=running,
            occupied=occupied,
            mine=mine,
            global_lock=global_lock,
            detail=detail,
        )

    # -------------------------------------------------- 获取与释放

    def acquire(
        self, *, takeover: bool = True, force: bool = False, now: datetime | None = None
    ) -> WorkerStatus:
        """拿全局锁 + run 锁并落租约；无法确认上一任已退出时拒绝启动第二个 worker。

        - `takeover=False`：连遗留锁都不许清（用于"只接受干净启动"的场景）；
        - `force=True`：仅绕过"进程锁记录里的 pid 查不到死活"这类**推断**，并留下 `forced_takeover`
          事件；它**绕不过 OS 互斥**——只要还有实例持着 guard，第二个 worker 一定起不来。
        """
        now = now or utc_now()
        run = self.repo.require_run(self.run_id)
        if run["status"] not in _LIVE_STATUSES:
            raise ValueError(f"run 状态 {run['status']} 不需要 worker，拒绝启动")
        current = self.status(now=now)
        if current.occupied and not force:
            raise WorkerAlreadyRunning(f"run {self.run_id} 的 worker 还没被排除：{current.detail}")
        if current.lock_present and not current.mine and not takeover:
            raise WorkerAlreadyRunning(f"存在遗留锁且未允许接管：{current.detail}")

        try:
            self._claim_lock(
                global_lock_path(self.paths),
                guard=self._global_guard,
                payload={"run_id": self.run_id, "pid": self.pid, "token": self.owner_token,
                         "acquired_at": _iso(now)},
                label="全局锁",
                takeover=takeover,
                force=force,
                now=now,
            )
            claimed = self._claim_lock(
                lock_path(self.paths, self.run_id),
                guard=self._run_guard,
                payload={"run_id": self.run_id, "pid": self.pid, "token": self.owner_token,
                         "acquired_at": _iso(now)},
                label="进程锁",
                takeover=takeover,
                force=force,
                now=now,
                run_id=self.run_id,
            )
        except BaseException:
            # 半拿状态不能留：要么两把都占住，要么一把都不占（记录也撤掉，别给下一个 run 留假占用）
            for path in (lock_path(self.paths, self.run_id), global_lock_path(self.paths)):
                self._drop_if_mine(path)
            self._release_guards()
            raise
        if claimed and not current.mine and not current.lock_clear:
            self.repo.record_event(
                self.run_id,
                "forced_takeover",
                detail={"previous_pid": current.lock_pid, "previous_token": current.lock_token,
                        "previous_state": current.lock_state, "new_pid": self.pid,
                        "new_token": self.owner_token,
                        "guard": str(guard_of(lock_path(self.paths, self.run_id)))},
                at=now,
            )
        elif claimed and not current.mine:
            self.repo.record_event(
                self.run_id,
                "stale_lock_removed",
                detail={"locked_pid": current.lock_pid, "lock_state": current.lock_state,
                        "lease_active": current.lease_active,
                        "lock_path": str(lock_path(self.paths, self.run_id))},
                at=now,
            )
        self._write_lease(now, previous=current.lease_present and not current.mine)
        return self.status(now=now)

    def _release_guards(self) -> None:
        self._run_guard.release()
        self._global_guard.release()

    def _claim_lock(
        self, path: Path, *, guard: OsGuard, payload: dict, label: str, takeover: bool,
        force: bool, now: datetime, run_id: str | None = None,
    ) -> bool:
        """先在 OS 互斥里排队，再改归属记录；返回是否替换掉了别人的记录。

        旧写法是 probe→unlink→create：两个进程都 probe 到同一把 dead 锁时，A 建好自己的新锁后，
        B 仍会按旧快照 unlink 掉 A 的活锁再建 B 的，两边都返回成功。改为"持互斥者独占临界区"后，
        B 拿不到互斥就直接失败，永不删除 A 的文件；互斥文件本身从不删除，所以没有清理竞争。
        """
        existing = probe_lock(path, expect_run_id=run_id)
        if existing.state != LOCK_ABSENT and existing.token != self.owner_token:
            if not existing.clear and not force:
                raise WorkerAlreadyRunning(
                    f"{label}仍被 pid={existing.pid} 占着：{existing.evidence_text(label)}"
                )
            if not takeover:
                raise WorkerAlreadyRunning(
                    f"{label}存在且未允许清理：{existing.evidence_text(label)}"
                )
        if not guard.try_acquire():
            raise WorkerAlreadyRunning(
                f"{label}的 OS 互斥（{guard_of(path).name}）仍被另一个实例持有：worker 还没被排除，"
                "force 也不能绕过独占"
            )
        replaced = existing.present and existing.token != self.owner_token
        if existing.present:
            path.unlink(missing_ok=True)
        try:
            _create_exclusive(path, payload, label=label)
        except BaseException:
            guard.release()
            raise
        return replaced

    def _write_lease(self, now: datetime, *, previous: bool) -> None:
        expires = _iso(now + timedelta(seconds=self.lease_seconds))
        stamp = _iso(now)
        replaced: dict | None = None
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM worker_leases WHERE run_id = ?", (self.run_id,)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO worker_leases(run_id, owner_pid, owner_token, acquired_at,"
                    " heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.run_id, self.pid, self.owner_token, stamp, stamp, expires),
                )
            else:
                if previous and str(existing["owner_token"]) != self.owner_token:
                    replaced = dict(existing)
                self.conn.execute(
                    "UPDATE worker_leases SET owner_pid = ?, owner_token = ?, acquired_at = ?,"
                    " heartbeat_at = ?, expires_at = ?, released_at = NULL WHERE run_id = ?",
                    (self.pid, self.owner_token, stamp, stamp, expires, self.run_id),
                )
        if replaced is not None:
            self.repo.record_event(
                self.run_id,
                "lease_taken_over",
                detail={
                    "previous_pid": int(replaced["owner_pid"]),
                    "previous_token": replaced["owner_token"],
                    "previous_expires_at": replaced["expires_at"],
                    "new_pid": self.pid,
                    "new_token": self.owner_token,
                },
                at=now,
            )

    def heartbeat(self, *, now: datetime | None = None) -> None:
        """续租；租约已被别人接走时拒绝写回，避免僵尸 worker 覆盖新 owner。"""
        now = now or utc_now()
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE worker_leases SET heartbeat_at = ?, expires_at = ?"
                " WHERE run_id = ? AND owner_token = ? AND released_at IS NULL",
                (_iso(now), _iso(now + timedelta(seconds=self.lease_seconds)),
                 self.run_id, self.owner_token),
            )
            if cursor.rowcount != 1:
                raise WorkerAlreadyRunning(
                    f"run {self.run_id} 的租约不属于本实例（token={self.owner_token[:8]}…），停止续租"
                )
        if not self._run_guard.held:
            raise WorkerAlreadyRunning(
                f"run {self.run_id} 的进程锁互斥已不在本实例手上（token={self.owner_token[:8]}…），"
                "外部访问前必须停止"
            )
        for path, label, expect in ((global_lock_path(self.paths), "全局锁", None),
                                    (lock_path(self.paths, self.run_id), "进程锁", self.run_id)):
            self._renew_lock(path, now, label=label, expect_run_id=expect)

    def _renew_lock(self, path: Path, now: datetime, *, label: str, expect_run_id: str | None) -> None:
        """只在自己仍持有该锁时刷新内容；不能把别人的锁改写或删掉。"""
        evidence = probe_lock(path, expect_run_id=expect_run_id)
        if evidence.state == LOCK_ABSENT or evidence.token != self.owner_token:
            return
        _create_exclusive(
            path,
            {"run_id": self.run_id, "pid": self.pid, "token": self.owner_token,
             "heartbeat_at": _iso(now)},
            label=label,
            replace=True,
        )

    def release(self, *, now: datetime | None = None) -> None:
        """正常收尾：按令牌放租约，仍在互斥里删自己的归属记录，最后交还互斥。"""
        now = now or utc_now()
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE worker_leases SET released_at = ?, heartbeat_at = ?"
                " WHERE run_id = ? AND owner_token = ? AND released_at IS NULL",
                (_iso(now), _iso(now), self.run_id, self.owner_token),
            )
        # 删记录必须在还互斥之前：接盘者拿到互斥时看到的要么是旧记录要么没有，绝不会是"删一半"
        for path in (lock_path(self.paths, self.run_id), global_lock_path(self.paths)):
            self._drop_if_mine(path)
        self._release_guards()

    def _drop_if_mine(self, path: Path) -> None:
        evidence = probe_lock(path)
        if evidence.present and evidence.token == self.owner_token:
            path.unlink(missing_ok=True)

    @contextmanager
    def held(self, *, takeover: bool = True, force: bool = False, now: datetime | None = None):
        """短临界区用同一个时间戳拿锁和放锁，异常也一定放锁。"""
        self.acquire(takeover=takeover, force=force, now=now)
        try:
            yield self
        finally:
            self.release(now=now)


def _create_exclusive(path: Path, payload: dict, *, label: str, replace: bool = False) -> None:
    """写锁文件不留半成品窗口：临时文件写全 → `os.link` 到目标（已存在即失败）。

    `os.link` 不可用的文件系统退回 `O_CREAT|O_EXCL`，那条路径存在极短的写入窗口，
    但读侧把读不出内容的锁判为 `invalid`（拒绝接管），不会把半截锁当死人留下的干净锁。
    """
    body = json.dumps(payload, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{payload['token']}.tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        if replace:
            os.replace(str(tmp), str(path))
        else:
            try:
                os.link(str(tmp), str(path))
            except FileExistsError:
                raise
            except (OSError, NotImplementedError):
                # 文件系统不支持硬链接：退回 O_CREAT|O_EXCL，仍有极短的写入窗口
                handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise WorkerAlreadyRunning(
            f"{label}被并发抢先创建（race 窗口）：{path}，本次不启动第二个 worker"
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- 控制请求


def request_control(conn: sqlite3.Connection, request: ControlRequest) -> int:
    """界面侧写入控制意图：只落库，不直接改 run 状态。"""
    repo = Repository(conn, migrate=False)
    repo.require_run(request.run_id)
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO control_requests(run_id, action, requested_at, suggest_budget_delta,"
            " search_budget_delta, note) VALUES (?, ?, ?, ?, ?, ?)",
            (
                request.run_id,
                request.action.value,
                _iso(request.requested_at),
                request.suggest_budget_delta,
                request.search_budget_delta,
                request.note,
            ),
        )
    return int(cursor.lastrowid)


def pending_controls(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM control_requests WHERE run_id = ? AND applied_at IS NULL ORDER BY requested_at, id",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _extend_deltas(row: dict) -> dict[str, int]:
    deltas: dict[str, int] = {}
    if row.get("suggest_budget_delta"):
        deltas["suggest_attempts"] = int(row["suggest_budget_delta"])
    if row.get("search_budget_delta"):
        deltas["search_attempts"] = int(row["search_budget_delta"])
    if not deltas:
        raise ValueError(f"extend 请求 {row['id']} 没有正数增量，拒绝当作空操作应用")
    return deltas


def consume_control(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    ledger: Ledger | None = None,
    at: datetime | None = None,
) -> dict | None:
    """worker 在安全点消费最早一条未应用请求，返回应用结果（含 applied 动作与说明）。"""
    queue = pending_controls(conn, run_id)
    if not queue:
        return None
    row = queue[0]
    now = at or utc_now()
    repo = Repository(conn, migrate=False)
    action = ControlActionType(str(row["action"]))
    result: dict = {"request_id": int(row["id"]), "action": action.value, "applied_at": _iso(now)}
    if action is ControlActionType.pause:
        repo.set_run_status(
            run_id,
            RunTermination(status=RunStatus.paused, pause_reason=PauseReason.user, note=row.get("note")),
            at=now,
        )
    elif action is ControlActionType.resume:
        if str(repo.require_run(run_id)["status"]) != RunStatus.running.value:
            repo.set_run_status(run_id, RunTermination(status=RunStatus.running), at=now)
        else:
            # A dead worker can leave its run marked running. The new owner
            # still consumes the resume request, without a running->running transition.
            result["already_running"] = True
        repo.clear_pause_reason(run_id)
    elif action is ControlActionType.extend:
        if ledger is None:
            raise ValueError("extend 请求需要 Ledger 才能追加额度，缺少时不静默丢弃")
        if ledger.run_id != run_id:
            raise ValueError("Ledger 属于另一个 run，拒绝跨 run 追加预算")
        revision = ledger.extend(_extend_deltas(row), at=now, note=row.get("note"))
        result["budget_revision"] = revision
    else:
        repo.set_run_status(
            run_id,
            RunTermination(
                status=RunStatus.completed,
                outcome=TerminalOutcome.cancelled,
                note=row.get("note") or "用户取消",
            ),
            at=now,
        )
    with transaction(conn):
        conn.execute(
            "UPDATE control_requests SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
            (_iso(now), int(row["id"])),
        )
    result["status_after"] = str(repo.require_run(run_id)["status"])
    return result


def consume_all_controls(
    conn: sqlite3.Connection, run_id: str, *, ledger: Ledger | None = None, at: datetime | None = None
) -> list[dict]:
    applied: list[dict] = []
    while True:
        result = consume_control(conn, run_id, ledger=ledger, at=at)
        if result is None:
            return applied
        applied.append(result)


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "LOCK_FILENAME",
    "ControlRequest",
    "LockEvidence",
    "WorkerAlreadyRunning",
    "WorkerControl",
    "WorkerStatus",
    "consume_all_controls",
    "consume_control",
    "global_lock_path",
    "lease_expired",
    "lock_path",
    "pending_controls",
    "pid_alive",
    "probe_lock",
    "probe_pid",
    "read_lock",
    "request_control",
]
