"""P0-19/P0-20：分项预算、采样成本与停止策略。

停止判定的反例比正例重要——"把没做完说成做完了"是最坏的失败模式，所以这里逐条钉死：
- 成功观察量不足、窗口没填满、重复率无定义、内容验证为 0、分支没覆盖全，任一条都必须拒绝收敛；
- 失败/验证码/预算耗尽只能 paused 或对应终态，终态里不许出现 converged；
- 命中某项预算只封那一个动作，其他动作继续可跑；
- 单分支停滞只暂缓该分支；
- 重复率的分母只数成功观察到的建议条数，一次建议都没有时是 None 而不是 0。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xhs_discovery.config import BudgetSection, Settings, load_settings
from xhs_discovery.discovery.cost import MIN_NORMALIZED_COST, OperationCost, judge_relevance, score_tier
from xhs_discovery.discovery.ingest import ingest_body, ingest_search, ingest_suggestion
from xhs_discovery.discovery.merge import PRELIMINARY_ANALYSIS_VERSION
from xhs_discovery.discovery.stopping import (
    ACTION_BUDGET_KEYS,
    PAUSE_FOR_CATEGORY,
    StopSignals,
    WindowMetrics,
    apply_branch_stalls,
    branch_states,
    budget_state,
    collect_signals,
    convergence_failures,
    content_validation_count,
    evaluate,
    exhausted_branches,
    stalled_branches,
    successful_observation_count,
    uncovered_branches,
    window_metrics,
)
from xhs_discovery.models.common import SessionRef, sha256_text
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    CandidateOrigin,
    ErrorCategory,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    SlotKind,
    StatementKind,
    TerminalOutcome,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.run import RewardUpdate, RunTermination
from xhs_discovery.models.source import (
    AuthorRef,
    ContentVersionRef,
    KeywordText,
    OperationContext,
    PerWordOutcome,
    PostBodyResult,
    PostCard,
    PostSearchResult,
    SuggestionItem,
    SuggestionResult,
)
from xhs_discovery.storage.ledger import BudgetExhausted, Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.normalize import KeywordNormalizer

from tests.conftest import T0

ROOT = Path(__file__).resolve().parents[1]
TINY = load_settings(ROOT / "config" / "tests-convergence-tiny.yaml")
NORMALIZER = KeywordNormalizer(locations=("上海", "北京"))
BRANCH = "b-main"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def session_for(session_id: str, collected_at: datetime) -> SessionRef:
    return SessionRef(
        session_id=session_id,
        collected_at=collected_at,
        source_name="fixture",
        source_version="0.0.1+test",
        synthetic=True,
    )


def budget_settings(**overrides) -> Settings:
    payload = {
        "suggest_attempts": 3,
        "search_attempts": 2,
        "body_attempts": 0,
        "author_attempts": 0,
        "calibration_suggest_attempts": 2,
        "max_posts_stored": 10,
        "max_runtime_minutes": 5,
    }
    payload.update(overrides)
    return TINY.model_copy(update={"budget": BudgetSection(**payload)})


@dataclass
class StopEnv:
    repo: Repository
    run_id: str
    settings: Settings
    ledger: Ledger
    keyword_id: int
    observations: int = 0

    @property
    def conn(self):
        return self.repo.conn

    def context(self, keyword_text: str, keyword_id: int, *, operation_type=OperationType.suggest, minute: int = 0) -> OperationContext:
        return OperationContext(
            run_id=self.run_id,
            keyword_id=keyword_id,
            keyword=KeywordText(raw=keyword_text, normalized=keyword_text),
            operation_type=operation_type,
            revisit_seq=self.observations,
            attempt_no=1,
        )

    def register(self, text: str, *, branch_key: str = BRANCH) -> int:
        normalized = NORMALIZER.normalize(text).normalized
        keyword_id = self.repo.ensure_keyword(normalized, text, seen_at=T0)
        self.repo.upsert_run_keyword(
            self.run_id,
            keyword_id,
            depth=1,
            origin=CandidateOrigin.observed.value,
            status="queued",
            branch_id=self.repo.branch_id(self.run_id, branch_key),
            seen_at=T0,
        )
        return keyword_id

    def suggest(
        self,
        texts: tuple[str, ...],
        *,
        parent: str = "上海中考",
        state: ResultState = ResultState.completed,
        minute: int = 0,
        sample_kind: CalibrationSampleKind | None = None,
        error: ErrorInfo | None = None,
    ) -> int:
        """写一次联想观察（可选标记为校准样本），返回 observation_id。"""
        self.observations += 1
        keyword_id = self.register(parent)
        context = self.context(parent, keyword_id, minute=minute)
        operation_id = self.repo.open_operation(
            self.run_id,
            keyword_id,
            OperationType.suggest,
            self.observations,
            branch_key=BRANCH,
            sample_kind=sample_kind,
        )
        attempt_id = self.repo.start_attempt(operation_id, 1)
        items = [SuggestionItem(text=text, rank=i + 1) for i, text in enumerate(texts)]
        per_word = []
        if state is ResultState.partial:
            per_word = [PerWordOutcome(keyword=context.keyword, state=state)]
        failure = error
        if state is ResultState.failed and failure is None:
            failure = ErrorInfo(
                code="WORD_PAGE_TIMEOUT",
                message="超时",
                category="network",
                retryable=True,
                human_action_required=False,
            )
        result = SuggestionResult(
            state=state,
            context=context,
            session=session_for(f"sess-s-{self.observations}", at(minute)),
            items=items if state in (ResultState.completed, ResultState.partial) else [],
            per_word=per_word,
            error=failure,
        )
        ingest = ingest_suggestion(
            self.repo,
            self.run_id,
            result,
            normalizer=NORMALIZER,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )
        return ingest.observation_id

    def content_validation(self, *, title: str = "上海中考名额分配怎么申请", minute: int = 0) -> None:
        """搜索卡片 + 正文观察：只有后者才算内容验证。"""
        self.observations += 1
        keyword_id = self.register("上海中考")
        note_id = f"n-{sha256_text(title)[:10]}"
        cards = [
            PostCard(
                note_id=note_id,
                access_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                title=title,
                author=AuthorRef(kind="public_ref", public_ref="u-1"),
                published_at_text="2026-09-01",
                likes_text="3",
                result_rank=1,
                search_keyword=KeywordText(raw="上海中考", normalized="上海中考"),
            )
        ]
        search_context = OperationContext(
            run_id=self.run_id,
            keyword_id=keyword_id,
            keyword=KeywordText(raw="上海中考", normalized="上海中考"),
            operation_type=OperationType.search,
            revisit_seq=self.observations,
            attempt_no=1,
        )
        search_operation = self.repo.open_operation(
            self.run_id, keyword_id, OperationType.search, self.observations, branch_key=BRANCH
        )
        search_attempt = self.repo.start_attempt(search_operation, 1)
        ingest_search(
            self.repo,
            self.run_id,
            PostSearchResult(
                state=ResultState.completed,
                context=search_context,
                session=session_for(f"sess-c-{self.observations}", at(minute)),
                items=cards,
            ),
            operation_id=search_operation,
            attempt_id=search_attempt,
        )
        body_context = search_context.model_copy(
            update={"operation_type": OperationType.body, "revisit_seq": self.observations}
        )
        body_operation = self.repo.open_operation(
            self.run_id, keyword_id, OperationType.body, self.observations, branch_key=BRANCH
        )
        body_attempt = self.repo.start_attempt(body_operation, 1)
        ingest_body(
            self.repo,
            self.run_id,
            PostBodyResult(
                state=ResultState.completed,
                context=body_context,
                session=session_for(f"sess-b-{self.observations}", at(minute)),
                body="正文内容",
                content_version=ContentVersionRef(
                    text="正文内容", text_sha256=sha256_text("正文内容")
                ),
            ),
            note_id=note_id,
            operation_id=body_operation,
            attempt_id=body_attempt,
        )

    def add_need(self, canonical: str, *, created_at: datetime) -> int:
        """直接写一条显性问题簇，用来控制"窗口内新增簇"的数量。"""
        cursor = self.conn.execute(
            "INSERT INTO needs(run_id, canonical_text, statement_kind, evidence_status,"
            " evidence_complete_json, analysis_version, extractor_version, created_at, updated_at)"
            " VALUES (?, ?, ?, 'verified', '{}', ?, 'x', ?, ?)",
            (
                self.run_id,
                canonical,
                StatementKind.explicit_problem.value,
                PRELIMINARY_ANALYSIS_VERSION,
                created_at.isoformat(),
                created_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def reward(self, *, clusters: int, evidence_ids: list[int], branch_key: str = BRANCH) -> None:
        self.repo.apply_reward(
            RewardUpdate(
                run_id=self.run_id,
                keyword_id=self.keyword_id,
                operation_type=OperationType.search,
                revisit_seq=0,
                attempt_no=1,
                attributed_branch_id=branch_key,
                new_valid_problem_clusters=clusters,
                new_related_keywords=0,
                normalized_cost=1.0,
                cluster_keyword_weight=0.2,
                result_state=ResultState.completed,
                valid_cluster_evidence_ids=evidence_ids,
                slot_kind=SlotKind.high_score,
                sample_kind=CalibrationSampleKind.active_exploration,
            ),
            at=T0,
        )

    def spend(self, key: str, units: float, *, operation_type: OperationType, operation_id: int) -> None:
        self.repo.start_attempt(operation_id, 1, at=T0)
        reservation = self.ledger.reserve(key, units, operation_id=operation_id, at=T0)
        self.ledger.settle_external(
            operation_id=operation_id,
            operation_type=operation_type,
            attempt_no=1,
            key=key,
            units=units,
            reason="test",
            reservation_id=reservation,
            at=T0,
        )

    def signals(self, *, now: datetime | None = None, actions: tuple[OperationType, ...] = tuple(OperationType)) -> StopSignals:
        return collect_signals(self.repo, self.run_id, self.settings, self.ledger, actions=actions, now=now)

    def window(self, **overrides) -> WindowMetrics:
        kwargs = dict(window=TINY.stopping.convergence_window, exclude_calibration=True)
        kwargs.update(overrides)
        return window_metrics(self.conn, self.run_id, **kwargs)


@pytest.fixture
def env(storage) -> StopEnv:
    settings = budget_settings()
    storage.repo.ensure_branch(storage.run_id, BRANCH, label="主分支")
    keyword_id = storage.repo.ensure_keyword("上海中考", "上海中考", seen_at=T0)
    storage.repo.upsert_run_keyword(
        storage.run_id,
        keyword_id,
        depth=0,
        origin=CandidateOrigin.observed.value,
        status="explored",
        branch_id=storage.repo.branch_id(storage.run_id, BRANCH),
        seen_at=T0,
    )
    return StopEnv(
        repo=storage.repo,
        run_id=storage.run_id,
        settings=settings,
        ledger=Ledger.from_settings(storage.conn, storage.run_id, settings),
        keyword_id=keyword_id,
    )


# ---------------------------------------------------------------- 窗口指标


def test_repeat_rate_is_none_when_no_suggestion_observed(env: StopEnv) -> None:
    env.suggest(())
    window = env.window()
    assert window.observations == 1
    assert window.observed_items == 0
    assert window.repeat_rate is None
    signals = env.signals()
    assert any("无定义" in reason for reason in convergence_failures(signals, env.settings))


def test_failed_observations_are_not_successful_ones(env: StopEnv) -> None:
    env.suggest(("上海中考志愿",), state=ResultState.failed, minute=1)
    env.suggest(("上海中考志愿",), minute=2)
    assert successful_observation_count(env.conn, env.run_id) == 1
    window = env.window()
    assert window.observations == 1
    assert window.observed_items == 1


def test_repeat_counts_only_items_seen_in_earlier_observations(env: StopEnv) -> None:
    env.suggest(("上海中考志愿", "上海中考建档线"), minute=1)
    env.suggest(("上海中考志愿",), minute=2)
    window = env.window()
    assert window.observed_items == 3
    assert window.repeat_items == 1
    assert window.repeat_rate == pytest.approx(round(1 / 3, 6))


def test_window_uses_only_the_latest_observations(env: StopEnv) -> None:
    for index in range(10):
        env.suggest((f"上海中考线索{index}",), minute=index + 1)
    window = env.window(window=8)
    assert window.window_observations == 8
    assert window.window_full is True
    assert window.observations == 10
    assert window.last_observed_at == at(10).isoformat()
    # 更早的 2 次观察不进窗口分母
    assert window.observed_items == 8


def test_calibration_observations_stay_out_of_the_convergence_window(env: StopEnv) -> None:
    env.suggest(("上海中考志愿",), minute=1)
    env.suggest(("上海中考分值",), minute=2, sample_kind=CalibrationSampleKind.coverage_calibration)
    active = env.window()
    assert active.observations == 1
    assert active.observed_items == 1
    everything = env.window(exclude_calibration=False)
    assert everything.observations == 2
    assert everything.observed_items == 2


def test_window_new_clusters_only_counts_the_current_analysis_version(env: StopEnv) -> None:
    env.suggest(("上海中考志愿",), minute=5)
    env.add_need("窗口内新增问题", created_at=at(6))
    env.add_need("上一个分析版本的问题", created_at=at(6))
    env.conn.execute(
        "UPDATE needs SET analysis_version = 'p2-refine.1' WHERE canonical_text = ?",
        ("上一个分析版本的问题",),
    )
    window = env.window(analysis_version=PRELIMINARY_ANALYSIS_VERSION)
    assert window.new_clusters == 1


# -------------------------------------------------------------- 内容验证


def test_cards_without_body_are_not_content_validation(env: StopEnv) -> None:
    env.suggest(("上海中考志愿",), minute=1)
    assert content_validation_count(env.conn, env.run_id) == 0


def test_each_validated_post_counts_once(env: StopEnv) -> None:
    env.content_validation(title="上海中考名额分配怎么申请", minute=1)
    env.content_validation(title="上海中考名额分配怎么申请", minute=2)
    assert content_validation_count(env.conn, env.run_id) == 1


def test_zero_content_validation_blocks_convergence(env: StopEnv) -> None:
    failures = convergence_failures(env.signals(), env.settings)
    assert any("内容验证" in reason for reason in failures)
    env.content_validation()
    assert not any("内容验证" in reason for reason in convergence_failures(env.signals(), env.settings))


# ------------------------------------------------------------ 分支停滞判定


def warm_branch(env: StopEnv, branch_key: str) -> None:
    """先产出一次（解除"未覆盖"），再连续三次无新增，把 stale_streak 推到 3。"""
    env.reward(clusters=1, evidence_ids=[1], branch_key=branch_key)
    for _ in range(3):
        env.reward(clusters=0, evidence_ids=[], branch_key=branch_key)


def test_single_branch_stall_does_not_stop_the_run(env: StopEnv) -> None:
    env.repo.ensure_branch(env.run_id, "b-side", label="旁支")
    env.repo.enqueue(
        env.run_id, env.keyword_id, "b-side", score=0.5, selection_reason="测试待办"
    )
    warm_branch(env, BRANCH)
    env.reward(clusters=0, evidence_ids=[])  # 达到 tiny 配置的 branch_stall_trials=4
    states = branch_states(env.repo, env.run_id)
    assert stalled_branches(states, stall_trials=2) == (BRANCH,)
    assert uncovered_branches(states) == ("b-side",)
    signals = env.signals()
    assert signals.stalled == (BRANCH,)
    assert signals.window is not None
    # 一个分支停滞 + 另一个分支还有待办：继续跑，不判全局停止
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger, error=None)
    assert decision.status is RunStatus.running
    assert any("未覆盖一次" in reason for reason in convergence_failures(signals, env.settings))


def test_stalled_branch_is_suspended_once_per_streak(env: StopEnv) -> None:
    warm_branch(env, BRANCH)
    signals = env.signals()
    assert apply_branch_stalls(env.repo, env.run_id, env.settings, signals) == ()
    # branch_stall_trials=4（tiny 配置），先把停滞拉到阈值
    for _ in range(3):
        env.reward(clusters=0, evidence_ids=[])
    signals = env.signals()
    suspended = apply_branch_stalls(env.repo, env.run_id, env.settings, signals)
    assert suspended == (BRANCH,)
    assert apply_branch_stalls(env.repo, env.run_id, env.settings, signals) == ()
    events = env.repo.events(env.run_id, kind="branch_suspended")
    assert len(events) == 1
    assert json.loads(events[0]["detail_json"])["branch_key"] == BRANCH


def test_empty_queue_does_not_count_as_a_stall(env: StopEnv) -> None:
    env.reward(clusters=1, evidence_ids=[1])
    states = branch_states(env.repo, env.run_id)
    assert stalled_branches(states, stall_trials=1) == ()
    assert exhausted_branches(states) == (BRANCH,)


# ------------------------------------------------------------------ 预算


def test_hitting_one_budget_only_blocks_that_action(env: StopEnv) -> None:
    operation_id = env.repo.open_operation(
        env.run_id, env.keyword_id, OperationType.suggest, 1, branch_key=BRANCH
    )
    env.spend("suggest_attempts", 3, operation_type=OperationType.suggest, operation_id=operation_id)
    state = budget_state(env.repo, env.run_id, env.ledger)
    assert "suggest_attempts" in state.exhausted_keys
    assert OperationType.suggest in state.blocked_actions
    assert OperationType.search in state.runnable_actions
    # body/author 预算为 0：从一开始就不可执行，但不影响 suggest/search
    assert OperationType.body in state.blocked_actions
    assert env.ledger.can_afford("calibration_suggest_attempts") is True


def test_calibration_budget_is_independent_from_exploration(env: StopEnv) -> None:
    operation_id = env.repo.open_operation(
        env.run_id, env.keyword_id, OperationType.suggest, 1, branch_key=BRANCH
    )
    env.spend("calibration_suggest_attempts", 2, operation_type=OperationType.suggest, operation_id=operation_id)
    state = budget_state(env.repo, env.run_id, env.ledger)
    assert "calibration_suggest_attempts" in state.exhausted_keys
    assert env.ledger.can_afford("suggest_attempts") is True
    assert OperationType.suggest in state.runnable_actions


def test_runtime_limit_is_reported_without_spending_actions(env: StopEnv) -> None:
    env.repo.set_run_status(env.run_id, RunTermination(status=RunStatus.running, note="开工"), at=T0)
    early = budget_state(env.repo, env.run_id, env.ledger, now=at(2))
    late = budget_state(env.repo, env.run_id, env.ledger, now=at(6))
    assert early.runtime_limit_hit is False
    assert late.runtime_limit_hit is True


def test_over_spend_is_rejected_before_any_ledger_row(env: StopEnv) -> None:
    operation_id = env.repo.open_operation(
        env.run_id, env.keyword_id, OperationType.search, 1, branch_key=BRANCH
    )
    env.ledger.reserve("search_attempts", 2, operation_id=operation_id, at=T0)
    with pytest.raises(BudgetExhausted):
        env.ledger.reserve("search_attempts", 1, operation_id=operation_id, at=T0)
    assert env.ledger.used("search_attempts") == 0


def test_body_budget_zero_cannot_be_reserved(env: StopEnv) -> None:
    operation_id = env.repo.open_operation(
        env.run_id, env.keyword_id, OperationType.body, 1, branch_key=BRANCH
    )
    with pytest.raises(BudgetExhausted):
        env.ledger.reserve("body_attempts", 1, operation_id=operation_id, at=T0)


# ------------------------------------------------------------------ 终态


def exhausted_env(env: StopEnv) -> StopEnv:
    for operation_type, key in ACTION_BUDGET_KEYS.items():
        limit = int(env.settings.budget.model_dump()[key])
        if limit <= 0:
            continue
        operation_id = env.repo.open_operation(
            env.run_id, env.keyword_id, operation_type, 900, branch_key=BRANCH
        )
        env.spend(key, limit, operation_type=operation_type, operation_id=operation_id)
    return env


def test_all_actions_blocked_and_empty_queue_is_budget_exhausted(env: StopEnv) -> None:
    exhausted_env(env)
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger)
    assert decision.status is RunStatus.completed
    assert decision.outcome is TerminalOutcome.budget_exhausted
    assert decision.signals.pending_items == 0
    assert "未满足的收敛条件" in decision.note
    assert decision.as_termination().outcome == TerminalOutcome.budget_exhausted.value


def test_empty_queue_with_budget_left_is_queue_exhausted(env: StopEnv) -> None:
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger)
    assert decision.outcome is TerminalOutcome.queue_exhausted
    assert decision.status is RunStatus.completed
    assert "队列耗尽" in decision.note


def test_pending_work_without_budget_pauses_for_more_budget(env: StopEnv) -> None:
    child = env.register("上海中考中职")
    env.repo.enqueue(env.run_id, child, BRANCH, score=0.4, selection_reason="测试待办")
    exhausted_env(env)
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger)
    assert decision.status is RunStatus.paused
    assert decision.pause_reason is PauseReason.fault
    assert decision.outcome is None
    assert "追加预算后可恢复" in decision.note


def test_convergence_needs_every_condition(env: StopEnv) -> None:
    env.content_validation(minute=0)
    for index in range(8):
        env.suggest(("上海中考志愿",), minute=index + 1)
    env.reward(clusters=0, evidence_ids=[])
    for _ in range(3):
        env.reward(clusters=0, evidence_ids=[])
    frontier_id = env.repo.enqueue(
        env.run_id, env.keyword_id, BRANCH, score=0.1, selection_reason="待办"
    )
    env.repo.set_frontier_status(frontier_id, "done")
    signals = env.signals()
    assert convergence_failures(signals, env.settings) == [], convergence_failures(signals, env.settings)
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger)
    assert decision.status is RunStatus.completed
    assert decision.outcome is TerminalOutcome.converged
    assert "收敛" in decision.note


def test_one_missing_condition_breaks_convergence(env: StopEnv) -> None:
    env.content_validation(minute=0)
    for index in range(7):  # 少一次成功观察：8 是 tiny 配置的下限
        env.suggest(("上海中考志愿",), minute=index + 1)
    for _ in range(4):
        env.reward(clusters=0, evidence_ids=[])
    failures = convergence_failures(env.signals(), env.settings)
    assert any("不足最低" in reason for reason in failures)
    assert evaluate(env.repo, env.run_id, env.settings, env.ledger).outcome is not TerminalOutcome.converged


def test_new_clusters_in_window_break_convergence(env: StopEnv) -> None:
    env.content_validation(minute=0)
    for index in range(8):
        env.suggest(("上海中考志愿",), minute=index + 1)
        env.add_need(f"新增问题{index}", created_at=at(index + 1))
    for _ in range(4):
        env.reward(clusters=0, evidence_ids=[])
    failures = convergence_failures(env.signals(), env.settings)
    assert any("新增有效问题簇" in reason for reason in failures)


def test_low_repeat_rate_breaks_convergence(env: StopEnv) -> None:
    env.content_validation(minute=0)
    for index in range(8):
        env.suggest((f"全新线索{index}",), minute=index + 1)
    for _ in range(4):
        env.reward(clusters=0, evidence_ids=[])
    signals = env.signals()
    assert signals.window.repeat_rate == 0.0
    assert any("重复率" in reason for reason in convergence_failures(signals, env.settings))


@pytest.mark.parametrize(
    ("category", "reason"),
    [
        (ErrorCategory.auth, PauseReason.auth_required),
        (ErrorCategory.action_required, PauseReason.action_required),
        (ErrorCategory.profile_busy, PauseReason.profile_busy),
        (ErrorCategory.network, PauseReason.network),
        (ErrorCategory.page, PauseReason.selector_mismatch),
        (ErrorCategory.output, PauseReason.fault),
    ],
)
def test_errors_pause_with_mapped_reason(env: StopEnv, category: ErrorCategory, reason: PauseReason) -> None:
    assert PAUSE_FOR_CATEGORY[category] is reason
    error = ErrorInfo(
        code="X", message="boom", category=category.value, retryable=False, human_action_required=True
    )
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger, error=error)
    assert decision.status is RunStatus.paused
    assert decision.pause_reason is reason
    assert decision.outcome is not TerminalOutcome.converged
    assert decision.outcome is None


def test_cancelled_error_is_a_terminal_cancelled(env: StopEnv) -> None:
    error = ErrorInfo(code="CANCELLED", message="取消", category="cancelled", retryable=False, human_action_required=False)
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger, error=error)
    assert decision.status is RunStatus.completed
    assert decision.outcome is TerminalOutcome.cancelled


def test_budget_category_error_never_masquerades_as_convergence(env: StopEnv) -> None:
    error = ErrorInfo(
        code="BUDGET", message="预算不足", category="budget", retryable=False, human_action_required=False
    )
    decision = evaluate(env.repo, env.run_id, env.settings, env.ledger, error=error)
    assert decision.status is RunStatus.paused
    assert "不得记为收敛" in decision.note


def test_signals_serialise_for_the_report(env: StopEnv) -> None:
    env.suggest(("上海中考志愿",), minute=1)
    payload = env.signals().as_dict()
    assert set(payload) == {
        "window",
        "budget",
        "stalled_branches",
        "exhausted_branches",
        "uncovered_branches",
        "content_validations",
        "pending_items",
    }
    assert payload["window"]["successful_observations"] == 1
    assert payload["window"]["repeat_rate"] == 0.0
    assert json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- P0-19 成本


def test_retry_and_posts_raise_the_normalized_cost() -> None:
    assert OperationCost(OperationType.suggest, attempts=1).normalized == 1.0
    assert OperationCost(OperationType.suggest, attempts=2).normalized == 2.0
    assert OperationCost(OperationType.search, attempts=1, posts_stored=10).normalized == 1.5
    assert OperationCost(OperationType.search, attempts=0).normalized == MIN_NORMALIZED_COST


def test_sampling_tier_uses_the_configured_thresholds(env: StopEnv) -> None:
    sampling = env.settings.sampling
    assert score_tier(None, env.settings) == ("low", sampling.low_value_posts)
    assert score_tier(0.9, env.settings) == ("high", sampling.high_value_posts)
    assert score_tier(0.4, env.settings) == ("medium", sampling.medium_value_posts)
    assert score_tier(0.1, env.settings) == ("low", sampling.low_value_posts)


def test_relevance_is_judged_conservatively() -> None:
    judgment = judge_relevance(
        "上海中考中职班怎么选", anchors=("上海中考",), domain_terms=("志愿",), normalizer=NORMALIZER
    )
    assert judgment.related is True
    off_topic = judge_relevance(
        "跨境电商独立站搭建", anchors=("上海中考",), domain_terms=("志愿",), normalizer=NORMALIZER
    )
    assert off_topic.related is False
    assert off_topic.status == "irrelevant"
    lexical = judge_relevance(
        "志愿填报时间", anchors=("北京高考",), domain_terms=("志愿",), normalizer=NORMALIZER
    )
    assert lexical.related is True and "领域词典" in lexical.reason
