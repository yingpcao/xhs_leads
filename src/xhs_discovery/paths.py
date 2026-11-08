"""运行期路径布局：数据库、run 目录、每次 attempt 的独立产物目录。

原始响应按 run/operation/revisit/attempt 分层落盘，保证并发运行与恢复导入互不覆盖。
产物解析一律返回 `resolve()` 后的绝对路径，并按调用方给定的 run 目录限制归属（见 relpath）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from xhs_discovery.models.enums import OperationType
from xhs_discovery.relpath import ensure_inside, split_relative

DB_FILENAME = "xhs_discovery.db"
# 本机同时只允许一个 worker（跨 run）：per-run 锁之外还要一把全局锁
GLOBAL_LOCK_FILENAME = "worker-global.lock"


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    @property
    def db_path(self) -> Path:
        return self.root / DB_FILENAME

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def global_lock_path(self) -> Path:
        """本机唯一 worker 的跨 run 锁：per-run 锁不能替代它，否则每个 run 都能各起一个。"""
        return self.root / GLOBAL_LOCK_FILENAME

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / _safe_segment(run_id)

    def operation_dir(self, run_id: str, keyword_id: int, op: OperationType, revisit_seq: int) -> Path:
        return self.run_dir(run_id) / "operations" / f"k{keyword_id}" / op.value / f"r{revisit_seq}"

    def attempt_dir(
        self, run_id: str, keyword_id: int, op: OperationType, revisit_seq: int, attempt_no: int
    ) -> Path:
        return self.operation_dir(run_id, keyword_id, op, revisit_seq) / f"a{attempt_no}"

    def relative(self, path: Path) -> str:
        """写入 raw_artifacts 的相对路径（Posix 写法），避免绝对路径进库。"""
        parts = Path(path).resolve().relative_to(self.root).parts
        split_relative(PurePosixPath(*parts).as_posix())
        return PurePosixPath(*parts).as_posix()

    def resolve_artifact(self, relative_path: str, *, under: Path | None = None) -> Path:
        """解析产物路径并返回绝对路径；`under` 给出时进一步限制到该目录（通常是本次 run 目录）。"""
        parts = split_relative(relative_path)
        candidate = self.root / Path(*parts)
        resolved = ensure_inside(candidate, self.runs_dir)
        return ensure_inside(resolved, under) if under is not None else resolved

    def resolve_within_run(self, relative_path: str, run_id: str) -> Path:
        """重启补导入与写入的唯一入口：路径必须解析进本次 run 目录。"""
        return self.resolve_artifact(relative_path, under=self.run_dir(run_id))


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
    if not cleaned or cleaned.startswith("."):
        raise ValueError(f"非法路径片段：{value!r}")
    return cleaned
