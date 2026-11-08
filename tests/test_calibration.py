"""P0-21：覆盖校准的冻结池、独立预算与"池内相对覆盖"。

这块的意义是给主动探索一个外部参照，因此要钉死的是它不能被策略自己定义：
- 池一旦冻结就不可改写：之后新发现的词不进池，重放同一配置得到同一个 pool_id；
- 预算单列 `calibration_suggest_attempts`，与主动探索的 `suggest_attempts` 互不占用；
- 校准观察只认 completed/partial 为"已采样"，失败重试仍留在待办；
- 覆盖比例的分母只有池内成员，且必须带着"不是平台总体覆盖率"的范围声明；
- 校准观察不进 frontier、不进收敛窗口（见 test_stopping.py）。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xhs_discovery.config import BudgetSection, Settings, load_settings
from xhs_discovery.discovery.calibration import (
    CALIBRATION_BUDGET_KEY,
    POOL_EVENT,
    SCOPE_NOTE,
    _candidate_rows,
    calibration_operation_type,
    coverage,
    freeze_pool,
    load_pool,
    next_sample,
    pending_samples,
    plan_next,
    sampled_keyword_ids,
)
from xhs_discovery.discovery.ingest import ingest_suggestion
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.enums import (
    CandidateOrigin,
    CalibrationSampleKind,
    OperationType,
    ResultState,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.source import (
    KeywordText,
    OperationContext,
    SuggestionItem,
    SuggestionResult,
)
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.ledger import BudgetExhausted, Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.normalize import KeywordNormalizer

from tests.conftest import T0, build_run_identity

NORMALIZER = KeywordNormalizer(locations=("上海", "北京"))
BRANCH = "b-main"
ROOT = Path(__file__).resolve().parents[1]
POOL_TEXTS = ("上海中考志愿", "上海中考建档线", "上海中考名额分配", "上海中考中职", "上海中考复读")


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def settings_with(**budget) -> Settings:
    payload = dict(
        suggest_attempts=10,
        search_attempts=2,
        body_attempts=0,
        author_attempts=0,
        calibration_suggest_attempts=3,
        max_posts_stored=10,
        max_runtime_minutes=5,
    )
    payload.update(budget)
    base = load_settings(ROOT / "config" / "default.yaml")
    return base.model_copy(update={"budget": BudgetSection(**payload)})


@dataclass
class CalEnv:
    repo: Repository
    run_id: str
    settings: Settings
    ledger: Ledger
    seed_keyword_id: int
    pool_keyword_ids: tuple[int, ...]
    observations: int = 0

    @property
    def conn(self):
        return self.repo.conn

    def register(self, text: str, *, depth: int = 1, status: str = "queued") -> int:
        normalized = NORMALIZER.normalize(text).normalized
        keyword_id = self.repo.ensure_keyword(normalized, text, seen_at=T0)
        self.repo.upsert_run_keyword(
            self.run_id,
            keyword_id,
            depth=depth,
            origin=CandidateOrigin.observed.value,
            status=status,
            branch_id=self.repo.branch_id(self.run_id, BRANCH),
            seen_at=T0,
        )
        return keyword_id

    def observe(
        self,
        *,
        parent_id: int,
        parent_text: str = "上海中考",
        child_texts: tuple[str, ...],
        calibration: bool = False,
        minute: int = 1,
        state: ResultState = ResultState.completed,
    ) -> int:
        """写一次联想观察；`calibration=True` 时标成 coverage_calibration 样本。"""
        self.observations += 1
        operation_type = calibration_operation_type()
        operation_id = self.repo.open_operation(
            self.run_id,
            parent_id,
            operation_type,
            self.observations,
            branch_key=BRANCH,
            sample_kind=(
                CalibrationSampleKind.coverage_calibration if calibration else None
            ),
        )
        attempt_id = self.repo.start_attempt(operation_id, 1)
        context = OperationContext(
            run_id=self.run_id,
            keyword_id=parent_id,
            keyword=KeywordText(raw=parent_text, normalized=NORMALIZER.normalize(parent_text).normalized),
            operation_type=operation_type,
            revisit_seq=self.observations,
            attempt_no=1,
        )
        items = [SuggestionItem(text=text, rank=i + 1) for i, text in enumerate(child_texts)]
        result = SuggestionResult(
            state=state,
            context=context,
            session=SessionRef(
                session_id=f"sess-{self.observations}",
                collected_at=at(minute),
                source_name="fixture",
                source_version="0.0.1+test",
                synthetic=True,
            ),
            items=items if state in (ResultState.completed, ResultState.partial) else [],
            error=(
                None
                if state is not ResultState.failed
                else (
                    ErrorInfo(
                        code="WORD_PAGE_TIMEOUT",
                        message="超时",
                        category="network",
                        retryable=True,
                        human_action_required=False,
                    )
                )
            ),
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

    def spend(self, key: str, units: float, *, keyword_id: int) -> None:
        operation_id = self.repo.open_operation(
            self.run_id, keyword_id, OperationType.suggest, 500 + units, branch_key=BRANCH
        )
        self.repo.start_attempt(operation_id, 1)
        reservation = self.ledger.reserve(key, units, operation_id=operation_id, at=T0)
        self.ledger.settle_external(
            operation_id=operation_id,
            operation_type=OperationType.suggest,
            attempt_no=1,
            key=key,
            units=units,
            reason="校准采样",
            reservation_id=reservation,
            at=T0,
        )


@pytest.fixture
def env(storage) -> CalEnv:
    settings = settings_with()
    storage.repo.ensure_branch(storage.run_id, BRANCH, label="主分支")
    seed_id = storage.repo.ensure_keyword("上海中考", "上海中考", seen_at=T0)
    storage.repo.upsert_run_keyword(
        storage.run_id,
        seed_id,
        depth=0,
        origin=CandidateOrigin.observed.value,
        status="explored",
        branch_id=storage.repo.branch_id(storage.run_id, BRANCH),
        seen_at=T0,
    )
    pool_ids = tuple(env_register(storage.repo, storage.run_id, text) for text in POOL_TEXTS)
    return CalEnv(
        repo=storage.repo,
        run_id=storage.run_id,
        settings=settings,
        ledger=Ledger.from_settings(storage.conn, storage.run_id, settings),
        seed_keyword_id=seed_id,
        pool_keyword_ids=pool_ids,
    )


def env_register(repo: Repository, run_id: str, text: str) -> int:
    normalized = NORMALIZER.normalize(text).normalized
    keyword_id = repo.ensure_keyword(normalized, text, seen_at=T0)
    repo.upsert_run_keyword(
        run_id,
        keyword_id,
        depth=1,
        origin=CandidateOrigin.observed.value,
        status="queued",
        branch_id=repo.branch_id(run_id, BRANCH),
        seen_at=T0,
    )
    return keyword_id


def mirror_run(env: CalEnv, run_id: str) -> str:
    """复制一份候选集完全相同的 run，用来比较不同种子/成员筛选的行为。"""
    env.repo.create_run(build_run_identity(run_id), config_json='{"budget": {}}')
    env.repo.ensure_branch(run_id, BRANCH, label="主分支")
    env_register(env.repo, run_id, "上海中考")
    for text in POOL_TEXTS:
        env_register(env.repo, run_id, text)
    return run_id


# ------------------------------------------------------------------ 冻结与恢复


def test_freeze_draws_a_stable_subset_from_the_seed(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=3, at=T0)
    assert pool.size == 3
    assert pool.seed == env.settings.run.random_seed
    assert pool.requested_size == 3
    assert pool.pool_id.startswith("cp-")
    assert [member.keyword_id for member in pool.members] == sorted(
        member.keyword_id for member in pool.members
    )
    assert set(pool.members) <= set(_candidate_rows(env.repo, env.run_id))


def test_freeze_clamps_to_available_candidates(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=99, at=T0)
    assert pool.requested_size == 99
    assert pool.size == len(POOL_TEXTS) + 1  # 含种子词自己


def test_seed_decides_the_sample_and_is_replayable(env: CalEnv, storage) -> None:
    first = freeze_pool(env.repo, env.run_id, env.settings, size=3, seed=7, at=T0)
    repo2 = Repository(storage_db.connect(storage.paths.db_path))
    second = freeze_pool(repo2, env.run_id, env.settings, size=3, seed=7, at=T0)
    assert first.pool_id == second.pool_id
    assert [m.as_dict() for m in first.members] == [m.as_dict() for m in second.members]
    repo2.conn.close()


def test_seed_decides_which_candidates_are_drawn(env: CalEnv) -> None:
    """抽样顺序只由种子决定：同一候选集 + 同一种子 = 同一个成员集合。"""
    for seed in (1, 99):
        run_id = f"run-s{seed}"
        mirror_run(env, run_id)
        pool = freeze_pool(env.repo, run_id, env.settings, size=2, seed=seed, at=T0)
        expected = {
            member.keyword_id
            for member in random.Random(seed).sample(_candidate_rows(env.repo, run_id), k=2)
        }
        assert {member.keyword_id for member in pool.members} == expected


def test_frozen_pool_does_not_absorb_later_discoveries(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=3, at=T0)
    late_id = env.register("上海中考国际部的学校")
    again = freeze_pool(env.repo, env.run_id, env.settings, size=5, at=T0 + timedelta(minutes=3))
    assert again.pool_id == pool.pool_id
    assert again.size == pool.size
    assert late_id not in {member.keyword_id for member in again.members}
    assert len(env.repo.events(env.run_id, kind=POOL_EVENT)) == 1


def test_pool_reloads_from_the_event_after_a_restart(env: CalEnv, storage) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=3, at=T0)
    fresh = Repository(storage_db.connect(storage.paths.db_path))
    loaded = load_pool(fresh, env.run_id)
    assert loaded is not None
    assert loaded == pool
    fresh.conn.close()


def test_freeze_rejects_missing_budget_or_candidates(env: CalEnv) -> None:
    with pytest.raises(ValueError, match="没有校准预算"):
        freeze_pool(env.repo, env.run_id, env.settings, size=0)
    other = build_run_identity("run-empty")
    env.repo.create_run(other, config_json='{"budget": {}}')
    with pytest.raises(ValueError, match="还没有可冻结的候选词"):
        freeze_pool(env.repo, "run-empty", env.settings, at=T0)


def test_event_payload_is_the_whole_pool(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=2, at=T0)
    events = env.repo.events(env.run_id, kind=POOL_EVENT)
    detail = json.loads(events[0]["detail_json"])
    assert detail["pool_id"] == pool.pool_id
    assert detail["member_count"] == 2
    assert detail["seed"] == pool.seed
    assert len(detail["members"]) == 2


# -------------------------------------------------------------------- 抽样


def test_only_completed_samples_count_as_sampled(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=2, at=T0)
    member = pool.members[0]
    env.observe(parent_id=member.keyword_id, child_texts=(), state=ResultState.failed, minute=1)
    assert sampled_keyword_ids(env.conn, env.run_id) == set()
    assert plan_next(env.repo, env.run_id, pool, env.ledger) == member
    env.observe(
        parent_id=member.keyword_id, child_texts=("上海中考加分",), calibration=True, minute=2
    )
    assert sampled_keyword_ids(env.conn, env.run_id) == {member.keyword_id}
    assert plan_next(env.repo, env.run_id, pool, env.ledger) == pool.members[1]


def test_active_exploration_observations_do_not_mark_a_member_sampled(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=1, at=T0)
    member = pool.members[0]
    env.observe(parent_id=member.keyword_id, child_texts=("上海中考加分",), minute=1)
    assert sampled_keyword_ids(env.conn, env.run_id) == set()


def test_pending_queue_walks_the_pool_and_stops_when_done(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=2, at=T0)
    done: set[int] = set()
    assert next_sample(pool, done=done) == pool.members[0]
    done.add(pool.members[0].keyword_id)
    assert next_sample(pool, done=done) == pool.members[1]
    done.add(pool.members[1].keyword_id)
    assert next_sample(pool, done=done) is None
    assert pending_samples(env.repo, env.run_id, pool, env.ledger) == pool.members


def test_calibration_budget_gates_the_pending_samples(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=2, at=T0)
    env.spend(CALIBRATION_BUDGET_KEY, 3, keyword_id=env.seed_keyword_id)
    assert pending_samples(env.repo, env.run_id, pool, env.ledger) == ()
    assert env.ledger.can_afford("suggest_attempts") is True


def test_calibration_cannot_borrow_the_exploration_budget(env: CalEnv) -> None:
    with pytest.raises(BudgetExhausted):
        env.spend("calibration_suggest_attempts", 4, keyword_id=env.seed_keyword_id)
    assert env.ledger.used(CALIBRATION_BUDGET_KEY) == 0


def test_pool_members_do_not_enter_the_frontier(env: CalEnv) -> None:
    freeze_pool(env.repo, env.run_id, env.settings, size=3, at=T0)
    assert env.repo.frontier_rows(env.run_id, status="pending") == []


# -------------------------------------------------------------------- 覆盖


def test_coverage_ratios_only_describe_the_pool(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=2, at=T0)
    first, second = pool.members
    # 主动探索召回了 first（一次），采样完成 second
    env.observe(parent_id=env.seed_keyword_id, child_texts=(first.normalized,), minute=1)
    env.observe(parent_id=second.keyword_id, child_texts=(), calibration=True, minute=2)
    report = coverage(env.repo, env.run_id, pool)
    assert report.pool_size == 2
    assert report.sampled == 1
    assert report.discovered_by_exploration == 1
    assert report.sample_share == 0.5
    assert report.discovery_share == 0.5
    assert report.platform_return_share == 0.0
    assert report.scope_note == SCOPE_NOTE
    by_id = {member["keyword_id"]: member for member in report.members}
    assert by_id[first.keyword_id]["observed_by_exploration_times"] == 1
    assert by_id[first.keyword_id]["sampled"] is False
    assert by_id[second.keyword_id]["sampled"] is True
    assert by_id[second.keyword_id]["observed_by_exploration_times"] == 0
    assert "不能宣称" in report.scope_note


def test_exploration_and_calibration_observations_are_not_mixed(env: CalEnv) -> None:
    """校准观察里平台把词自己推荐回来，记为可见性证据，但不算"主动探索发现过"。"""
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=1, at=T0)
    member = pool.members[0]
    env.observe(
        parent_id=member.keyword_id,
        child_texts=(member.normalized,),
        calibration=True,
        minute=1,
    )
    report = coverage(env.repo, env.run_id, pool)
    assert report.platform_returned_self == 1
    assert report.discovered_by_exploration == 0
    entry = report.members[0]
    assert entry["platform_returned_self"] is True
    assert entry["observed_by_exploration_times"] == 0
    assert entry["best_rank_by_exploration"] is None


def test_coverage_of_a_pool_that_was_never_touched_reports_zero_shares(env: CalEnv) -> None:
    pool = freeze_pool(env.repo, env.run_id, env.settings, size=2, at=T0)
    report = coverage(env.repo, env.run_id, pool)
    assert report.sampled == 0
    assert report.sample_share == 0.0
    assert report.platform_return_share == 0.0
    assert report.members[0]["sampled"] is False


def mark_irrelevant(env: CalEnv, run_id: str, keyword_id: int) -> None:
    env.repo.upsert_run_keyword(
        run_id, keyword_id, status="irrelevant", irrelevant_reason="与锚词无重叠（明显偏题）", seen_at=T0
    )


def test_irrelevant_candidates_are_left_out_of_a_new_pool(env: CalEnv) -> None:
    """冻结时已判偏题的词不进池；但冻结之后判偏题的词仍在池里（池不可改写）。"""
    run_id = mirror_run(env, "run-c")
    keep = env_register(env.repo, run_id, "上海中考体考")
    drop = env_register(env.repo, run_id, "上海中考舞蹈")
    mark_irrelevant(env, run_id, drop)
    pool = freeze_pool(env.repo, run_id, env.settings, size=99, at=T0)
    member_ids = {member.keyword_id for member in pool.members}
    assert keep in member_ids
    assert drop not in member_ids

    frozen = freeze_pool(env.repo, env.run_id, env.settings, size=99, at=T0)
    target = frozen.members[0].keyword_id
    mark_irrelevant(env, env.run_id, target)
    assert target in {m.keyword_id for m in load_pool(env.repo, env.run_id).members}
