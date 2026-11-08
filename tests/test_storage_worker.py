"""P0-10：单 worker 锁、租约与运行控制。

判定"正在运行"要同时看到进程锁与未过期租约；反过来，**接管**的门槛比判定更严：
只要锁指向的进程还能确认存活、或者根本确认不了死活，就不许起第二个 worker。
所有权看实例令牌而不是 pid（pid 会被复用）。跨 run 也只允许一个 worker。
下面几个用例是真实多进程跑的，不是在同进程里假装。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import xhs_discovery

from conftest import build_run_identity
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import ControlActionType, PauseReason, RunStatus, TerminalOutcome
from xhs_discovery.models.run import ControlRequest, RunTermination
from xhs_discovery.storage import worker as worker_mod
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.worker import WorkerAlreadyRunning, WorkerControl, request_control

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
SRC_DIR = Path(xhs_discovery.__file__).resolve().parents[1]
OTHER_TOKEN = "0" * 32


def sleeper(seconds: int = 30):
    """真的起一个进程当"上一个 worker"，这样 pid 存活检查不是自证。"""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def stop(proc) -> int:
    proc.terminate()
    proc.wait(timeout=10)
    return proc.pid


@pytest.fixture
def live_pid():
    proc = sleeper()
    try:
        yield proc.pid
    finally:
        stop(proc)


@pytest.fixture
def dead_pid():
    proc = sleeper(1)
    pid = stop(proc)
    return pid


def lease_row(conn, run_id, pid, *, expires_at, released_at=None, token=OTHER_TOKEN):
    with conn:
        conn.execute(
            "INSERT INTO worker_leases(run_id, owner_pid, owner_token, acquired_at, heartbeat_at,"
            " expires_at, released_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, pid, token, _iso(NOW), _iso(NOW), _iso(expires_at),
             _iso(released_at) if released_at else None),
        )


def _iso(value: datetime) -> str:
    return value.isoformat()


def write_lock(storage, pid: int, *, token: str = OTHER_TOKEN, run_id: str | None = None,
               raw: str | None = None) -> Path:
    path = worker_mod.lock_path(storage.paths, storage.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = raw if raw is not None else json.dumps(
        {"run_id": run_id or storage.run_id, "pid": pid, "token": token}, ensure_ascii=False
    )
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- 真实子进程


def holder_script(db_path: Path, root: Path, run_id: str, *, lease_seconds: int = 120) -> str:
    """子进程版 worker：拿锁→报出身份→等 stdin 一行→放锁退出。"""
    return f"""
import sys, json
sys.path.insert(0, {str(SRC_DIR)!r})
from pathlib import Path
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db
from xhs_discovery.storage.worker import WorkerControl
conn = db.connect(Path({str(db_path)!r}))
paths = RuntimePaths(Path({str(root)!r}))
control = WorkerControl(conn, paths, {run_id!r}, lease_seconds={lease_seconds})
try:
    status = control.acquire()
