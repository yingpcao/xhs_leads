"""原始产物原子落盘与重启补导入（P0-09）。

纪律来自 `docs/DATA_MODEL.md` 第 5/6 节：
- 先写临时文件再原子改名，然后在同一个短事务里登记 `raw_artifacts` 引用；
- 产物只允许本次 run 目录内的相对路径，落盘与补导入都按 `resolve()` 后的真实路径确认归属；
- 补导入按内容哈希幂等：重复扫描不新增行，也就不会被重复扣费；
- 无法归属到 operation 的产物显式记 `artifact_orphaned`，不假装导入成功；
- 重启后仍拿不到可靠结果的 operation 标 `uncertain`，宁可承认不确定也不把崩溃窗口读成"没有结果"。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from xhs_discovery.models.common import ArtifactKind, utc_now
from xhs_discovery.models.enums import OperationState, OperationType
from xhs_discovery.paths import GLOBAL_LOCK_FILENAME, RuntimePaths
from xhs_discovery.relpath import split_relative
from xhs_discovery.storage.db import transaction
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import GUARD_SUFFIX, LOCK_FILENAME

KINDS: tuple[str, ...] = ("stdout_json", "stderr_log", "raw_response", "export")
_TEMP_PREFIX = ".tmp-"


class ArtifactConflict(RuntimeError):
    """同一 run 内同一路径出现不同内容：历史引用不可复现，必须写到新的 attempt 目录。"""


def _iso(value: datetime | None) -> str:
    if value is None:
        return utc_now().isoformat()
    if value.tzinfo is None:
        raise ValueError("产物时间戳必须带时区")
    return value.isoformat()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def kind_for_filename(filename: str) -> ArtifactKind:
    """补导入时按扩展名推断类别；推断不了就记 raw_response，不猜测它是 stdout_json。"""
    lowered = filename.lower()
    if lowered.endswith((".log", ".txt", ".err")):
        return "stderr_log"
    if lowered.endswith(".json"):
        return "stdout_json"
    return "raw_response"


@dataclass(frozen=True)
class LandedArtifact:
    artifact_id: int
    rel_path: str
    sha256: str
    size_bytes: int
    kind: str
    file_written: bool
    row_inserted: bool

    def as_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "file_written": self.file_written,
            "row_inserted": self.row_inserted,
        }


def _require_run(conn: sqlite3.Connection, run_id: str) -> None:
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
        raise ValueError(f"run 不存在：{run_id}")


def _existing(conn: sqlite3.Connection, run_id: str, rel_path: str) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT id, sha256 FROM raw_artifacts WHERE run_id = ? AND rel_path = ?",
        (run_id, rel_path),
    ).fetchone()
    return None if row is None else (int(row["id"]), str(row["sha256"]))


def _insert(conn: sqlite3.Connection, run_id: str, rel_path: str, digest: str, size: int, kind: str,
            operation_id: int | None, attempt_id: int | None, stamp: str) -> int:
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO raw_artifacts(run_id, operation_id, attempt_id, rel_path, sha256,"
            " size_bytes, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, operation_id, attempt_id, rel_path, digest, size, kind, stamp),
        )
        row = conn.execute(
            "SELECT id FROM raw_artifacts WHERE rel_path = ? AND sha256 = ?", (rel_path, digest)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def write_artifact(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    *,
    kind: ArtifactKind,
    payload: bytes | str,
    operation_id: int | None = None,
    attempt_id: int | None = None,
    filename: str | None = None,
    rel_path: str | None = None,
    at: datetime | None = None,
) -> LandedArtifact:
    """落盘一份原始产物：内容寻址、原子改名，再在事务里登记引用。"""
    if kind not in KINDS:
        raise ValueError(f"非法产物类别：{kind}")
    _require_run(conn, run_id)
    body = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    digest = _digest(body)
    rel = rel_path if rel_path is not None else default_rel_path(
        conn, paths, run_id, operation_id=operation_id, attempt_id=attempt_id, filename=filename
    )
    target = paths.resolve_within_run(rel, run_id)

    found = _existing(conn, run_id, rel)
    if found is not None:
        existing_id, existing_sha = found
        if existing_sha != digest:
            raise ArtifactConflict(
                f"{rel} 已登记为 {existing_sha[:12]}…，本次内容 {digest[:12]}… 不同；"
                "重试请写入新的 attempt 目录，改写旧文件会让历史引用不可复现"
            )
        if target.exists() and _digest(target.read_bytes()) == digest:
            return LandedArtifact(existing_id, rel, digest, len(body), kind, False, False)

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=_TEMP_PREFIX, suffix=".part")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    artifact_id = _insert(conn, run_id, rel, digest, len(body), kind, operation_id, attempt_id, _iso(at))
    inserted = found is None
    return LandedArtifact(artifact_id, rel, digest, len(body), kind, True, inserted)


def default_rel_path(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    *,
    operation_id: int | None,
    attempt_id: int | None,
    filename: str | None,
) -> str:
    """按 run/operations/k词/op/r重探/a尝试 的分层生成路径，保证并发 attempt 互不覆盖。"""
    if not filename:
        raise ValueError("未给出 rel_path 时必须给出 filename")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValueError(f"文件名不得含目录分隔或以点开头：{filename}")
    if operation_id is None or attempt_id is None:
        raise ValueError("未给出 rel_path 时必须同时给出 operation_id 与 attempt_id 才能推导分层目录")
    op = conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if op is None or op["run_id"] != run_id:
        raise ValueError(f"operation {operation_id} 不属于 run {run_id}")
    attempt = conn.execute("SELECT * FROM operation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if attempt is None or int(attempt["operation_id"]) != int(operation_id):
        raise ValueError(f"attempt {attempt_id} 不属于 operation {operation_id}")
    directory = paths.attempt_dir(
        run_id,
        int(op["keyword_id"]),
        OperationType(op["operation_type"]),
        int(op["revisit_seq"]),
        int(attempt["attempt_no"]),
    )
    return paths.relative(directory / filename)


@dataclass(frozen=True)
class AttemptKey:
    keyword_id: int
    operation_type: OperationType
    revisit_seq: int
    attempt_no: int


def parse_attempt_path(rel_path: str, paths: RuntimePaths, run_id: str) -> AttemptKey | None:
    """从 `runs/<run>/operations/k<词>/<op>/r<重探>/a<尝试>/<文件>` 还原归属；结构不符返回 None。

    允许一层 `cli/` 子目录：CLI 数据源把 stdout/stderr/工具 out/元数据落在 attempt 目录下的
    `cli/` 里，重启补导入必须还能把它们归属回本 run 本 attempt，否则真实响应会变成孤儿产物。
    """
    parts = split_relative(rel_path)
    prefix = tuple(paths.relative(paths.run_dir(run_id)).split("/"))
    if parts[: len(prefix) + 1] != prefix + ("operations",):
        return None
    tail = parts[len(prefix) + 1:]
    if len(tail) == 6 and tail[4] == "cli":
        tail = tail[:4] + (tail[5],)
    if len(tail) != 5:
        return None
    if not (tail[0].startswith("k") and tail[2].startswith("r") and tail[3].startswith("a")):
        return None
    numbers = (tail[0][1:], tail[2][1:], tail[3][1:])
    if not all(item.isdigit() for item in numbers):
        return None
    if tail[1] not in {item.value for item in OperationType}:
        return None
    return AttemptKey(int(numbers[0]), OperationType(tail[1]), int(numbers[1]), int(numbers[2]))


def _is_worker_bookkeeping(name: str) -> bool:
    """worker 锁的归属记录、OS 互斥文件与它们的临时变体都不是采集产物。

    `*.guard` 由活着的 worker 用 OS 字节锁独占，Windows 上读它会抛 PermissionError；
    补导入按目录扫产物时把它们当文件读，等于让恢复路径去抢 worker 的锁。
    """
    return name.endswith(GUARD_SUFFIX) or name.startswith((LOCK_FILENAME, GLOBAL_LOCK_FILENAME))


def scan_unregistered(conn: sqlite3.Connection, paths: RuntimePaths, run_id: str) -> list[str]:
    """扫描本次 run 目录里尚未入库的产物（跳过临时文件与别的 run）。"""
    run_dir = paths.run_dir(run_id)
    if not run_dir.exists():
        return []
    registered = {
        str(row["rel_path"])
        for row in conn.execute("SELECT rel_path FROM raw_artifacts WHERE run_id = ?", (run_id,))
    }
    found: list[str] = []
    for candidate in sorted(run_dir.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.name.startswith(_TEMP_PREFIX) or candidate.name.endswith(".part"):
            continue
        if _is_worker_bookkeeping(candidate.name):
            continue
        # 用未解析的链接路径记账：指向 run 外的符号链接会在这里保留本 run 内的位置，
        # 真正的归属判定留到 import 的 resolve 检查，避免把别人的产物写成自己的 rel_path。
        if not candidate.is_relative_to(paths.root):
            continue
        rel = candidate.relative_to(paths.root).as_posix()
        if rel not in registered:
            found.append(rel)
    return found


@dataclass
class ImportReport:
    imported: list[LandedArtifact] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    escaped: list[str] = field(default_factory=list)

    @property
    def imported_count(self) -> int:
        return len(self.imported)


def import_unregistered(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    *,
    at: datetime | None = None,
) -> ImportReport:
    """重启补导入：按目录分层还原 operation/attempt 归属，按内容哈希幂等登记。"""
    report = ImportReport()
    stamp = _iso(at)
    repo = Repository(conn, migrate=False)
    for rel in scan_unregistered(conn, paths, run_id):
        try:
            target = paths.resolve_within_run(rel, run_id)
        except ValueError:
            report.escaped.append(rel)
            continue
        if not target.is_file():
            report.escaped.append(rel)
            continue
        body = target.read_bytes()
        digest = _digest(body)
        found = _existing(conn, run_id, rel)
        if found is not None:
            if found[1] != digest:
                report.conflicts.append(rel)
            continue
        key = parse_attempt_path(rel, paths, run_id)
        operation_id = attempt_id = None
        if key is not None:
            operation = repo.operation_by_key(
                run_id, key.keyword_id, key.operation_type, key.revisit_seq
            )
            if operation is not None:
                operation_id = int(operation["id"])
                attempt = repo.attempt_by_number(operation_id, key.attempt_no)
                if attempt is not None:
                    attempt_id = int(attempt["id"])
        if operation_id is None:
            report.orphaned.append(rel)
            repo.record_event(
                run_id, "artifact_orphaned", detail={"rel_path": rel, "sha256": digest}, at=at
            )
        artifact_id = _insert(conn, run_id, rel, digest, len(body), kind_for_filename(target.name),
                              operation_id, attempt_id, stamp)
        report.imported.append(
            LandedArtifact(artifact_id, rel, digest, len(body), kind_for_filename(target.name), False, True)
        )
    if report.imported or report.orphaned or report.conflicts or report.escaped:
        repo.record_event(
            run_id,
            "imported_after_restart",
            detail={
                "imported": [row.rel_path for row in report.imported],
                "orphaned": report.orphaned,
                "conflicts": report.conflicts,
                "escaped": report.escaped,
            },
            at=at,
        )
    return report


@dataclass
class RecoveryReport:
    imports: ImportReport
    reconciled: list[dict] = field(default_factory=list)
    uncertain: list[int] = field(default_factory=list)
    released_reservations: list[int] = field(default_factory=list)


def recover_run(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    *,
    ledger=None,
    at: datetime | None = None,
) -> RecoveryReport:
    """崩溃恢复：先补导入，再用已写入的 attempt 事实回填，回填不了的标 uncertain。

    `completed/partial/skipped/unsupported` 的 operation 不重放（第 6.3 条）；
    `pending` 是还没轮到的小队列项，保持原状；`in_flight` 有终态 attempt 结果的按事实回填，
    没有任何结果的标 uncertain。
    """
    repo = Repository(conn, migrate=False)
    imports = import_unregistered(conn, paths, run_id, at=at)
    reconciled: list[dict] = []
    uncertain: list[int] = []
    for operation in repo.unfinished_operations(run_id):
        operation_id = int(operation["id"])
        if operation["status"] != "in_flight":
            # pending 只是还没轮到的排队项，不是崩溃窗口：留队列自己认领，别标 uncertain。
            continue
        finished = [
            row
            for row in repo.attempts(operation_id)
            if row["finished_at"] is not None and row["result_state"] is not None
        ]
        if finished:
            latest = max(finished, key=lambda row: int(row["attempt_no"]))
            state = OperationState(str(latest["result_state"]))
            repo.set_operation_status(operation_id, state, at=at)
            reconciled.append(
                {
                    "operation_id": operation_id,
                    "from": operation["status"],
                    "to": state.value,
                    "attempt_no": int(latest["attempt_no"]),
                }
            )
            continue
        repo.set_operation_status(operation_id, OperationState.uncertain, at=at)
        uncertain.append(operation_id)
        repo.record_event(
            run_id,
            "artifact_orphaned",
            detail={
                "operation_id": operation_id,
                "reason": "崩溃窗口内没有任何 attempt 写入结果，无法判定是否有外部访问，标 uncertain",
            },
            at=at,
        )
    if uncertain:
        repo.record_event(
            run_id, "imported_after_restart", detail={"uncertain_operations": uncertain}, at=at
        )
    released: list[int] = []
    if ledger is not None:
        released = ledger.release_stale_reservations(at=at)
    return RecoveryReport(imports, reconciled, uncertain, released)


__all__ = [
    "KINDS",
    "ArtifactConflict",
    "AttemptKey",
    "ImportReport",
    "LandedArtifact",
    "RecoveryReport",
    "default_rel_path",
    "import_unregistered",
    "kind_for_filename",
    "parse_attempt_path",
    "recover_run",
    "scan_unregistered",
    "write_artifact",
]
