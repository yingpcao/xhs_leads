"""调度（P0-17）：Best-first + 分支 UCB + 60/25/15 槽位窗口 + 冷启动 + 确定性随机。

三种槽位各自的语义不同，混用会让"探索分配"变成一句口号：
- `high_score`：按 frontier 分数 Best-first（分数为 NULL 的候选排在最后，SQLite DESC 的 NULL 序）；
- `branch_exploration`：先用 UCB 选分支，再在该分支里取最高分；trials=0 的分支视为无穷大收益上界，
  因此新分支一定先被覆盖一次（计划里的"新分支先覆盖一次"）；
- `random_novelty`：只在未探索候选里用**种子化 RNG** 抽样，并记录可计算的选取概率。
  随机槽只支持探索，不构成无偏校准样本（校准另有冻结池，见 calibration.py）。

空桶回流：本槽类型没有可用候选时，按 high_score → branch_exploration → random_novelty → 任意待办
逐级借槽，并把"借自哪个桶"写进 selection_reason；绝不因为某个桶空了就停止调度或伪造候选。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from xhs_discovery.config import Settings
from xhs_discovery.models.enums import CalibrationSampleKind, SlotKind
from xhs_discovery.models.run import BranchStat
from xhs_discovery.storage.repository import Repository

SLOT_ORDER = (SlotKind.high_score, SlotKind.branch_exploration, SlotKind.random_novelty)


@dataclass(frozen=True)
class ScheduledSlot:
    frontier_id: int
    keyword_id: int
    normalized: str
    branch_key: str
    slot_kind: SlotKind
    sample_kind: CalibrationSampleKind
    score: float | None
    selection_reason: str
    selection_probability: float | None

    def as_dict(self) -> dict:
        return {
            "frontier_id": self.frontier_id,
            "keyword_id": self.keyword_id,
            "normalized": self.normalized,
            "branch_key": self.branch_key,
            "slot_kind": self.slot_kind.value,
            "sample_kind": self.sample_kind.value,
            "score": self.score,
            "selection_reason": self.selection_reason,
            "selection_probability": self.selection_probability,
        }


def slot_counts(settings: Settings) -> dict[SlotKind, int]:
    scheduler = settings.scheduler
    return {
        SlotKind.high_score: scheduler.slots_high_score,
        SlotKind.branch_exploration: scheduler.slots_branch_exploration,
        SlotKind.random_novelty: scheduler.slots_random_novelty,
    }


def plan_window(settings: Settings) -> tuple[SlotKind, ...]:
    """把一个窗口内的名额交错排列（Bresenham 式配额），使前几槽不会全是同一类型。

    连续 12 个高分槽再跟 5 个探索槽，会让早期轨迹完全看不到新分支；
    交错是确定性的，不引入随机，所以重放结果一致。
    """
    counts = slot_counts(settings)
    window = settings.scheduler.slot_window
    assigned = {kind: 0 for kind in SLOT_ORDER}
    plan: list[SlotKind] = []
    for position in range(1, window + 1):
        best: SlotKind | None = None
        best_gap = 0.0
        for kind in SLOT_ORDER:
            quota = counts[kind]
            if quota <= 0 or assigned[kind] >= quota:
                continue
            gap = quota * position / window - assigned[kind]
            if best is None or gap > best_gap:
                best, best_gap = kind, gap
        if best is None:
            break
        assigned[best] += 1
        plan.append(best)
    return tuple(plan)


def load_branch_stats(repo: Repository, run_id: str) -> dict[str, BranchStat]:
    rows = repo.conn.execute(
        "SELECT branch_key, trials, total_reward, stale_streak, min_validation_share_left"
        " FROM branch_stats WHERE run_id = ? ORDER BY branch_key",
        (run_id,),
    ).fetchall()
    return {
        str(row["branch_key"]): BranchStat(
            run_id=run_id,
            branch_id=str(row["branch_key"]),
            trials=int(row["trials"]),
            total_reward=float(row["total_reward"]),
            stale_streak=int(row["stale_streak"]),
            min_validation_share_left=int(row["min_validation_share_left"]),
        )
        for row in rows
    }


def ucb_values(stats: dict[str, BranchStat], *, coefficient: float) -> dict[str, float]:
    """UCB1：均值奖励 + 系数 × sqrt(ln(N)/n)。零试验分支返回 inf，保证冷启动先覆盖。"""
    total = sum(stat.trials for stat in stats.values())
    values: dict[str, float] = {}
    for key, stat in stats.items():
        if stat.trials == 0:
            values[key] = math.inf
            continue
        exploration = coefficient * math.sqrt(math.log(max(total, 1)) / stat.trials)
        values[key] = round(stat.mean_reward + exploration, 6)
    return values


def normalize_ucb(values: dict[str, float]) -> dict[str, float]:
    """把 UCB 映射到 [0,1] 作为 U 分量；inf（零试验）取 1.0，全零统计时统一给 0.5 中性值。"""
    finite = [value for value in values.values() if math.isfinite(value)]
    if not finite:
        return {key: (1.0 if math.isinf(value) else 0.5) for key, value in values.items()}
    low, high = min(finite), max(finite)
    span = high - low
    out: dict[str, float] = {}
    for key, value in values.items():
        if math.isinf(value):
            out[key] = 1.0
        elif span <= 0:
            out[key] = 0.5
        else:
            out[key] = round((value - low) / span, 6)
    return out


def pending_by_branch(repo: Repository, run_id: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in repo.frontier_rows(run_id, status="pending"):
        grouped.setdefault(str(row["branch_key"]), []).append(row)
    return grouped


def _pending_for_kind(rows: list[dict], kind: SlotKind) -> list[dict]:
    """槽类型匹配：slot_kind 为 NULL 的行任何槽都能取；已被标别的行只回到对应槽。"""
    return [row for row in rows if row["slot_kind"] is None or str(row["slot_kind"]) == kind.value]


def _best_first(rows: list[dict]) -> dict | None:
    scored = [row for row in rows if row["score"] is not None]
    if not scored:
        return None
    return sorted(scored, key=lambda row: (-float(row["score"]), row["id"]))[0]


class Scheduler:
    def __init__(self, repo: Repository, run_id: str, settings: Settings, *, rng: random.Random | None = None) -> None:
        self.repo = repo
        self.run_id = run_id
        self.settings = settings
        self.window = plan_window(settings)
        # 随机槽使用 run 的种子派生 RNG：同一个 run 配置重放得到同一条选取序列
        self.rng = rng if rng is not None else random.Random(settings.run.random_seed)

    def slot_kind_at(self, index: int) -> SlotKind:
        return self.window[index % len(self.window)]

    def ucb(self) -> dict[str, float]:
        return ucb_values(
            load_branch_stats(self.repo, self.run_id),
            coefficient=self.settings.scheduler.ucb_exploration_coefficient,
        )

    def next_slot(
        self, *, index: int, pending: dict[str, list[dict]] | None = None
    ) -> ScheduledSlot | None:
        """给定窗口内位置，选出下一个要执行的操作；无候选时返回 None（由停止策略判定）。"""
        pending = pending_by_branch(self.repo, self.run_id) if pending is None else pending
        if not any(pending.values()):
            return None
        wanted = self.slot_kind_at(index)
        order = (wanted,) + tuple(kind for kind in SLOT_ORDER if kind is not wanted)
        for kind in order:
            chosen = self._pick(kind, pending)
            if chosen is None:
                continue
            if kind is not wanted:
                chosen = replace(
                    chosen,
                    selection_reason=(
                        f"{chosen.selection_reason}；空桶回流：{wanted.value} 槽本次没有可用候选，"
                        f"借 {kind.value} 槽执行"
                    ),
                )
            return chosen
        return None

    def _pick(self, kind: SlotKind, pending: dict[str, list[dict]]) -> ScheduledSlot | None:
        stats = load_branch_stats(self.repo, self.run_id)
        if kind is SlotKind.high_score:
            candidates = [row for rows in pending.values() for row in _pending_for_kind(rows, kind)]
            row = _best_first(candidates)
            if row is None:
                return None
            return self._to_slot(
                row,
                kind,
                reason=(
                    f"高分槽 Best-first：分数 {float(row['score']):.6f}，"
                    f"分解见 score_breakdown_json"
                ),
                probability=None,
            )
        if kind is SlotKind.branch_exploration:
            values = self.ucb()
            branches = sorted(
                [key for key, rows in pending.items() if _pending_for_kind(rows, kind)],
                key=lambda key: (-values.get(key, math.inf), key),
            )
            if not branches:
                return None
            branch = branches[0]
            rows = _pending_for_kind(pending[branch], kind)
            cold = stats.get(branch)
            row = _best_first(rows) or (rows[0] if rows else None)
            if row is None:
                return None
            raw = values.get(branch, math.inf)
            label = "新分支零试验优先覆盖" if math.isinf(raw) else f"分支 UCB={raw:.6f}"
            need_validation = bool(cold and cold.min_validation_share_left > 0)
            reason = (
                f"分支探索槽：{label}（分支 {branch}）"
                + ("，该分支还有未用的最小验证份额" if need_validation else "")
            )
            return self._to_slot(row, kind, reason=reason, probability=None)
        candidates = [row for rows in pending.values() for row in _pending_for_kind(rows, kind)]
        novel = [row for row in candidates if row["score"] is None] or candidates
        if not novel:
            return None
        row = self.rng.choice(sorted(novel, key=lambda item: item["id"]))
        probability = round(1.0 / len(novel), 6)
        return self._to_slot(
            row,
            kind,
            reason=f"随机/新颖槽：种子 RNG 从 {len(novel)} 个待办里抽取（概率 {probability:.6f}）",
            probability=probability,
        )

    def _to_slot(
        self, row: dict, kind: SlotKind, *, reason: str, probability: float | None
    ) -> ScheduledSlot:
        return ScheduledSlot(
            frontier_id=int(row["id"]),
            keyword_id=int(row["keyword_id"]),
            normalized=str(row["normalized_text"]),
            branch_key=str(row["branch_key"]),
            slot_kind=kind,
            sample_kind=CalibrationSampleKind(str(row["sample_kind"])),
            score=None if row["score"] is None else float(row["score"]),
            selection_reason=reason,
            selection_probability=probability,
        )


__all__ = [
    "SLOT_ORDER",
    "ScheduledSlot",
    "Scheduler",
    "load_branch_stats",
    "normalize_ucb",
    "pending_by_branch",
    "plan_window",
    "slot_counts",
    "ucb_values",
]
