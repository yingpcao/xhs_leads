"""P0-04：运行/预算/奖励/控制契约（P0-B、P0-C 的实现输入）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from xhs_discovery.models.enums import (
    ControlActionType,
    ErrorCategory,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    SlotKind,
    TerminalOutcome,
)
from xhs_discovery.models.errors import error_from_exit_code
from xhs_discovery.models.run import (
    BranchStat,
    BudgetLedgerEntry,
    ControlRequest,
    RewardUpdate,
    RunIdentity,
    RunTermination,
)
from xhs_discovery.models.source import KeywordText

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
HASH = "0" * 64


def run_identity(**overrides) -> RunIdentity:
    payload = dict(
        run_id="run-a",
        seed=KeywordText(raw="上海中考", normalized="上海中考"),
        mode="fixture",
        config_hash=HASH,
        random_seed=20261024,
        created_at=NOW,
        synthetic_fixture=True,
    )
    payload.update(overrides)
    return RunIdentity(**payload)


def test_fixture_run_must_be_marked_synthetic():
    assert run_identity().synthetic_fixture is True
    with pytest.raises(ValidationError, match="必须标记 synthetic"):
        run_identity(synthetic_fixture=False)
    with pytest.raises(ValidationError, match="mode 只能是"):
        run_identity(mode="dream")
    with pytest.raises(ValidationError, match="必须带时区"):
        run_identity(created_at=datetime(2026, 10, 24, 12, 0, 0))


def test_live_run_identity_is_allowed_but_still_records_mode():
    assert run_identity(mode="live", synthetic_fixture=False).mode == "live"


def test_external_access_budget_event_requires_attempt_and_consumes_units():
    entry = BudgetLedgerEntry(
        run_id="run-a",
        budget_key="suggest_attempts",
        operation_type=OperationType.suggest,
        attempt_no=2,
        consumed_units=1,
        external_access=True,
        reason="失败重试同样占预算",
    )
    assert entry.external_access is True
    with pytest.raises(ValidationError, match="operation_type 与 attempt_no"):
        BudgetLedgerEntry(
            run_id="run-a", budget_key="suggest_attempts", consumed_units=1,
            external_access=True, reason="缺 attempt",
        )
    with pytest.raises(ValidationError, match="至少消耗 1"):
        BudgetLedgerEntry(
            run_id="run-a", budget_key="suggest_attempts", operation_type=OperationType.suggest,
            attempt_no=1, consumed_units=0, external_access=True, reason="零消耗",
        )


def test_cache_hit_does_not_consume_external_budget_but_is_counted():
    cache = BudgetLedgerEntry(
        run_id="run-a", budget_key="suggest_attempts", consumed_units=0,
        external_access=False, reason="cache_hit",
    )
    assert cache.consumed_units == 0
    with pytest.raises(ValidationError, match="缓存读取不占外部访问预算"):
        BudgetLedgerEntry(
            run_id="run-a", budget_key="suggest_attempts", consumed_units=1,
            external_access=False, reason="错误地把缓存算作外部访问",
        )


def test_new_branch_requires_first_coverage_and_zero_reward():
    fresh = BranchStat(run_id="run-a", branch_id="b1", trials=0, total_reward=0.0, stale_streak=0)
    assert fresh.needs_first_coverage is True
    assert fresh.mean_reward == 0.0
    with pytest.raises(ValidationError, match="累计奖励必须为 0"):
        BranchStat(run_id="run-a", branch_id="b1", trials=0, total_reward=1.0, stale_streak=0)
    used = BranchStat(run_id="run-a", branch_id="b1", trials=4, total_reward=2.0, stale_streak=1)
    assert used.mean_reward == 0.5
    assert used.needs_first_coverage is False
    assert used.display_label is None


def reward(**overrides) -> RewardUpdate:
    payload = dict(
        run_id="run-a",
        keyword_id=3,
        operation_type=OperationType.search,
        revisit_seq=0,
        attempt_no=1,
        attributed_branch_id="b1",
        new_valid_problem_clusters=2,
        new_related_keywords=3,
        normalized_cost=1.5,
        cluster_keyword_weight=0.2,
        result_state=ResultState.completed,
        valid_cluster_evidence_ids=[11, 12],
        slot_kind=SlotKind.high_score,
    )
    payload.update(overrides)
    return RewardUpdate(**payload)


def test_reward_formula_and_single_branch_attribution():
    r = reward()
    assert r.reward == pytest.approx((2 + 0.2 * 3) / 1.5)
    assert r.attributed_branch_id == "b1"
    with pytest.raises(ValidationError):
        reward(attributed_branch_id="")


def test_valid_problem_cluster_without_evidence_earns_nothing():
    with pytest.raises(ValidationError, match="可定位的显性问题证据"):
        reward(valid_cluster_evidence_ids=[])


def test_failed_operation_cannot_credit_new_demand():
    with pytest.raises(ValidationError, match="失败操作不得记新增奖励"):
        reward(result_state=ResultState.failed)
    failed = reward(
        result_state=ResultState.failed,
        new_valid_problem_clusters=0,
        new_related_keywords=0,
        valid_cluster_evidence_ids=[],
    )
    assert failed.reward == 0.0


def test_suggest_only_operation_gets_keyword_reward_but_no_cluster_credit():
    with pytest.raises(ValidationError, match="只做联想的操作只获得关键词奖励"):
        reward(operation_type=OperationType.suggest)
    suggest = reward(
        operation_type=OperationType.suggest,
        new_valid_problem_clusters=0,
        valid_cluster_evidence_ids=[],
    )
    assert suggest.reward == pytest.approx((0 + 0.2 * 3) / 1.5)


def test_control_request_extend_adds_quota_instead_of_resetting_total():
    extend = ControlRequest(
        run_id="run-a", action=ControlActionType.extend, suggest_budget_delta=50, note="追加 50 次联想"
    )
    assert extend.suggest_budget_delta == 50
    assert extend.applied_at is None
    with pytest.raises(ValidationError, match="追加额度"):
        ControlRequest(run_id="run-a", action=ControlActionType.extend)
    with pytest.raises(ValidationError, match="只有 extend 可以携带预算增量"):
        ControlRequest(run_id="run-a", action=ControlActionType.pause, search_budget_delta=10)
    with pytest.raises(ValidationError):
        ControlRequest(run_id="run-a", action=ControlActionType.extend, suggest_budget_delta=0)


def test_pause_needs_a_reason_and_convergence_needs_its_trigger():
    assert RunTermination(status=RunStatus.paused, pause_reason=PauseReason.user).pause_reason.value == "user"
    with pytest.raises(ValidationError, match="必须记录原因"):
        RunTermination(status=RunStatus.paused)
    with pytest.raises(ValidationError, match="completed 必须区分"):
        RunTermination(status=RunStatus.completed)
    with pytest.raises(ValidationError, match="进行中状态不得携带终态原因"):
        RunTermination(status=RunStatus.running, outcome=TerminalOutcome.converged, note="x")
    with pytest.raises(ValidationError, match="converged 必须写明"):
        RunTermination(status=RunStatus.completed, outcome=TerminalOutcome.converged)
    ok = RunTermination(
        status=RunStatus.completed,
        outcome=TerminalOutcome.converged,
        note="最近 6 次观察新增问题簇 1 且重复率 0.83",
    )
    assert ok.outcome is TerminalOutcome.converged
    budget = RunTermination(status=RunStatus.completed, outcome=TerminalOutcome.budget_exhausted)
    assert budget.outcome is TerminalOutcome.budget_exhausted


def test_budget_exhausted_is_never_reported_as_convergence():
    outcomes = {t for t in TerminalOutcome}
    assert outcomes >= {"budget_exhausted", "converged", "queue_exhausted", "no_actions_available"}
    term = RunTermination(status=RunStatus.completed, outcome=TerminalOutcome.budget_exhausted, note="联想预算耗尽")
    assert term.outcome is not TerminalOutcome.converged
    assert term.pause_reason is None


def test_error_classification_drives_pause_not_retry():
    for code, expected_category in ((3, ErrorCategory.auth), (4, ErrorCategory.action_required)):
        err = error_from_exit_code(code, "人工介入")
        assert err.category is expected_category
        assert err.retryable is False
        assert err.human_action_required is True
