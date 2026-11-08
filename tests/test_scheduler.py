"""P0-17：Best-first、分支 UCB、60/25/15 槽位窗口与确定性随机。

计划要求"探索分配"是可核对的，因此这里逐个守住：
- 窗口内名额交错（Bresenham 配额），前几槽不得全是同一类型；
- 零试验分支的收益上界是 +inf，新分支一定先被覆盖一次；
- 随机槽只用 run 种子派生的 RNG，并记录可计算的选取概率；
- 空桶必须回流借槽并在选取理由里写清借自哪个桶，不能因为某个桶空了就停止或伪造候选。
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from pathlib import Path

import pytest

from xhs_discovery.config import Settings, load_settings
from xhs_discovery.discovery.scheduler import (
    SLOT_ORDER,
    Scheduler,
    load_branch_stats,
    normalize_ucb,
    pending_by_branch,
    plan_window,
    slot_counts,
    ucb_values,
)
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    OperationType,
    ResultState,
    SlotKind,
)
from xhs_discovery.models.run import BranchStat, RewardUpdate
from xhs_discovery.text.normalize import KeywordNormalizer

from tests.conftest import T0

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def default_settings() -> Settings:
    return load_settings(ROOT / "config" / "default.yaml")


def register(
    storage,
    text: str,
    branch_key: str,
    *,
    score: float | None = None,
    slot_kind: SlotKind | None = None,
    reason: str = "测试入队",
    sample_kind: CalibrationSampleKind = CalibrationSampleKind.active_exploration,
    min_validation_share_left: int = 0,
) -> int:
    normalized = KeywordNormalizer().normalize(text).normalized
    keyword_id = storage.repo.ensure_keyword(normalized, text, seen_at=T0)
    storage.repo.ensure_branch(
        storage.run_id, branch_key, min_validation_share_left=min_validation_share_left
    )
    storage.repo.upsert_run_keyword(
        storage.run_id, keyword_id, depth=1,
        branch_id=storage.repo.branch_id(storage.run_id, branch_key),
        origin="observed", status="queued", seen_at=T0,
    )
    return storage.repo.enqueue(
        storage.run_id, keyword_id, branch_key,
        score=score, slot_kind=slot_kind, selection_reason=reason, sample_kind=sample_kind,
    )


def reward_update(branch_key: str, *, clusters: int = 0, keywords: int = 1) -> RewardUpdate:
    return RewardUpdate(
        run_id="run-a",
        keyword_id=1,
        # 内容采样类操作才允许携带问题簇奖励（suggest 只拿关键词奖励，由契约层强制）
        operation_type=OperationType.search if clusters else OperationType.suggest,
        revisit_seq=0,
        attempt_no=1,
        attributed_branch_id=branch_key,
        new_valid_problem_clusters=clusters,
        new_related_keywords=keywords,
        normalized_cost=1.0,
        cluster_keyword_weight=0.2,
        result_state=ResultState.completed,
        valid_cluster_evidence_ids=[7] if clusters else [],
    )


def longest_run(plan) -> int:
    best = current = 0
    for index in range(len(plan)):
        current = current + 1 if index and plan[index] is plan[index - 1] else 1
        best = max(best, current)
    return best


def test_slot_counts_follow_the_configured_ratio(default_settings: Settings):
    counts = slot_counts(default_settings)
    assert counts == {
        SlotKind.high_score: default_settings.scheduler.slots_high_score,
        SlotKind.branch_exploration: default_settings.scheduler.slots_branch_exploration,
        SlotKind.random_novelty: default_settings.scheduler.slots_random_novelty,
    }
    total = sum(counts.values())
    assert total == default_settings.scheduler.slot_window
    assert counts[SlotKind.high_score] / total == pytest.approx(0.60)
    assert counts[SlotKind.branch_exploration] / total == pytest.approx(0.25)
    assert counts[SlotKind.random_novelty] / total == pytest.approx(0.15)


def test_window_interleaves_kinds_instead_of_batching_them(default_settings: Settings):
    plan = plan_window(default_settings)
    assert len(plan) == default_settings.scheduler.slot_window
    assert Counter(plan) == Counter(slot_counts(default_settings))
    # 连续高分槽会把新分支挤到看不见：前四槽里必须出现别的类型
    assert len(set(plan[:4])) > 1
    assert longest_run(plan) < 6


def test_window_plan_is_deterministic_and_shrinks_with_config():
    first = plan_window(load_settings(ROOT / "config" / "default.yaml"))
    second = plan_window(load_settings(ROOT / "config" / "default.yaml"))
    assert first == second
    tiny = plan_window(load_settings(ROOT / "config" / "tests-convergence-tiny.yaml"))
    assert len(tiny) == 8
    assert Counter(tiny) == Counter({
        SlotKind.high_score: 5, SlotKind.branch_exploration: 2, SlotKind.random_novelty: 1
    })


def test_slot_kind_at_wraps_the_window(default_settings: Settings, storage):
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    window = plan_window(default_settings)
    assert scheduler.slot_kind_at(0) == window[0]
    assert scheduler.slot_kind_at(len(window)) == window[0]
    assert scheduler.slot_kind_at(len(window) + 3) == window[3]


def test_ucb_gives_zero_trial_branches_infinite_upper_bound():
    stats = {
        "b-new": BranchStat(run_id="r", branch_id="b-new", trials=0, total_reward=0.0,
                            stale_streak=0, min_validation_share_left=1),
        "b-old": BranchStat(run_id="r", branch_id="b-old", trials=10, total_reward=5.0,
                             stale_streak=0, min_validation_share_left=0),
    }
    values = ucb_values(stats, coefficient=1.0)
    assert values["b-new"] == math.inf
    assert values["b-old"] == pytest.approx(round(0.5 + math.sqrt(math.log(10) / 10), 6))
    # 试验越多探索项越小：同均值下，被访问少的分支仍然更靠后
    heavier = ucb_values(
        {"b": BranchStat(run_id="r", branch_id="b", trials=40, total_reward=20.0, stale_streak=0)},
        coefficient=1.0,
    )
    assert heavier["b"] == pytest.approx(round(0.5 + math.sqrt(math.log(40) / 40), 6))
    assert heavier["b"] < values["b-old"]


def test_normalize_ucb_maps_to_unit_interval():
    normalized = normalize_ucb({"a": math.inf, "b": 0.2, "c": 0.6})
    assert normalized["a"] == 1.0
    assert normalized["b"] == 0.0
    assert normalized["c"] == 1.0
    assert normalize_ucb({"a": 0.3, "b": 0.3}) == {"a": 0.5, "b": 0.5}
    assert normalize_ucb({}) == {}


def test_branch_exploration_covers_a_new_branch_first(storage, default_settings: Settings):
    warm = register(storage, "上海中考志愿填报", "b-known", score=0.9)
    cold = register(storage, "上海中考名额分配", "b-new", score=0.1)
    storage.repo.apply_reward(reward_update("b-known"), at=T0)
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    index = plan_window(default_settings).index(SlotKind.branch_exploration)
    slot = scheduler.next_slot(index=index)
    # 分数更高的已知分支让位：零试验分支的 UCB 上界是 inf，必须先覆盖一次
    assert slot.frontier_id == cold and slot.frontier_id != warm
    assert slot.branch_key == "b-new"
    assert slot.slot_kind is SlotKind.branch_exploration
    assert "新分支零试验优先覆盖" in slot.selection_reason


def test_branch_exploration_notes_remaining_validation_share(storage, default_settings: Settings):
    register(storage, "上海中考志愿填报", "b-known", score=0.9)
    register(storage, "上海中考名额分配", "b-new", score=0.2, min_validation_share_left=2)
    # 先给已知分支一次试验，让 UCB 的 inf 排序把新分支顶上来
    storage.repo.apply_reward(reward_update("b-known"), at=T0)
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    index = plan_window(default_settings).index(SlotKind.branch_exploration)
    slot = scheduler.next_slot(index=index)
    assert slot.branch_key == "b-new"
    assert "未用的最小验证份额" in slot.selection_reason
    assert slot.as_dict()["sample_kind"] == "active_exploration"


def test_high_score_slot_is_best_first_across_branches(storage, default_settings: Settings):
    register(storage, "上海中考低分", "b-a", score=0.2)
    best = register(storage, "上海中考高分", "b-b", score=0.85)
    register(storage, "上海中考未评分", "b-b", score=None)
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    slot = scheduler.next_slot(index=plan_window(default_settings).index(SlotKind.high_score))
    assert slot.frontier_id == best
    assert slot.slot_kind is SlotKind.high_score
    assert "0.850000" in slot.selection_reason
    assert slot.selection_probability is None


def test_best_first_breaks_ties_by_frontier_id(storage, default_settings: Settings):
    """同分必须按入队顺序稳定排序：平局随机化会让重放轨迹对不上。"""
    first = register(storage, "上海中考同分甲", "b-a", score=0.85)
    second = register(storage, "上海中考同分乙", "b-b", score=0.85)
    rows = {row["branch_key"]: [row] for row in storage.repo.frontier_rows(storage.run_id)}
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    index = plan_window(default_settings).index(SlotKind.high_score)
    assert scheduler.next_slot(index=index, pending=rows).frontier_id == min(first, second)
    assert scheduler.next_slot(index=index, pending=rows).frontier_id != max(first, second)


def test_random_slot_uses_seeded_rng_and_records_probability(storage, default_settings: Settings):
    ids = [register(storage, f"上海中考待办{i}", "b-a", score=None) for i in range(4)]
    index = plan_window(default_settings).index(SlotKind.random_novelty)
    slot = Scheduler(storage.repo, storage.run_id, default_settings).next_slot(index=index)
    assert slot.frontier_id in ids
    assert slot.selection_probability == pytest.approx(0.25)
    assert "种子 RNG" in slot.selection_reason
    # 同一个 run 种子重放得到同一条选取序列
    replay = Scheduler(storage.repo, storage.run_id, default_settings)
    assert replay.next_slot(index=index).frontier_id == slot.frontier_id
    other_seed = Scheduler(storage.repo, storage.run_id, default_settings, rng=random.Random(7))
    assert other_seed.next_slot(index=index).as_dict()["slot_kind"] == "random_novelty"


def test_random_slot_prefers_unscored_candidates(storage, default_settings: Settings):
    scored = register(storage, "上海中考已评分", "b-a", score=0.7)
    novel = register(storage, "上海中考未评分", "b-a", score=None)
    index = plan_window(default_settings).index(SlotKind.random_novelty)
    slot = Scheduler(storage.repo, storage.run_id, default_settings, rng=random.Random(1)).next_slot(index=index)
    assert slot.frontier_id == novel and slot.frontier_id != scored
    assert slot.selection_probability == pytest.approx(1.0)


def test_empty_bucket_flows_back_and_says_which_bucket_it_borrowed(storage, default_settings: Settings):
    only = register(storage, "上海中考随机专用", "b-a", score=None, slot_kind=SlotKind.random_novelty)
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    slot = scheduler.next_slot(index=plan_window(default_settings).index(SlotKind.high_score))
    assert slot.frontier_id == only
    # 借槽执行时记录的是被借桶的类型，并把回流写进理由，否则"60/25/15"会失真
    assert slot.slot_kind is SlotKind.random_novelty
    assert "空桶回流" in slot.selection_reason and "high_score" in slot.selection_reason


def test_pending_grouping_skips_finished_rows(storage, default_settings: Settings):
    kept = register(storage, "上海中考待办", "b-a", score=0.5)
    dropped = register(storage, "上海中考作废", "b-b", score=0.5)
    storage.repo.set_frontier_status(dropped, "dropped")
    grouped = pending_by_branch(storage.repo, storage.run_id)
    assert set(grouped) == {"b-a"}
    assert [row["id"] for row in grouped["b-a"]] == [kept]
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    assert scheduler.next_slot(index=0).frontier_id == kept
    storage.repo.set_frontier_status(kept, "done")
    assert scheduler.next_slot(index=0) is None


def test_frontier_keeps_score_breakdown_for_audit(storage):
    normalized = KeywordNormalizer().normalize("上海中考志愿填报").normalized
    keyword_id = storage.repo.ensure_keyword(normalized, "上海中考志愿填报", seen_at=T0)
    storage.repo.ensure_branch(storage.run_id, "b-a")
    breakdown = {"score": 0.42, "features": {"R": {"normalized": 0.5}}, "model_version": "priority-heuristic.x"}
    storage.repo.enqueue(
        storage.run_id, keyword_id, "b-a", score=0.42, score_breakdown=breakdown,
        selection_reason="测试分解入队",
    )
    row = storage.repo.frontier_rows(storage.run_id)[0]
    stored = json.loads(row["score_breakdown_json"])
    assert stored["model_version"] == "priority-heuristic.x"
    assert stored["features"]["R"]["normalized"] == 0.5
    assert float(row["score"]) == 0.42


def test_calibration_rows_stay_visible_in_the_queue_but_are_not_picked_as_active(
    storage, default_settings: Settings
):
    """调度器读全队列（含校准样本），由引擎按冻结池单独派发；这里锁定"不会混进主动探索槽"。"""
    active = register(storage, "上海中考主动探索", "b-a", score=0.6)
    calib = register(
        storage, "上海中考校准词", "b-a", score=None,
        sample_kind=CalibrationSampleKind.coverage_calibration,
    )
    scheduler = Scheduler(storage.repo, storage.run_id, default_settings)
    slot = scheduler.next_slot(index=plan_window(default_settings).index(SlotKind.high_score))
    assert slot.sample_kind is CalibrationSampleKind.active_exploration
    assert slot.frontier_id == active
    grouped = pending_by_branch(storage.repo, storage.run_id)
    assert {row["id"] for row in grouped["b-a"]} == {active, calib}


def test_branch_stats_are_loaded_per_branch_and_isolated_by_run(storage):
    register(storage, "上海中考甲", "b-a")
    register(storage, "上海中考乙", "b-b")
    stats = load_branch_stats(storage.repo, storage.run_id)
    assert set(stats) == {"b-a", "b-b"}
    assert stats["b-a"].trials == 0 and stats["b-a"].needs_first_coverage is True
    assert load_branch_stats(storage.repo, "run-missing") == {}


def test_reward_updates_only_the_explored_branch(storage):
    register(storage, "上海中考甲", "b-a")
    register(storage, "上海中考乙", "b-b")
    stat = storage.repo.apply_reward(reward_update("b-a", clusters=1, keywords=2), at=T0)
    assert stat.trials == 1
    assert stat.total_reward == pytest.approx(1 + 0.2 * 2)
    assert stat.stale_streak == 0
    stats = load_branch_stats(storage.repo, storage.run_id)
    assert stats["b-b"].trials == 0 and stats["b-b"].total_reward == 0.0
    assert math.isinf(ucb_values(stats, coefficient=1.0)["b-b"])


def test_quiet_reward_grows_the_stale_streak_used_by_branch_stalls(storage):
    register(storage, "上海中考甲", "b-a")
    for _ in range(2):
        storage.repo.apply_reward(reward_update("b-a", keywords=0, clusters=0), at=T0)
    stat = load_branch_stats(storage.repo, storage.run_id)["b-a"]
    assert (stat.trials, stat.total_reward, stat.stale_streak) == (2, 0.0, 2)
    # 一次有产出的观察就把停滞清零：停滞必须是连续无产出
    storage.repo.apply_reward(reward_update("b-a", keywords=1), at=T0)
    assert load_branch_stats(storage.repo, storage.run_id)["b-a"].stale_streak == 0


def test_slot_order_covers_the_three_configured_kinds(default_settings: Settings):
    assert SLOT_ORDER == (SlotKind.high_score, SlotKind.branch_exploration, SlotKind.random_novelty)
    assert set(plan_window(default_settings)) == set(SLOT_ORDER)
