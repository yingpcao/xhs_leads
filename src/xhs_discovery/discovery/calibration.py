"""覆盖校准：独立冻结的候选池与固定随机种子抽样（P0-21）。

这块的目的不是"多采几个词"，而是给主动探索一个**外部参照**：
主动探索只会反复访问自己已经发现的东西，所以"发现了多少"必须拿一个不再变动的池子来对照，
否则覆盖率永远是被策略自己定义的。

三条硬约束（DEVELOPMENT_PLAN.md 第 5.3 节）：
- **池一旦冻结就不改**：成员写进 `run_events`，重启后按同一事件恢复，新发现的词不会补进池里；
- **预算单列**：走 `calibration_suggest_attempts`，与 `suggest_attempts` 互不占用，
  校准跑光不会让主动探索停摆，反之亦然；
- **标签隔离**：校准操作在 `operations.sample_kind = coverage_calibration`，
  停止策略的收敛窗口默认排除它们（见 `stopping.window_metrics`），
  报告出来的只有"该池的相对覆盖"，绝不写成平台总体覆盖率。

抽样顺序与成员集合都由 `random.Random(seed)` 决定，同一配置重放得到同一个池。
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.config import Settings
from xhs_discovery.models.enums import CalibrationSampleKind, OperationType
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository

POOL_EVENT = "calibration_pool_frozen"
CALIBRATION_BUDGET_KEY = "calibration_suggest_attempts"
SCOPE_NOTE = (
    "比例只描述冻结池内部：池来自冻结时刻已发现的候选，抽样也只覆盖池内成员，"
    "因此不能宣称为小红书平台总体覆盖率。"
)


@dataclass(frozen=True)
class CalibrationMember:
    keyword_id: int
    normalized: str
    branch_key: str | None
    depth: int

    def as_dict(self) -> dict:
        return {
            "keyword_id": self.keyword_id,
            "normalized": self.normalized,
            "branch_key": self.branch_key,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "CalibrationMember":
        return cls(
            keyword_id=int(payload["keyword_id"]),
            normalized=str(payload["normalized"]),
            branch_key=payload.get("branch_key"),
            depth=int(payload.get("depth", 0)),
        )


@dataclass(frozen=True)
class CalibrationPool:
    run_id: str
    pool_id: str
    seed: int
    requested_size: int
    frozen_at: str
    members: tuple[CalibrationMember, ...]

    @property
    def size(self) -> int:
        return len(self.members)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "pool_id": self.pool_id,
            "seed": self.seed,
            "requested_size": self.requested_size,
            "frozen_at": self.frozen_at,
            "members": [member.as_dict() for member in self.members],
        }


def _candidate_rows(repo: Repository, run_id: str) -> list[CalibrationMember]:
    """冻结时刻的候选快照：排除判定明显偏题的词，其余按 keyword_id 稳定排序。"""
    rows = repo.conn.execute(
        "SELECT rk.keyword_id, k.normalized_text, rk.depth, b.branch_key"
        " FROM run_keywords rk JOIN keywords k ON k.id = rk.keyword_id"
        " LEFT JOIN branches b ON b.id = rk.branch_id"
        " WHERE rk.run_id = ? AND rk.status <> 'irrelevant' ORDER BY rk.keyword_id",
        (run_id,),
    ).fetchall()
    return [
        CalibrationMember(
            keyword_id=int(row["keyword_id"]),
            normalized=str(row["normalized_text"]),
            branch_key=row["branch_key"],
            depth=int(row["depth"]),
        )
        for row in rows
    ]


def freeze_pool(
    repo: Repository,
    run_id: str,
    settings: Settings,
    *,
    size: int | None = None,
    seed: int | None = None,
    at: datetime | None = None,
) -> CalibrationPool:
    """从当前候选里定容抽样并冻结写入事件；已存在冻结池时直接返回既有池（不可改写）。"""
    existing = load_pool(repo, run_id)
    if existing is not None:
        return existing
    requested = settings.budget.calibration_suggest_attempts if size is None else size
    if requested <= 0:
        raise ValueError("calibration_suggest_attempts 为 0 时没有校准预算，不应冻结池")
    pool_seed = settings.run.random_seed if seed is None else seed
    candidates = _candidate_rows(repo, run_id)
    if not candidates:
        raise ValueError("当前 run 还没有可冻结的候选词，无法建立校准池")
    drawn = random.Random(pool_seed).sample(candidates, k=min(requested, len(candidates)))
    # 事件里按 keyword_id 排序保存：抽样顺序只由种子决定，落库顺序不影响可重放性
    drawn = sorted(drawn, key=lambda member: member.keyword_id)
    digest = hashlib.sha256(
        json.dumps([member.as_dict() for member in drawn], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    pool = CalibrationPool(
        run_id=run_id,
        pool_id=f"cp-{digest[:16]}",
        seed=pool_seed,
        requested_size=requested,
        frozen_at=(at or datetime.now()).isoformat(),
        members=tuple(drawn),
    )
    repo.record_event(
        run_id,
        POOL_EVENT,
        detail={**pool.as_dict(), "member_count": pool.size},
        at=at,
    )
    return pool


def load_pool(repo: Repository, run_id: str) -> CalibrationPool | None:
    """恢复冻结池：取最后一条冻结事件（重放同一次配置会写出同一 pool_id）。"""
    rows = repo.events(run_id, kind=POOL_EVENT)
    if not rows:
        return None
    payload = json.loads(rows[-1]["detail_json"] or "{}")
    return CalibrationPool(
        run_id=run_id,
        pool_id=str(payload["pool_id"]),
        seed=int(payload["seed"]),
        requested_size=int(payload["requested_size"]),
        frozen_at=str(payload["frozen_at"]),
        members=tuple(CalibrationMember.from_dict(item) for item in payload["members"]),
    )


def sampled_keyword_ids(conn, run_id: str) -> set[int]:
    """已成功采样过的成员：只认 completed/partial，失败重试仍留在待办里。"""
    rows = conn.execute(
        "SELECT DISTINCT so.keyword_id FROM suggestion_observations so"
        " JOIN operations op ON op.id = so.operation_id"
        " WHERE so.run_id = ? AND op.sample_kind = ? AND so.state IN ('completed', 'partial')",
        (run_id, CalibrationSampleKind.coverage_calibration.value),
    ).fetchall()
    return {int(row["keyword_id"]) for row in rows}


def next_sample(pool: CalibrationPool, *, done: set[int]) -> CalibrationMember | None:
    for member in pool.members:
        if member.keyword_id not in done:
            return member
    return None


def pending_samples(
    repo: Repository, run_id: str, pool: CalibrationPool, ledger: Ledger
) -> tuple[CalibrationMember, ...]:
    if not ledger.can_afford(CALIBRATION_BUDGET_KEY):
        return ()
    done = sampled_keyword_ids(repo.conn, run_id)
    return tuple(member for member in pool.members if member.keyword_id not in done)


def plan_next(
    repo: Repository, run_id: str, pool: CalibrationPool, ledger: Ledger
) -> CalibrationMember | None:
    """校准队列就是池本身：不进 frontier，避免 ON CONFLICT 把主动探索的待办重置。"""
    return next_sample(pool, done=sampled_keyword_ids(repo.conn, run_id))


@dataclass(frozen=True)
class CalibrationCoverage:
    pool_id: str
    pool_size: int
    sampled: int
    discovered_by_exploration: int
    platform_returned_self: int
    sample_share: float | None
    discovery_share: float | None
    platform_return_share: float | None
    members: tuple[dict, ...]
    scope_note: str

    def as_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "pool_size": self.pool_size,
            "sampled": self.sampled,
            "discovered_by_exploration": self.discovered_by_exploration,
            "platform_returned_self": self.platform_returned_self,
            "sample_share": self.sample_share,
            "discovered_share_of_pool": self.discovery_share,
            "platform_return_share": self.platform_return_share,
            "scope_note": self.scope_note,
            "members": list(self.members),
        }


def coverage(repo: Repository, run_id: str, pool: CalibrationPool) -> CalibrationCoverage:
    """三个分母都只用池内成员数，任何一项都不外推成平台覆盖率。"""
    sampled = sampled_keyword_ids(repo.conn, run_id)
    members: list[dict] = []
    discovered = 0
    returned_self = 0
    for member in pool.members:
        seen_by_exploration = repo.conn.execute(
            "SELECT COUNT(*) AS n, MIN(si.rank) AS best_rank FROM suggestion_items si"
            " JOIN suggestion_observations so ON so.id = si.observation_id"
            " JOIN operations op ON op.id = so.operation_id"
            " WHERE so.run_id = ? AND si.keyword_id = ?"
            " AND COALESCE(op.sample_kind, '') <> 'coverage_calibration'"
            " AND so.state IN ('completed', 'partial')",
            (run_id, member.keyword_id),
        ).fetchone()
        observed_times = int(seen_by_exploration["n"])
        if observed_times:
            discovered += 1
        # 平台是否把该词自己又推荐回来：可见性证据，与"我们是否发现过"是两件事
        self_returned = repo.conn.execute(
            "SELECT COUNT(*) AS n FROM suggestion_items si"
            " JOIN suggestion_observations so ON so.id = si.observation_id"
            " JOIN operations op ON op.id = so.operation_id"
            " WHERE so.run_id = ? AND so.keyword_id = ? AND si.keyword_id = ?"
            " AND op.sample_kind = 'coverage_calibration' AND so.state IN ('completed', 'partial')",
            (run_id, member.keyword_id, member.keyword_id),
        ).fetchone()
        if int(self_returned["n"]):
            returned_self += 1
        members.append(
            {
                **member.as_dict(),
                "sampled": member.keyword_id in sampled,
                "observed_by_exploration_times": observed_times,
                "best_rank_by_exploration": (
                    None
                    if seen_by_exploration["best_rank"] is None
                    else int(seen_by_exploration["best_rank"])
                ),
                "platform_returned_self": bool(int(self_returned["n"])),
            }
        )
    size = pool.size

    def share(count: int) -> float | None:
        return None if size == 0 else round(count / size, 6)

    return CalibrationCoverage(
        pool_id=pool.pool_id,
        pool_size=size,
        sampled=len([member for member in pool.members if member.keyword_id in sampled]),
        discovered_by_exploration=discovered,
        platform_returned_self=returned_self,
        sample_share=share(len([m for m in pool.members if m.keyword_id in sampled])),
        discovery_share=share(discovered),
        platform_return_share=share(returned_self),
        members=tuple(members),
        scope_note=SCOPE_NOTE,
    )


def calibration_operation_type() -> OperationType:
    """校准只用联想观察，预算键与主动探索的 suggest 分开。"""
    return OperationType.suggest


__all__ = [
    "CALIBRATION_BUDGET_KEY",
    "POOL_EVENT",
    "SCOPE_NOTE",
    "CalibrationCoverage",
    "CalibrationMember",
    "CalibrationPool",
    "calibration_operation_type",
    "coverage",
    "freeze_pool",
    "load_pool",
    "next_sample",
    "pending_samples",
    "plan_next",
    "sampled_keyword_ids",
]