except Exception as exc:  # noqa: BLE001 - 子进程只负责如实报告
    print(json.dumps({{"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}}), flush=True)
    sys.exit(7)
print(json.dumps({{"ok": True, "pid": control.pid, "token": control.owner_token,
                   "running": status.running}}), flush=True)
sys.stdin.read()
control.release()
conn.close()
"""


def start_holder(storage, run_id: str = "run-a"):
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script(storage.paths.db_path, storage.paths.root, run_id)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    line = proc.stdout.readline()
    if not line:
        proc.wait(timeout=15)
        raise AssertionError(f"子进程没能报出身份，exit={proc.returncode}")
    return proc, json.loads(line)


def let_go(proc) -> int:
    if proc.stdin is not None:
        proc.stdin.close()
    return proc.wait(timeout=20)


# ------------------------------------------------------------------ 进程存活判定


def test_pid_alive_uses_the_os_not_the_lock_file(live_pid, dead_pid):
    assert worker_mod.probe_pid(live_pid) == worker_mod.LOCK_LIVE
    assert worker_mod.probe_pid(dead_pid) == worker_mod.LOCK_DEAD
    assert worker_mod.pid_alive(os.getpid()) is True
    assert worker_mod.pid_alive(0) is False
    assert worker_mod.pid_alive(-5) is False
    assert worker_mod.probe_pid(None) == worker_mod.LOCK_INVALID


def test_read_lock_tolerates_garbage(storage):
    path = write_lock(storage, os.getpid(), raw="半个 JSON")
    assert worker_mod.read_lock(path) is None
    control = WorkerControl(storage.conn, storage.paths, storage.run_id)
    status = control.status(now=NOW)
    assert status.lock_present is True and status.lock_alive is False
    assert status.running is False
    assert "无效锁" in status.detail
    # 读不出内容的锁不能当"没人占着"：拒绝接管，除非人工 force 确认
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=NOW)
    assert control.acquire(now=NOW, force=True).running is True


# ------------------------------------------------------------------ 获取与判定


def test_acquire_writes_both_the_lock_and_an_unexpired_lease(storage):
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=60)
    status = control.acquire(now=NOW)
    assert status.running is True and status.mine is True and status.occupied is False
    payload = json.loads(worker_mod.lock_path(storage.paths, storage.run_id).read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["token"] == control.owner_token
    assert worker_mod.global_lock_path(storage.paths).exists(), "跨 run 的全局锁也要一起拿住"
    row = storage.conn.execute("SELECT * FROM worker_leases WHERE run_id = ?",
                              (storage.run_id,)).fetchone()
    assert row["owner_pid"] == os.getpid()
    assert row["owner_token"] == control.owner_token
    assert row["expires_at"] == _iso(NOW + timedelta(seconds=60))
    control.release(now=NOW)
    assert worker_mod.global_lock_path(storage.paths).exists() is False


def test_a_second_worker_refuses_to_start_while_the_owner_is_alive(storage, live_pid):
    owner = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=120, pid=live_pid)
    owner.acquire(now=NOW)
    intruder = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = intruder.status(now=NOW)
    assert status.running is True and status.lock_pid == live_pid and status.mine is False
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        intruder.acquire(now=NOW)
    assert worker_mod.read_lock(worker_mod.lock_path(storage.paths, storage.run_id))["pid"] == live_pid
    # 复核（19:57）：force 只是审计手段，不是并发许可——持有者仍跑着时必须进不来
    with pytest.raises(WorkerAlreadyRunning, match="OS 互斥"):
        intruder.acquire(now=NOW, force=True)
    assert storage.repo.events(storage.run_id, kind="forced_takeover") == []
    record = worker_mod.read_lock(worker_mod.lock_path(storage.paths, storage.run_id))
    assert record["token"] == owner.owner_token, "force 失败也不许改写别人的归属记录"
    owner.release(now=NOW)
    assert intruder.acquire(now=NOW).running is True
    intruder.release(now=NOW)


def test_force_may_only_take_a_record_that_no_guard_protects(storage, live_pid):
    """`force=True` 的合法场景：记录写着活 pid，但互斥已不在任何实例手上（旧版本/外部写的记录）。"""
    write_lock(storage, live_pid, token=OTHER_TOKEN)
    lease_row(storage.conn, storage.run_id, live_pid, token=OTHER_TOKEN,
              expires_at=NOW + timedelta(seconds=120))
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=NOW)
    acquired = control.acquire(now=NOW, force=True)
    assert acquired.running is True and acquired.mine is True
    kinds = [event["kind"] for event in storage.repo.events(storage.run_id)]
    assert "forced_takeover" in kinds and "lease_taken_over" in kinds
    guard = worker_mod.OsGuard(worker_mod.guard_of(worker_mod.lock_path(storage.paths, storage.run_id)))
    assert guard.try_acquire() is False, "接管者必须正持着 OS 互斥"
    control.release(now=NOW)
    assert guard.try_acquire() is True
    guard.release()



def test_a_stale_lock_file_is_not_evidence_of_a_running_worker(storage, dead_pid):
    write_lock(storage, dead_pid)
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = control.status(now=NOW)
    assert status.lock_present and not status.lock_alive and not status.running
    assert status.lock_state == worker_mod.LOCK_DEAD and status.occupied is False
    assert "遗留锁" in status.detail
    with pytest.raises(WorkerAlreadyRunning, match="未允许接管"):
        control.acquire(now=NOW, takeover=False)
    acquired = control.acquire(now=NOW)
    assert acquired.running and acquired.mine
    assert control.repo.events(storage.run_id, kind="stale_lock_removed")


def test_a_live_pid_with_an_expired_lease_is_not_open_for_takeover(storage, live_pid):
    """租约过期只说明心跳停了，不代表进程退了：长操作期间照样不许起第二个 worker。"""
    write_lock(storage, live_pid)
    lease_row(storage.conn, storage.run_id, live_pid, expires_at=NOW - timedelta(seconds=1))
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = control.status(now=NOW)
    assert status.lock_alive is True and status.lease_active is False and status.running is False
    assert status.occupied is True, "锁主进程还活着，不能被判成可接管"
    assert "租约已过期" in status.detail
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=NOW)
    acquired = control.acquire(now=NOW, force=True)
    assert acquired.running is True
    event = control.repo.events(storage.run_id, kind="lease_taken_over")
    assert json.loads(event[0]["detail_json"])["previous_pid"] == live_pid


def test_an_unexpired_lease_with_a_live_owner_blocks_even_without_a_lock(storage, live_pid):
    """锁文件丢了但租约没过期、租约主人还活着：仍然不许第二个 worker 进来。"""
    lease_row(storage.conn, storage.run_id, live_pid, expires_at=NOW + timedelta(seconds=60))
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = control.status(now=NOW)
    assert status.lease_active is True and status.lock_present is False and status.running is False
    assert status.occupied is True
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=NOW)
    control.acquire(now=NOW, force=True)
    assert control.status(now=NOW).running is True


def test_a_released_lease_does_not_block_a_new_worker(storage, live_pid):
    lease_row(storage.conn, storage.run_id, live_pid, expires_at=NOW + timedelta(seconds=60),
              released_at=NOW)
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    assert control.status(now=NOW).running is False
    assert control.status(now=NOW).occupied is False
    control.acquire(now=NOW)


def test_a_lock_written_for_another_run_is_invalid_not_free(storage):
    storage.repo.create_run(build_run_identity("run-b"), config_json='{"budget": {}}')
    write_lock(storage, os.getpid(), run_id="run-b")
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = control.status(now=NOW)
    assert status.lock_state == worker_mod.LOCK_INVALID and status.occupied is True
    assert "无效锁" in status.detail


def test_unknown_pid_liveness_is_never_treated_as_dead(storage, monkeypatch):
    write_lock(storage, 999999)
    monkeypatch.setattr(worker_mod, "probe_pid",
                        lambda pid: worker_mod.LOCK_UNKNOWN if pid == 999999 else worker_mod.LOCK_DEAD)
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = control.status(now=NOW)
    assert status.lock_state == worker_mod.LOCK_UNKNOWN and status.running is False
    assert status.occupied is True, "确认不了死活不等于进程已退"
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=NOW)


def test_acquire_refuses_a_finished_run(storage):
    start(storage)
    storage.repo.set_run_status(
        storage.run_id, RunTermination(status=RunStatus.completed,
                                       outcome=TerminalOutcome.queue_exhausted)
    )
    control = WorkerControl(storage.conn, storage.paths, storage.run_id)
    with pytest.raises(ValueError, match="不需要 worker"):
        control.acquire(now=NOW)


# ------------------------------------------------------------------ 全局单 worker


def test_the_global_lock_blocks_a_second_run_on_the_same_machine(storage):
    """跨 run 也只有一个 worker：否则每个 run 各起一个，照样并发打平台。"""
    storage.repo.create_run(build_run_identity("run-b"), config_json='{"budget": {}}')
    storage.repo.set_run_status("run-b", RunTermination(status=RunStatus.running), at=NOW)
    first = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=60)
    first.acquire(now=NOW)
    second = WorkerControl(storage.conn, storage.paths, "run-b", lease_seconds=60)
    assert second.status(now=NOW).occupied is True
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        second.acquire(now=NOW)
    first.release(now=NOW)
    assert second.acquire(now=NOW).running is True
    second.release(now=NOW)


# ------------------------------------------------------------------ 心跳与释放


def test_heartbeat_extends_the_lease_and_refuses_after_takeover(storage):
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    control.acquire(now=NOW)
    control.heartbeat(now=NOW + timedelta(seconds=20))
    assert control.status(now=NOW + timedelta(seconds=40)).running is True
    assert control.status(now=NOW + timedelta(seconds=60)).running is False
    with storage.conn:
        storage.conn.execute("UPDATE worker_leases SET owner_token = ? WHERE run_id = ?",
                             (OTHER_TOKEN, storage.run_id))
    with pytest.raises(WorkerAlreadyRunning, match="不属于本实例"):
        control.heartbeat(now=NOW + timedelta(seconds=25))


def test_takeover_leaves_the_old_instance_unable_to_touch_lock_or_lease(storage, live_pid):
    """接管之后旧实例就是僵尸：续租要报错，放锁也不得删掉新持有者的锁或放掉新租约。

    旧实例这里以"只留下记录、互斥已不在它手上"的形态出现——真实互斥还持着时根本接管不了
    （见 `test_a_second_worker_refuses_to_start_while_the_owner_is_alive`）。
    """
    write_lock(storage, live_pid, token=OTHER_TOKEN)
    lease_row(storage.conn, storage.run_id, live_pid, token=OTHER_TOKEN,
              expires_at=NOW + timedelta(seconds=120))
    new = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    new.acquire(now=NOW, force=True)
    old = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=120,
                        pid=live_pid, owner_token=OTHER_TOKEN)
    with pytest.raises(WorkerAlreadyRunning, match="不属于本实例"):
        old.heartbeat(now=NOW + timedelta(seconds=10))
    old.release(now=NOW + timedelta(seconds=10))
    path = worker_mod.lock_path(storage.paths, storage.run_id)
    assert path.exists(), "旧实例不得删掉新锁"
    assert worker_mod.read_lock(path)["token"] == new.owner_token
    row = storage.conn.execute("SELECT owner_token, released_at FROM worker_leases WHERE run_id = ?",
                              (storage.run_id,)).fetchone()
    assert row["owner_token"] == new.owner_token and row["released_at"] is None
    assert new.status(now=NOW + timedelta(seconds=10)).running is True
    new.release(now=NOW + timedelta(seconds=11))



def test_release_clears_lock_and_lease_and_is_idempotent(storage):
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    control.acquire(now=NOW)
    control.release(now=NOW + timedelta(seconds=5))
    row = storage.conn.execute("SELECT released_at FROM worker_leases WHERE run_id = ?",
                              (storage.run_id,)).fetchone()
    assert row["released_at"] == _iso(NOW + timedelta(seconds=5))
    assert worker_mod.lock_path(storage.paths, storage.run_id).exists() is False
    # 互斥文件永不删除：它由 OS 在进程退出时释放，删掉它反而会重新引入清理竞争
    assert worker_mod.guard_of(worker_mod.lock_path(storage.paths, storage.run_id)).exists()
    assert worker_mod.guard_of(worker_mod.global_lock_path(storage.paths)).exists()
    control.release(now=NOW + timedelta(seconds=6))
    assert control.status(now=NOW + timedelta(seconds=6)).running is False


def test_held_releases_even_when_the_body_raises(storage):
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    with pytest.raises(RuntimeError, match="崩了"):
        with control.held(now=NOW):
            assert control.status(now=NOW).running is True
            raise RuntimeError("崩了")
    assert worker_mod.lock_path(storage.paths, storage.run_id).exists() is False
    assert worker_mod.global_lock_path(storage.paths).exists() is False
    assert storage.conn.execute("SELECT released_at FROM worker_leases").fetchone()["released_at"] is not None


def test_lock_path_is_confined_to_the_run_directory(storage):
    path = worker_mod.lock_path(storage.paths, storage.run_id)
    assert path.parent == storage.paths.run_dir(storage.run_id)
    assert path.name == worker_mod.LOCK_FILENAME
    assert worker_mod.global_lock_path(storage.paths) == storage.paths.root / "worker-global.lock"
    with pytest.raises(ValueError, match="lease_seconds"):
        WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=0)


def test_the_same_pid_is_not_the_same_worker_instance(storage):
    """PID 会被复用：所有权只认实例令牌，同 pid 的第二个实例不能冒充持有者。"""
    first = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    second = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    first.acquire(now=NOW)
    assert second.owner_token != first.owner_token
    assert second.status(now=NOW).mine is False
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        second.acquire(now=NOW)
    assert storage.repo.events(storage.run_id, kind="stale_lock_removed") == []
    assert storage.repo.events(storage.run_id, kind="lease_taken_over") == []
    rows = storage.conn.execute("SELECT COUNT(*) AS n FROM worker_leases").fetchone()
    assert rows["n"] == 1
    # 同一个实例重入才是幂等的
    assert first.acquire(now=NOW).mine is True
    first.release(now=NOW + timedelta(seconds=1))


# --------------------------------------------------------------------------- 多进程证据


def test_a_real_second_process_cannot_take_a_live_lock(storage):
    proc, holder = start_holder(storage)
    assert holder["ok"] and holder["running"] is True
    assert holder["pid"] != os.getpid()
    real = utc_now()  # 子进程用真实时钟落租约，判定也得用真实时钟
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    status = control.status(now=real)
    assert status.lock_alive is True and status.occupied is True and status.mine is False
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=real)
    assert let_go(proc) == 0
    assert control.status(now=real).lock_present is False
    assert control.acquire(now=real).running is True
    control.release(now=real)


def test_two_concurrent_processes_contend_and_exactly_one_wins(storage, tmp_path):
    """真实竞争：三个子进程同一时刻抢锁，只能有一个拿到。"""
    go = tmp_path / "GO"
    scripts = tmp_path / "race.py"
    scripts.write_text(
        f"""
import sys, json, time
sys.path.insert(0, {str(SRC_DIR)!r})
from pathlib import Path
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db
from xhs_discovery.storage.worker import WorkerControl
db_path, root, run_id, go = {str(storage.paths.db_path)!r}, {str(storage.paths.root)!r}, \\
    {storage.run_id!r}, {str(go)!r}
while not Path(go).exists():
    time.sleep(0.001)
conn = db.connect(db_path)
control = WorkerControl(conn, RuntimePaths(Path(root)), run_id, lease_seconds=120)
try:
    status = control.acquire()
    print(json.dumps({{"ok": True, "token": control.owner_token}}), flush=True)
    sys.stdin.read()
    control.release()
except Exception as exc:  # noqa: BLE001
    print(json.dumps({{"ok": False, "error": type(exc).__name__}}), flush=True)
""",
        encoding="utf-8",
    )
    procs = [
        subprocess.Popen([sys.executable, str(scripts)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
        for _ in range(3)
    ]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not go.exists():
        go.touch()
        time.sleep(0.001)
    results = []
    for proc in procs:
        line = proc.stdout.readline()
        results.append(json.loads(line) if line else {"ok": False, "error": "no-output"})
    winners = [item for item in results if item["ok"]]
    assert len(winners) == 1, f"只能有一个持有者：{results}"
    assert all(item["error"] == "WorkerAlreadyRunning" for item in results if not item["ok"])
    lock = json.loads(worker_mod.lock_path(storage.paths, storage.run_id).read_text(encoding="utf-8"))
    assert lock["token"] == winners[0]["token"]
    rows = storage.conn.execute("SELECT owner_token, released_at FROM worker_leases").fetchall()
    assert len(rows) == 1 and rows[0]["owner_token"] == winners[0]["token"]
    for proc in procs:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=25)
    control = WorkerControl(storage.conn, storage.paths, storage.run_id)
    assert control.acquire(now=utc_now()).running is True
    control.release(now=utc_now())


def test_a_second_process_cannot_force_past_a_live_guard(storage):
    """复核（19:57）：force 只是审计，不是并发许可——真实进程持着互斥时必须进不来。"""
    proc, holder = start_holder(storage)
    assert holder["ok"] is True
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    real = utc_now()
    with pytest.raises(WorkerAlreadyRunning, match="还没被排除"):
        control.acquire(now=real)
    with pytest.raises(WorkerAlreadyRunning, match="OS 互斥"):
        control.acquire(now=real, force=True)
    record = worker_mod.read_lock(worker_mod.lock_path(storage.paths, storage.run_id))
    assert record["token"] == holder["token"], "被拒的接管尝试不许改写归属记录"
    assert storage.repo.events(storage.run_id, kind="forced_takeover") == []
    assert let_go(proc) == 0
    assert control.acquire(now=real).running is True
    control.release(now=real)


def test_two_processes_both_seeing_a_dead_lock_still_admit_exactly_one(storage, dead_pid, tmp_path):
    """复核（19:57）指定要的场景：两个进程都 probe 到同一把遗留锁后一起冲。

    旧写法（probe→unlink→create）在这里会让两边都"成功"——后到的那方按旧快照把前一方刚建好的
    活锁删掉。现在所有权变更受 OS 互斥保护，败者既拿不到锁也不得删胜者的文件。
    """
    go = tmp_path / "GO"
    scripts = tmp_path / "stale_race.py"
    scripts.write_text(
        f"""
import sys, json, time
sys.path.insert(0, {str(SRC_DIR)!r})
from pathlib import Path
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db, worker as worker_mod
from xhs_discovery.storage.worker import WorkerControl
db_path, root, run_id, go = {str(storage.paths.db_path)!r}, {str(storage.paths.root)!r}, \\
    {storage.run_id!r}, {str(go)!r}
paths = RuntimePaths(Path(root))
lock = worker_mod.lock_path(paths, run_id)
seen = worker_mod.probe_lock(lock, expect_run_id=run_id)
print(json.dumps({{"saw": seen.state, "saw_pid": seen.pid}}), flush=True)
while not Path(go).exists():
    time.sleep(0.001)
conn = db.connect(db_path)
control = WorkerControl(conn, paths, run_id, lease_seconds=120)
try:
    control.acquire()
    print(json.dumps({{"ok": True, "token": control.owner_token}}), flush=True)
    sys.stdin.read()
    control.release()
except Exception as exc:  # noqa: BLE001
    print(json.dumps({{"ok": False, "error": type(exc).__name__}}), flush=True)
""",
        encoding="utf-8",
    )
    write_lock(storage, dead_pid)
    procs = [
        subprocess.Popen([sys.executable, str(scripts)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    seen = [json.loads(proc.stdout.readline()) for proc in procs]
    assert all(item["saw"] == "dead" for item in seen), f"两边都得先看到同一把遗留锁：{seen}"
    go.touch()
    results = [json.loads(proc.stdout.readline()) for proc in procs]
    winners = [item for item in results if item["ok"]]
    assert len(winners) == 1, f"遗留锁竞争也只能有一个赢家：{seen} -> {results}"
    record = worker_mod.read_lock(worker_mod.lock_path(storage.paths, storage.run_id))
    assert record["token"] == winners[0]["token"], "败者不得删掉或覆盖胜者的归属记录"
    rows = storage.conn.execute("SELECT owner_token, released_at FROM worker_leases").fetchall()
    assert len(rows) == 1 and rows[0]["owner_token"] == winners[0]["token"]
    for proc in procs:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=25)
    guard = worker_mod.OsGuard(worker_mod.guard_of(worker_mod.lock_path(storage.paths, storage.run_id)))
    assert guard.try_acquire() is True, "两个进程都退出后互斥必须已由系统释放"
    guard.release()


def test_a_live_guard_blocks_even_when_the_record_still_looks_dead(storage, dead_pid):
    """确定性地摆出"另一个进程正在临界区里"：记录还写着死 pid，也不许被删或被覆盖。

    这正是旧 probe→unlink→create 会翻车的时刻——按旧快照判断"锁是遗留的"，然后把别人
    刚建好的锁 unlink 掉。现在所有权变更先要拿到 OS 互斥，拿不到就直接失败，文件一个字节都不动。
    """
    path = worker_mod.lock_path(storage.paths, storage.run_id)
    write_lock(storage, dead_pid)
    guard = worker_mod.OsGuard(worker_mod.guard_of(path))
    assert guard.try_acquire() is True, "先占住互斥，模拟正在临界区里的另一个进程"
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    with pytest.raises(WorkerAlreadyRunning, match="OS 互斥"):
        control.acquire(now=NOW, force=True)
    assert worker_mod.read_lock(path)["pid"] == dead_pid, "被拒的一方不得动胜者的文件"
    assert storage.conn.execute("SELECT COUNT(*) AS n FROM worker_leases").fetchone()["n"] == 0
    assert worker_mod.read_lock(worker_mod.global_lock_path(storage.paths)) is None, "半拿状态必须撤回"
    guard.release()
    assert control.acquire(now=NOW).mine is True, "互斥交还后才允许接管遗留记录"
    assert storage.repo.events(storage.run_id, kind="stale_lock_removed")
    control.release(now=NOW)


def test_a_killed_process_leaves_a_dead_lock_that_may_be_taken_over(storage):
    """真崩溃：子进程被 SIGKILL，锁文件留在盘上但 pid 已死——这才允许接管。"""
    proc, holder = start_holder(storage)
    assert holder["ok"] is True
    proc.kill()
    proc.wait(timeout=15)
    path = worker_mod.lock_path(storage.paths, storage.run_id)
    assert path.exists(), "被杀死的进程来不及清理锁"
    control = WorkerControl(storage.conn, storage.paths, storage.run_id, lease_seconds=30)
    real = utc_now()
    status = control.status(now=real)
    assert status.lock_pid == holder["pid"] and status.lock_state == worker_mod.LOCK_DEAD
    assert status.occupied is False
    acquired = control.acquire(now=real)
    assert acquired.running and acquired.mine
    assert control.repo.events(storage.run_id, kind="stale_lock_removed")
    control.release(now=real)


# ------------------------------------------------------------------ 控制请求


def make_request(action, **overrides) -> ControlRequest:
    payload = dict(run_id="run-a", action=action, requested_at=NOW)
    payload.update(overrides)
    return ControlRequest(**payload)


def start(storage) -> None:
    storage.repo.set_run_status(storage.run_id, RunTermination(status=RunStatus.running), at=NOW)


def test_request_control_only_writes_intent(storage):
    start(storage)
    request_id = request_control(storage.conn, make_request(ControlActionType.pause, note="先看数据"))
    assert storage.repo.require_run(storage.run_id)["status"] == "running"
    pending = worker_mod.pending_controls(storage.conn, storage.run_id)
    assert [row["id"] for row in pending] == [request_id]
    assert pending[0]["note"] == "先看数据"
    assert pending[0]["applied_at"] is None


def test_pause_and_resume_roundtrip_keeps_the_reason_history(storage):
    start(storage)
    request_control(storage.conn, make_request(ControlActionType.pause, note="用户暂停"))
    applied = worker_mod.consume_control(storage.conn, storage.run_id, at=NOW + timedelta(seconds=1))
    assert applied["action"] == "pause"
    assert storage.repo.require_run(storage.run_id)["pause_reason"] == PauseReason.user.value
    request_control(storage.conn, make_request(ControlActionType.resume))
    worker_mod.consume_control(storage.conn, storage.run_id, at=NOW + timedelta(seconds=2))
    run = storage.repo.require_run(storage.run_id)
    assert run["status"] == "running" and run["pause_reason"] is None
    kinds = [event["kind"] for event in storage.repo.events(storage.run_id)]
    assert kinds.count("paused") == 1 and "resumed" in kinds
    assert worker_mod.pending_controls(storage.conn, storage.run_id) == []


def test_extend_consumes_the_ledger_and_only_adds_quota(storage):
    start(storage)
    ledger = Ledger(storage.conn, storage.run_id, {"suggest_attempts": 1, "search_attempts": 0})
    ledger.reserve("suggest_attempts", 1)
    request_control(storage.conn, make_request(ControlActionType.extend, suggest_budget_delta=4,
                                               search_budget_delta=2, note="追加一轮"))
    with pytest.raises(ValueError, match="需要 Ledger"):
        worker_mod.consume_control(storage.conn, storage.run_id, at=NOW)
    with pytest.raises(ValueError, match="属于另一个 run"):
        worker_mod.consume_control(storage.conn, storage.run_id,
                                   ledger=Ledger(storage.conn, "run-b", {"suggest_attempts": 1}), at=NOW)
    # 失败的消费不得把请求标记成已应用：额度追加是一次也不能少的契约
    assert worker_mod.pending_controls(storage.conn, storage.run_id) != []
    applied = worker_mod.consume_control(storage.conn, storage.run_id, ledger=ledger, at=NOW)
    assert applied["budget_revision"] == 2
    assert ledger.view("suggest_attempts").limit == 5
    assert ledger.view("search_attempts").limit == 2
    assert ledger.view("suggest_attempts").used == 0
    assert storage.repo.require_run(storage.run_id)["status"] == "running"
    assert json.loads(storage.repo.events(storage.run_id, kind="extended")[0]["detail_json"])["note"] == "追加一轮"


def test_extend_without_deltas_is_rejected_at_both_layers(storage):
    with pytest.raises(ValueError, match="extend 表示追加额度"):
        make_request(ControlActionType.extend)
    with pytest.raises(ValueError, match="只有 extend 可以携带预算增量"):
        make_request(ControlActionType.pause, suggest_budget_delta=3)
    with pytest.raises(ValueError, match="suggest_budget_delta"):
        make_request(ControlActionType.extend, suggest_budget_delta=0)
    with pytest.raises(sqlite3.IntegrityError):
        storage.conn.execute(
            "INSERT INTO control_requests(run_id, action, requested_at) VALUES (?, 'extend', ?)",
            (storage.run_id, _iso(NOW)),
        )


def test_cancel_ends_the_run_with_an_explicit_outcome(storage):
    start(storage)
    request_control(storage.conn, make_request(ControlActionType.cancel, note="不再采了"))
    applied = worker_mod.consume_all_controls(storage.conn, storage.run_id, at=NOW + timedelta(seconds=3))
    assert [row["action"] for row in applied] == ["cancel"]
    run = storage.repo.require_run(storage.run_id)
    assert run["status"] == "completed" and run["outcome"] == TerminalOutcome.cancelled.value
    assert run["finished_at"] is not None
    assert worker_mod.consume_control(storage.conn, storage.run_id) is None


def test_requests_are_applied_in_order_and_only_once(storage):
    start(storage)
    request_control(storage.conn, make_request(ControlActionType.pause, note="先停"))
    request_control(storage.conn, make_request(ControlActionType.extend, search_budget_delta=1,
                                               note="再补额度"))
    ledger = Ledger(storage.conn, storage.run_id, {"search_attempts": 0})
    applied = worker_mod.consume_all_controls(storage.conn, storage.run_id, ledger=ledger,
                                             at=NOW + timedelta(seconds=1))
    assert [row["action"] for row in applied] == ["pause", "extend"]
    assert applied[1]["budget_revision"] == 2
    assert ledger.view("search_attempts").limit == 1
    assert storage.repo.require_run(storage.run_id)["status"] == "paused"
    assert worker_mod.pending_controls(storage.conn, storage.run_id) == []
    assert worker_mod.consume_all_controls(storage.conn, storage.run_id) == []
    rows = storage.conn.execute("SELECT applied_at FROM control_requests ORDER BY id").fetchall()
    assert all(row["applied_at"] is not None for row in rows)


def test_illegal_control_action_is_rejected_by_the_schema(storage):
    with pytest.raises(sqlite3.IntegrityError):
        storage.conn.execute(
            "INSERT INTO control_requests(run_id, action, requested_at) VALUES (?, 'teleport', ?)",
            (storage.run_id, _iso(NOW)),
        )


def test_control_of_an_unknown_run_is_refused(storage):
    with pytest.raises(ValueError, match="run 不存在"):
        request_control(storage.conn, ControlRequest(run_id="run-ghost",
                                                    action=ControlActionType.pause,
                                                    requested_at=NOW))
