"""P0-22 worker 主循环自测：一次槽位的记账顺序、所有权、重试计费、重放与暂停恢复。

盯的都是开发计划对 P0-C 的硬要求：

    预留预算 → 开 attempt → 访问 → 落原始产物 → 幂等入库 → 结算（显式 reservation_id）
    → 规则归并 → 本次操作奖励 → 算子扩展 → 候选入队

以及几条纪律：重启用同一份 run 配置重建 Repository/Ledger/WorkerControl（额度只认库里的账）、
每次外部访问前检查锁所有权、`run_guarded()` 的 `finally` 一定放锁、失败重试是新 attempt 新预留
新结算且进成本分母、已有可靠观察的重放不再访问平台、异常与预算未达不得记成收敛。

全程 synthetic fixture：不访问真实平台、不读凭据、不接模型与外部 API。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from xhs_discovery.config import Settings, load_settings
from xhs_discovery.discovery import calibration as calib
from xhs_discovery.discovery import stopping
from xhs_discovery.discovery.engine import (
    CALIBRATION_SEQ_BASE,
    REPLAY_NOTE,
    DiscoveryEngine,
    EngineSetup,
    prepare_run,
)
from xhs_discovery.discovery.scheduler import plan_window
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    ControlActionType,
    OperationState,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    SlotKind,
    TerminalOutcome,
)
from xhs_discovery.models.run import ControlRequest
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.sources.base import AccessNotStarted, SearchSource
from xhs_discovery.sources.fixture import FixtureDataset, FixtureSearchSource
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import (
    WorkerAlreadyRunning,
    WorkerControl,
    global_lock_path,
    lock_path,
    request_control,
)

ROOT = Path(__file__).resolve().parents[1]
_REAL_REPLACE = os.replace
TINY = load_settings(ROOT / "config" / "tests-convergence-tiny.yaml")
DOMAIN_TERMS = ("中考", "志愿", "名额分配", "建档线")
LOCATIONS = ("上海", "北京", "闵行", "徐汇", "杨浦")

BUDGET_KEYS = {
    "suggest_attempts",
    "search_attempts",
    "body_attempts",
    "author_attempts",
    "calibration_suggest_attempts",
    "max_posts_stored",
    "max_runtime_minutes",
}
SAMPLING_KEYS = {"high_value_posts", "medium_value_posts", "low_value_posts", "first_sample_posts"}
RETRY_KEYS = {"max_retries"}


def engine_settings(seed_raw: str = "上海中考", **overrides) -> Settings:
    """测试用配置：只换种子与个别阈值，其余沿用 tiny 配置。

    nlp 路径写成绝对路径，engine 自建 IntentMatcher 时不依赖 pytest 的工作目录。
    """
    known = BUDGET_KEYS | SAMPLING_KEYS | RETRY_KEYS
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(f"未知的配置覆盖项：{sorted(unknown)}")
    budget_update = {key: value for key, value in overrides.items() if key in BUDGET_KEYS}
    sampling_update = {key: value for key, value in overrides.items() if key in SAMPLING_KEYS}
    retry_update = {key: value for key, value in overrides.items() if key in RETRY_KEYS}
    return TINY.model_copy(update={
        "run": TINY.run.model_copy(update={"seed_raw": seed_raw}),
        "nlp": TINY.nlp.model_copy(update={
            "intent_rules_path": str(ROOT / "config" / "intent_rules.yaml"),
            "domain_terms_path": str(ROOT / "config" / "domain_terms.txt"),
        }),
        "budget": TINY.budget.model_copy(update=budget_update) if budget_update else TINY.budget,
        "sampling": TINY.sampling.model_copy(update=sampling_update) if sampling_update else TINY.sampling,
        "retry": TINY.retry.model_copy(update=retry_update) if retry_update else TINY.retry,
    })


def make_source(settings) -> FixtureSearchSource:
    dataset = FixtureDataset.load(
        ROOT / settings.sources.fixture.base_path, settings.sources.fixture.dataset
    )
    return FixtureSearchSource(dataset)


class AccountingLedger(Ledger):
    """把引擎每次预留/结算的入参留在内存里，用来验证"结算必须显式带回自己的预留"。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reserved: dict[int, dict] = {}
        self.settlements: list[dict] = []

    def reserve(self, key, units: float = 1, *, operation_id=None, at=None) -> int:
        reservation_id = super().reserve(key, units, operation_id=operation_id, at=at)
        self.reserved[reservation_id] = {"key": key, "units": units, "operation_id": operation_id}
        return reservation_id

    def settle_external(self, *, operation_id, operation_type, attempt_no, key, units=1.0,
                        reason, reservation_id=None, at=None):
        self.settlements.append(
            {
                "operation_id": operation_id,
                "operation_type": operation_type,
                "attempt_no": attempt_no,
                "key": key,
                "units": units,
                "reservation_id": reservation_id,
            }
        )
        return super().settle_external(
            operation_id=operation_id, operation_type=operation_type, attempt_no=attempt_no,
            key=key, units=units, reason=reason, reservation_id=reservation_id, at=at,
        )


class Harness:
    """一套 run 目录 + 一个数据库；`restart()` 换新连接，模拟 worker 进程退出后重启。"""

    def __init__(self, work: Path, settings, run_id: str) -> None:
        self.settings = settings
        self.run_id = run_id
        self.paths = RuntimePaths(work / "data")
        self.paths.runs_dir.mkdir(parents=True, exist_ok=True)
        self._conns: list[sqlite3.Connection] = []
        self.engine: DiscoveryEngine
        self._open(prepare=True)

    # ---------------------------------------------------------------- 连接

    def _open(self, *, prepare: bool, source=None) -> None:
        conn = storage_db.connect(self.paths.db_path)
        self._conns.append(conn)
        repo = Repository(conn)
        if prepare:
            prepare_run(repo, self.settings, run_id=self.run_id, created_at=utc_now())
        self.conn = conn
        self.repo = repo
        self.ledger = Ledger.from_settings(conn, self.run_id, self.settings)
        self.control = WorkerControl(conn, self.paths, self.run_id)
        self.source = source or make_source(self.settings)
        self.engine = self._engine(self.source, self.ledger)

    def _engine(self, source: SearchSource, ledger: Ledger) -> DiscoveryEngine:
        return DiscoveryEngine(
            paths=self.paths,
            settings=self.settings,
            run_id=self.run_id,
            source=source,
            repo=self.repo,
            ledger=ledger,
            control=self.control,
            domain_terms=DOMAIN_TERMS,
            ambiguous_seeds=("上海",),
            locations=LOCATIONS,
        )

    def restart(self, *, source: SearchSource | None = None) -> None:
        """重启重建：新的连接/Repository/Ledger/WorkerControl，额度只从库里读回来。"""
        self._open(prepare=False, source=source)

    def use_accounting_ledger(self) -> AccountingLedger:
        ledger = AccountingLedger.from_settings(self.conn, self.run_id, self.settings)
        self.ledger = ledger
        self.engine.ledger = ledger
        return ledger

    def swap_source(self, source: SearchSource) -> None:
        self.source = source
        self.engine.source = source

    def close(self) -> None:
        for conn in self._conns:
            conn.close()
        self._conns.clear()

    # ---------------------------------------------------------------- 读数

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, params)]

    def used(self, key: str) -> float:
        return self.ledger.view(key).used

    def limit(self, key: str) -> float:
        return self.ledger.view(key).limit

    def run_row(self) -> dict:
        return self.repo.require_run(self.run_id)

    def ops(self) -> list[dict]:
        return self.repo.operations(self.run_id)

    def attempts(self, operation_id: int) -> list[dict]:
        return self.repo.attempts(operation_id)

    def frontier(self) -> list[dict]:
        return self.rows(
            "SELECT f.id, f.keyword_id, k.normalized_text, b.branch_key, f.status, f.slot_kind,"
            " f.score, f.sample_kind, bs.min_validation_share_left FROM frontier f"
            " JOIN keywords k ON k.id = f.keyword_id JOIN branches b ON b.id = f.branch_id"
            " LEFT JOIN branch_stats bs ON bs.run_id = f.run_id AND bs.branch_key = b.branch_key"
            " WHERE f.run_id = ? ORDER BY f.id",
            (self.run_id,),
        )

    def events(self, kind: str) -> list[dict]:
        rows = self.repo.events(self.run_id, kind=kind)
        for row in rows:
            row["detail"] = json.loads(row["detail_json"] or "{}")
        return rows

    def ghosts(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM budget_reservations WHERE consumed_at IS NULL"
        ).fetchone()["n"])

    # ---------------------------------------------------------------- 驱动

    def guarded_run(self, *, max_slots: int | None = None, calibration: bool = True):
        return self.engine.run_guarded(max_slots=max_slots, calibration=calibration)

    def one_slot(self, **kwargs):
        report = self.guarded_run(max_slots=1, **kwargs)
        assert report.slots, f"没有跑出槽位：{report.events}"
        return report.slots[0]


@pytest.fixture
def workers(tmp_path):
    opened: list[Harness] = []

    def make(*, seed: str = "上海中考", name: str = "main", run_id: str = "run-worker",
             **overrides) -> Harness:
        harness = Harness(tmp_path / name, engine_settings(seed, **overrides), run_id)
        opened.append(harness)
        return harness

    yield make
    for harness in opened:
        harness.close()


# ------------------------------------------------------------------ 记账顺序


def test_slot_books_every_action_in_the_documented_order(workers):
    """预留→访问→落产物→入库→结算：每个动作一笔账，产物与结算都能追到操作与 attempt。"""
    harness = workers(seed="上海中考", search_attempts=4, body_attempts=1, author_attempts=1,
                      low_value_posts=1, suggest_attempts=8)
    slot = harness.one_slot()

    assert slot.normalized == "上海中考"
    assert slot.suggest.state is ResultState.completed and slot.suggest.attempt_no == 1
    assert slot.search is not None and slot.search.state is ResultState.completed
    assert len(slot.bodies) == 1 and slot.bodies[0].state is ResultState.completed
    assert slot.author is not None and slot.author.state is ResultState.completed

    for receipt in (slot.suggest, slot.search, slot.bodies[0], slot.author):
        assert receipt.settlement_ids, f"{receipt.operation_type.value} 没有结算行"
        rows = harness.rows(
            "SELECT * FROM budget_ledger WHERE id = ?", (receipt.settlement_ids[0],)
        )
        assert rows[0]["operation_id"] == receipt.operation_id
        assert rows[0]["attempt_no"] == receipt.attempt_no
        assert rows[0]["external_access"] == 1
        assert rows[0]["budget_key"] == stopping.ACTION_BUDGET_KEYS[receipt.operation_type]
        assert len(receipt.artifact_paths) == 1

    assert harness.used("suggest_attempts") == 1.0
    assert harness.used("search_attempts") == 1.0
    assert harness.used("body_attempts") == 1.0
    assert harness.used("author_attempts") == 1.0
    # 卡片入库计入帖子配额，但配额不是外部访问
    quota = harness.rows(
        "SELECT * FROM budget_ledger WHERE budget_key = 'max_posts_stored' ORDER BY id"
    )
    assert quota and quota[0]["external_access"] == 0
    assert harness.used("max_posts_stored") == 1.0
    assert harness.ghosts() == 0, "槽位跑完必须不留幽灵预留"
    assert harness.rows("SELECT * FROM raw_artifacts ORDER BY id") != []
    assert {row["keyword_id"] for row in harness.ops()} == {
        receipt.keyword_id for receipt in (slot.suggest, slot.search, slot.bodies[0], slot.author)
    }


def test_settlement_carries_the_reservation_it_reserved(workers):
    """引擎一律显式带 reservation_id 结算，结算掉的正是本次为同一操作/键/单位留的那笔。"""
    harness = workers(seed="上海中考", suggest_attempts=4, search_attempts=2)
    ledger = harness.use_accounting_ledger()
    harness.guarded_run(max_slots=2)

    assert ledger.settlements, "跑了两槽却一笔结算都没记"
    for settlement in ledger.settlements:
        reservation_id = settlement["reservation_id"]
        assert reservation_id is not None, "结算必须显式带回自己的预留，不能靠事后猜"
        origin = ledger.reserved[reservation_id]
        assert origin["operation_id"] == settlement["operation_id"]
        assert origin["key"] == settlement["key"]
        assert origin["units"] == settlement["units"]
    # 每笔预留都被结算或释放，不留占额的孤儿
    unconsumed = harness.rows(
        "SELECT id FROM budget_reservations WHERE run_id = ? AND consumed_at IS NULL",
        (harness.run_id,),
    )
    assert unconsumed == []
    settled = harness.rows(
        "SELECT COUNT(*) AS n FROM budget_reservations WHERE run_id = ? AND operation_type IS NOT NULL",
        (harness.run_id,),
    )[0]["n"]
    assert settled == len(ledger.settlements)


def test_low_value_tier_skips_content_without_spending_search_budget(workers):
    """未评分的种子按 low 档处理：显式记 skipped 并说明原因，不偷偷花 search 额度。"""
    harness = workers(seed="上海中考", search_attempts=4, low_value_posts=0)
    slot = harness.one_slot()

    assert slot.search is not None
    assert slot.search.state is ResultState.skipped
    assert slot.search.attempt_no == 0
    assert slot.search.error is not None and slot.search.error.category.value == "budget"
    assert slot.search.error.code == "ACTION_NOT_PERMITTED"
    assert "档位 low 不采内容" in slot.search.error.message
    assert harness.used("search_attempts") == 0.0
    assert harness.rows(
        "SELECT * FROM operation_attempts WHERE operation_id = ?", (slot.search.operation_id,)
    ) == []
    # 内容没采到 → 不得给问题簇奖励，E 继续用中性先验
    assert slot.new_valid_clusters == 0
    assert slot.content_reward == 0.0


def test_first_sample_floor_opens_the_content_gate_for_unvalidated_words(workers):
    """首采保底打开"分数—证据"自指门：档位 low 但还没采过内容的词至少采一篇，账与理由都留痕。"""
    harness = workers(
        seed="上海中考", search_attempts=4, low_value_posts=0, first_sample_posts=1
    )
    slot = harness.one_slot()

    assert slot.search is not None and slot.search.state is ResultState.completed
    assert any("首采保底" in note for note in slot.notes), slot.notes
    reason = harness.rows(
        "SELECT reason FROM budget_ledger WHERE budget_key = 'search_attempts' ORDER BY id"
    )
    assert "档位 low+首采保底" in reason[0]["reason"], reason
    assert harness.used("search_attempts") == 1.0
    assert harness.used("max_posts_stored") >= 1.0


def test_first_sample_floor_fires_once_per_keyword(workers):
    """保底只补第一次证据获取：已有内容观察后回到档位口径，不逐轮放大低分词的开销。"""
    harness = workers(
        seed="上海中考", search_attempts=4, low_value_posts=0, first_sample_posts=1
    )
    slot = harness.one_slot()
    assert slot.search.state is ResultState.completed

    outcome, _posts, notes = harness.engine._collect_content(  # noqa: SLF001 - 直接验证第二次按档位走
        keyword_id=slot.keyword_id,
        branch_key="b-root",
        revisit_seq=1,
        slot_score=None,
        slot_kind=None,
        sample_kind=CalibrationSampleKind.active_exploration,
    )
    assert outcome.receipt.state is ResultState.skipped
    assert outcome.receipt.error is not None and "档位 low 不采内容" in outcome.receipt.error.message
    assert not any("首采保底" in note for note in notes), notes
    assert harness.used("search_attempts") == 1.0, "第二次不得再花内容额度"


def test_first_sample_floor_never_overrides_the_post_quota(workers):
    """帖子配额为 0 时保底不越权：照旧显式 skipped 并说明预算未达，不为凑证据而超上限入库。"""
    harness = workers(
        seed="上海中考", search_attempts=4, max_posts_stored=1, low_value_posts=0,
        first_sample_posts=1, suggest_attempts=8,
        name="floor-quota", run_id="run-floor-quota",
    )
    harness.ledger.record_quota("max_posts_stored", 1, reason="预扣：上一轮已入库 1 篇")
    slot = harness.one_slot()

    assert slot.search.state is ResultState.skipped
    assert slot.search.error is not None
    assert "max_posts_stored 剩余 0" in slot.search.error.message
    assert harness.used("search_attempts") == 0.0


def test_post_quota_cap_is_visible_rather_than_silently_exceeded(workers):
    """帖子配额只剩 1 时按 1 篇采；配额为 0 时显式跳过内容，而不是超上限入库凑指标。"""
    harness = workers(seed="上海中考", search_attempts=4, max_posts_stored=1, low_value_posts=2,
                      suggest_attempts=8)
    slot = harness.one_slot()
    assert harness.used("max_posts_stored") == 1.0
    assert slot.search.state is ResultState.completed

    second = workers(seed="上海中考", search_attempts=4, max_posts_stored=1, low_value_posts=2,
                     suggest_attempts=8, name="capped", run_id="run-capped")
    second.ledger.record_quota("max_posts_stored", 1, reason="预扣：上一轮已入库 1 篇")
    capped = second.one_slot()
    assert capped.search.state is ResultState.skipped
    assert capped.search.error is not None
    assert "max_posts_stored 剩余 0" in capped.search.error.message
    assert second.used("search_attempts") == 0.0
    assert second.used("max_posts_stored") == 1.0


# ------------------------------------------------------------------ 重试与成本


def test_retry_is_a_new_attempt_a_new_reservation_and_a_new_settlement(workers):
    """fixture 的第 1 次联想超时（可重试）：重试要新 attempt、新预留、新结算，成本进分母。"""
    harness = workers(seed="上海中考重试词", suggest_attempts=6)
    slot = harness.one_slot()

    assert slot.suggest.state is ResultState.completed
    assert slot.suggest.attempt_no == 2, "重试成功要按第 2 次尝试记账"
    attempts = harness.attempts(slot.suggest.operation_id)
    assert [row["attempt_no"] for row in attempts] == [1, 2]
    assert [row["result_state"] for row in attempts] == ["failed", "completed"]
    assert json.loads(attempts[0]["error_json"])["category"] == "network"
    assert harness.used("suggest_attempts") == 2.0
    accesses = harness.rows(
        "SELECT attempt_no, consumed_units FROM budget_ledger WHERE operation_id = ?"
        " AND external_access = 1 ORDER BY attempt_no",
        (slot.suggest.operation_id,),
    )
    assert [row["attempt_no"] for row in accesses] == [1, 2], "两次访问都要各记一笔"

    backoff = harness.events("retry_backoff")
    assert len(backoff) == 1
    assert backoff[0]["detail"]["attempt_no"] == 1
    # 逐词状态会被分类器冠上 WORD_ 前缀，但定性仍是 network 且允许有限重试
    assert backoff[0]["detail"]["error_code"] == "WORD_PAGE_TIMEOUT"
    assert backoff[0]["detail"]["planned_seconds"] == harness.settings.retry.backoff_base_seconds
    # 2 次尝试 → 成本 2.0：同样的产出奖励减半
    assert slot.new_related_keywords == 2
    assert slot.suggest_reward == pytest.approx(0.2 * 2 / 2.0)


def test_retry_budget_of_zero_leaves_one_charged_attempt(workers):
    """retry.max_retries=0：只发起一次，账本一笔，失败操作不得记任何新增奖励。"""
    harness = workers(seed="上海中考重试词", suggest_attempts=6, max_retries=0)
    slot = harness.one_slot()

    assert slot.suggest.state is ResultState.failed
    assert slot.suggest.attempt_no == 1
    assert [row["attempt_no"] for row in harness.attempts(slot.suggest.operation_id)] == [1]
    assert harness.used("suggest_attempts") == 1.0
    assert harness.events("retry_backoff") == []
    assert slot.new_related_keywords == 0
    assert slot.suggest_reward == 0.0
    assert harness.ops()[0]["status"] == OperationState.failed.value


def test_attempt_cap_reuses_the_same_operation_row(workers):
    """同一逻辑键的历史尝试用完后不再访问平台：要重做得有新的重探序号。"""
    harness = workers(seed="上海中考重试词", suggest_attempts=8, max_retries=0)
    keyword_id = harness.one_slot().keyword_id
    first = harness.repo.operation_by_key(harness.run_id, keyword_id, OperationType.suggest, 0)
    assert first["status"] == OperationState.failed.value
    assert len(harness.attempts(int(first["id"]))) == 1

    outcome = harness.engine._observe_suggestions(  # noqa: SLF001 - 直接验证序号上限这条纪律
        keyword_id=keyword_id, branch_key="b-root", revisit_seq=0,
        slot_kind=None, sample_kind=CalibrationSampleKind.active_exploration,
    )
    assert outcome.receipt.operation_id == int(first["id"]), "逻辑键相同必须是同一操作行"
    assert outcome.notes and "历史尝试已达上限" in outcome.notes[0]
    assert outcome.receipt.state is ResultState.skipped
    assert harness.used("suggest_attempts") == 1.0, "放弃重做不该再扣一次额度"
    assert len(harness.attempts(int(first["id"]))) == 1, "放弃重做不得再开一次 attempt"


# ------------------------------------------------------------------ 所有权与锁


def test_worker_without_the_lease_stops_before_spending_anything(workers):
    """租约不属于本实例时，操作前检查就把 worker 拦下：零访问、零预留，在飞操作留给接手者。"""
    harness = workers(seed="上海中考", suggest_attempts=6)
    report = harness.engine.run(max_slots=3)  # 故意不持锁跑

    assert any("租约所有权已丢失" in event for event in report.events)
    assert report.slots == []
    assert harness.used("suggest_attempts") == 0.0
    assert harness.ghosts() == 0
    in_flight = [row for row in harness.ops() if row["status"] == OperationState.in_flight.value]
    assert len(in_flight) == 1
    assert harness.attempts(int(in_flight[0]["id"])) == []
    assert harness.run_row()["status"] == RunStatus.running.value

    harness.restart()
    follow_up = harness.guarded_run(max_slots=1)
    assert follow_up.recovered["uncertain_operations"] == [int(in_flight[0]["id"])]
    assert any("uncertain" in json.dumps(row["detail"], ensure_ascii=False)
               for row in harness.events("artifact_orphaned"))


def test_takeover_by_a_new_holder_silences_the_old_worker(workers):
    """实例令牌不等于 PID：被接管后旧 worker 既续不了租也花不了预算。"""
    harness = workers(seed="上海中考", suggest_attempts=6)
    harness.control.acquire()
    harness.control.release()  # 模拟旧进程退出后交还互斥
    new_holder = WorkerControl(harness.conn, harness.paths, harness.run_id)
    new_holder.acquire()

    with pytest.raises(WorkerAlreadyRunning):
        harness.engine._observe_suggestions(  # noqa: SLF001 - 所有权检查就是这条纪律的落点
            keyword_id=_seed_keyword_id(harness), branch_key="b-root", revisit_seq=0,
            slot_kind=None, sample_kind=CalibrationSampleKind.active_exploration,
        )
    assert harness.used("suggest_attempts") == 0.0
    assert harness.ghosts() == 0
    new_holder.release()


def _seed_keyword_id(harness: Harness) -> int:
    row = harness.rows(
        "SELECT keyword_id FROM run_keywords WHERE run_id = ? AND depth = 0", (harness.run_id,)
    )
    assert len(row) == 1
    return int(row[0]["keyword_id"])


def test_run_guarded_releases_both_locks_when_the_source_explodes(workers):
    """访问途中炸掉：那一次访问照样计费、attempt 记 failed，`finally` 一定把两把锁交还。

    C-10 修正了本用例原先"异常必定 used=0"的错误假设：源在调用中途抛异常**无法证明**平台
    没收到那次访问，按计划口径照计一笔。锁释放断言与预算要求都保持原样，只是把退款改成计费。
    """

    class ExplodingSource(FixtureSearchSource):
        def get_suggestions(self, context):
            raise RuntimeError("模拟 worker 在外部访问中途崩了")

    harness = workers(seed="上海中考", suggest_attempts=6)
    harness.swap_source(ExplodingSource(harness.source.dataset))
    with pytest.raises(RuntimeError):
        harness.guarded_run(max_slots=2)

    assert lock_path(harness.paths, harness.run_id).exists() is False
    assert global_lock_path(harness.paths).exists() is False
    lease = harness.rows("SELECT * FROM worker_leases WHERE run_id = ?", (harness.run_id,))
    assert lease[0]["released_at"] is not None
    assert harness.used("suggest_attempts") == 1.0, "无法证明没访问就照计，退款等于把真实成本抹掉"
    assert harness.ghosts() == 0
    accesses = harness.rows(
        "SELECT operation_id, attempt_no, consumed_units, external_access, reason"
        " FROM budget_ledger WHERE budget_key = 'suggest_attempts' AND external_access = 1"
    )
    assert len(accesses) == 1, "一次 attempt 最多一笔访问账"
    assert float(accesses[0]["consumed_units"]) == 1.0
    operation = harness.ops()[0]
    assert operation["status"] == OperationState.failed.value
    attempt = harness.attempts(int(operation["id"]))[0]
    assert attempt["result_state"] == ResultState.failed.value
    error_json = json.loads(attempt["error_json"])
    assert error_json["category"] == "system"
    assert error_json["access_phase"] == "access" and error_json["access_charged"] == 1

    # 锁已交还：下一个 worker 能正常起，崩溃那笔操作按新的 attempt 继续，不改写历史
    harness.restart()
    harness.repo.enqueue(
        harness.run_id, _seed_keyword_id(harness), "b-root",
        selection_reason="崩溃后重新入队（测试用）", queued_at=utc_now(),
    )
    report = harness.guarded_run(max_slots=1)
    assert report.slots, "放锁失败会导致第二个 worker 起不来"
    redo = report.slots[0].suggest
    assert redo.revisit_seq == 1 and redo.operation_id != int(operation["id"]), "重做要用新的重探序号开新操作行"
    assert redo.attempt_no == 1 and redo.state is ResultState.completed
    assert [row["result_state"] for row in harness.attempts(int(operation["id"]))] == ["failed"], \
        "崩溃那一条 attempt 是历史事实，不得被改写"


# ------------------------------------------------------------------ 访问阶段与计费（C-10）


class ChargedMidAccessSource(FixtureSearchSource):
    """平台已经把结果回来，源在交回引擎之前炸了：访问发生与否无法证明，只能照计。"""

    def __init__(self, dataset) -> None:
        super().__init__(dataset)
        self.calls = 0

    def get_suggestions(self, context):
        self.calls += 1
        super().get_suggestions(context)
        raise TimeoutError("模拟：结果已到手，源在返回前崩了")


class ProvenNotAccessedSource(FixtureSearchSource):
    """数据源用 `AccessNotStarted` 交出"请求根本没发出"的证据（本地校验、会话未绑定）。"""

    def __init__(self, dataset) -> None:
        super().__init__(dataset)
        self.calls = 0

    def get_suggestions(self, context):
        self.calls += 1
        raise AccessNotStarted("模拟：参数校验在请求发出之前失败，平台没有收到这次访问")


class UnbindableSource(FixtureSearchSource):
    """连会话绑定这一步都失败：请求必然没发出，落在 `not_started` 阶段。"""

    def bind(self, context):
        raise AccessNotStarted("模拟：绑定会话前就失败")


def _suggest_receipt(harness: Harness, revisit_seq: int = 0):
    return harness.engine._observe_suggestions(  # noqa: SLF001 - 单操作阶段口径只能直接驱动
        keyword_id=_seed_keyword_id(harness), branch_key="b-root", revisit_seq=revisit_seq,
        slot_kind=None, sample_kind=CalibrationSampleKind.active_exploration,
    )


@contextlib.contextmanager
def leased(harness: Harness):
    """直接驱动单个操作时要持有租约（所有权检查在访问前，不持锁会被当成僵尸 worker）。"""
    harness.control.acquire()
    try:
        yield harness
    finally:
        harness.control.release()


def assert_one_charge(harness: Harness, operation_id: int, *, attempt_no: int = 1) -> dict:
    """该操作恰好一笔外部访问账，且预留被这笔账消费掉、不留幽灵。"""
    rows = harness.rows(
        "SELECT * FROM budget_ledger WHERE operation_id = ? AND external_access = 1"
        " ORDER BY id",
        (operation_id,),
    )
    assert len(rows) == 1, f"每 attempt 至多一笔访问账，实际 {len(rows)} 笔"
    assert int(rows[0]["attempt_no"]) == attempt_no
    assert float(rows[0]["consumed_units"]) == 1.0
    assert harness.ghosts() == 0, "已计费的访问必须把预留消费掉，不能继续占额也不能退回"
    consumed = harness.rows(
        "SELECT consumed_at FROM budget_reservations WHERE operation_id = ?", (operation_id,)
    )
    assert len(consumed) == 1 and consumed[0]["consumed_at"] is not None
    return rows[0]


def test_write_artifact_crash_after_returned_access_is_still_charged(workers, monkeypatch):
    """① 源正常返回、落盘时磁盘写失败：访问确实发生过，预算照计，本地故障不得变成免费额度。"""

    def boom(*args, **kwargs):
        raise OSError(28, "模拟：磁盘写入失败")

    harness = workers(seed="上海中考", suggest_attempts=6)
    calls = []
    original = harness.source.get_suggestions

    def counting(context):
        result = original(context)
        calls.append(context)
        return result

    harness.source.get_suggestions = counting  # type: ignore[method-assign]
    monkeypatch.setattr("xhs_discovery.discovery.engine.write_artifact", boom)

    with pytest.raises(OSError):
        harness.guarded_run(max_slots=1)

    assert len(calls) == 1, "访问已经成功返回"
    assert harness.used("suggest_attempts") == 1.0
    operation = harness.ops()[0]
    ledger_row = assert_one_charge(harness, int(operation["id"]))
    assert ledger_row["reason"] == "一次联想观察"
    attempt = harness.attempts(int(operation["id"]))[0]
    assert attempt["result_state"] == ResultState.failed.value
    assert json.loads(attempt["error_json"]) == {
        "message": "[Errno 28] 模拟：磁盘写入失败",
        "category": "output",
        "access_phase": "local",
        "access_charged": 1,
        "billing_error": None,
    }
    assert attempt["raw_artifact_count"] == 0
    # 本地一半都没做成：不该有任何观察/条目落库，但账与事件必须在
    assert harness.rows("SELECT COUNT(*) AS n FROM suggestion_observations")[0]["n"] == 0
    assert harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"] == 0
    assert lock_path(harness.paths, harness.run_id).exists() is False, "异常收口不得漏放 worker 锁"
    events = harness.events("attempt_charged_after_failure")
    assert len(events) == 1
    assert events[0]["detail"]["access_phase"] == "local"
    assert events[0]["detail"]["access_charged"] is True


def test_real_artifact_write_failure_keeps_atomicity_and_still_charges(workers, monkeypatch):
    """① 续：走真实落盘路径的失败（`os.replace` 炸）既不破坏 P0-09 原子纪律，也不退款。

    C-10 只改计费口径，不改产物写协议：临时文件必须被撤回、目标文件不得存在、
    `raw_artifacts` 不得有半行，而那一次已经发生的访问照样记账。
    """

    def boom(src, dst, *args, **kwargs):
        # 只打断采集产物的 rename：worker 归属记录也走 os.replace，不能一起炸
        if str(dst).endswith(".json"):
            raise OSError(28, "模拟：rename 到最终路径失败")
        return _REAL_REPLACE(src, dst, *args, **kwargs)

    harness = workers(seed="上海中考", suggest_attempts=6)
    monkeypatch.setattr("xhs_discovery.storage.artifacts.os.replace", boom)

    with pytest.raises(OSError):
        harness.guarded_run(max_slots=1)

    assert harness.used("suggest_attempts") == 1.0
    operation = harness.ops()[0]
    assert_one_charge(harness, int(operation["id"]))
    assert harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"] == 0
    assert harness.rows(
        "SELECT COUNT(*) AS n FROM operation_attempts WHERE raw_artifact_count = 1"
    )[0]["n"] == 0
    leftovers = [p.name for p in harness.paths.runs_dir.rglob(".tmp-*")]
    assert leftovers == [], f"失败的落盘必须撤回临时文件，实际残留 {leftovers}"
    assert [p.name for p in harness.paths.runs_dir.rglob("*.json")] == []

    monkeypatch.undo()  # 磁盘恢复：同一 attempt 已计费，重做得是新的一次访问
    with leased(harness):
        outcome = _suggest_receipt(harness, revisit_seq=int(operation["revisit_seq"]))
    assert outcome.receipt.state is ResultState.completed
    assert harness.used("suggest_attempts") == 2.0
    assert harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"] == 1


def test_ingest_crash_after_returned_access_is_still_charged(workers, monkeypatch):
    """② 产物已落盘、入库时炸：同样是"访问已发生"，照计一笔，已落产物按事实计数。"""

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("模拟：入库事务失败")

    harness = workers(seed="上海中考", suggest_attempts=6)
    monkeypatch.setattr("xhs_discovery.discovery.engine.ingest_suggestion", boom)

    with pytest.raises(sqlite3.OperationalError):
        harness.guarded_run(max_slots=1)

    assert harness.used("suggest_attempts") == 1.0
    operation = harness.ops()[0]
    assert_one_charge(harness, int(operation["id"]))
    attempt = harness.attempts(int(operation["id"]))[0]
    error_json = json.loads(attempt["error_json"])
    assert error_json["access_phase"] == "local" and error_json["access_charged"] == 1
    assert error_json["category"] == "output"
    assert attempt["raw_artifact_count"] == 1, "产物真的落库了，事实计数要跟上"
    assert harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"] == 1
    assert harness.rows("SELECT COUNT(*) AS n FROM suggestion_observations")[0]["n"] == 0


def test_source_exception_mid_call_is_not_treated_as_not_accessed(workers):
    """③ 源在调用中抛异常：不能自动等同"没访问"，一律保守计费。"""
    harness = workers(seed="上海中考", suggest_attempts=6)
    source = ChargedMidAccessSource(harness.source.dataset)
    harness.swap_source(source)

    with pytest.raises(TimeoutError):
        harness.guarded_run(max_slots=1)

    assert source.calls == 1
    assert harness.used("suggest_attempts") == 1.0, "调用中炸掉无法证明平台没收到，退款等于漏计"
    operation = harness.ops()[0]
    assert_one_charge(harness, int(operation["id"]))
    attempt = harness.attempts(int(operation["id"]))[0]
    error_json = json.loads(attempt["error_json"])
    assert error_json["access_phase"] == "access" and error_json["access_charged"] == 1
    assert error_json["category"] == "system"


@pytest.mark.parametrize(
    ("source_class", "phase"),
    [(ProvenNotAccessedSource, "access"), (UnbindableSource, "not_started")],
    ids=["source-proves-not-started", "bind-failed-before-handoff"],
)
def test_proven_not_accessed_failure_returns_the_reservation(workers, source_class, phase):
    """④ 有证据证明请求没发出：预留退回可用池、零访问账，额度立刻能再花。"""
    harness = workers(seed="上海中考", suggest_attempts=6)
    source = source_class(harness.source.dataset)
    harness.swap_source(source)

    with pytest.raises(AccessNotStarted):
        harness.guarded_run(max_slots=1)

    assert harness.used("suggest_attempts") == 0.0
    operation = harness.ops()[0]
    assert harness.rows(
        "SELECT COUNT(*) AS n FROM budget_ledger WHERE operation_id = ? AND external_access = 1",
        (int(operation["id"]),),
    )[0]["n"] == 0, "没发生的访问不得记账"
    released = harness.rows(
        "SELECT consumed_at FROM budget_reservations WHERE operation_id = ?", (int(operation["id"]),)
    )
    assert len(released) == 1 and released[0]["consumed_at"] is not None
    assert harness.ghosts() == 0, "退款要立刻收掉预留，不能留一笔永远占额的幽灵"
    assert harness.ledger.view("suggest_attempts").remaining == 6.0
    attempt = harness.attempts(int(operation["id"]))[0]
    error_json = json.loads(attempt["error_json"])
    assert error_json["access_phase"] == phase and error_json["access_charged"] == 0
    assert error_json["category"] == "usage"
    assert len(harness.events("attempt_refunded_not_accessed")) == 1
    assert harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"] == 0

    # 额度回来了：换成正常源再跑一次，才是真实的一次访问
    harness.swap_source(FixtureSearchSource(harness.source.dataset))
    with leased(harness):
        outcome = _suggest_receipt(harness, revisit_seq=1)
    assert outcome.receipt.state is ResultState.completed
    assert harness.used("suggest_attempts") == 1.0


def test_recovery_backfills_a_local_failure_whose_settle_also_failed(workers, monkeypatch):
    """⑤ 落盘炸 + 结算也炸：原始异常不被记账故障盖掉，预留留待恢复补记，重启后既不重复也不漏。"""

    class UnsettlingLedger(Ledger):
        def settle_external(self, **kwargs):  # 模拟结算事务本身失败
            raise RuntimeError("模拟：结算事务失败")

    def boom(*args, **kwargs):
        raise OSError(28, "模拟：磁盘写入失败")

    harness = workers(seed="上海中考", suggest_attempts=6)
    ledger = UnsettlingLedger.from_settings(harness.conn, harness.run_id, harness.settings)
    harness.ledger = ledger
    harness.engine.ledger = ledger
    monkeypatch.setattr("xhs_discovery.discovery.engine.write_artifact", boom)

    with pytest.raises(OSError):  # 原始异常优先：结算失败不能变成新的崩溃原因
        harness.guarded_run(max_slots=1)

    operation = harness.ops()[0]
    attempt = harness.attempts(int(operation["id"]))[0]
    error_json = json.loads(attempt["error_json"])
    assert error_json["access_phase"] == "local" and error_json["access_charged"] == 1
    assert "结算事务失败" in error_json["billing_error"]
    assert harness.used("suggest_attempts") == 0.0, "没写成的账不能假装写过"
    assert harness.ghosts() == 1, "结算失败的预留要留在账上等恢复补记，退款才是漏计"
    reservation = harness.rows(
        "SELECT id FROM budget_reservations WHERE operation_id = ?", (int(operation["id"]),)
    )[0]

    harness.repo.enqueue(
        harness.run_id, _seed_keyword_id(harness), "b-root",
        selection_reason="崩溃后重新入队（测试用）", queued_at=utc_now(),
    )
    monkeypatch.undo()  # 磁盘恢复健康：重启后既要补旧账，也要照常花新的一次访问
    harness.restart()  # 新的正常 Ledger：恢复按"已发起未记账"补记既成事实
    report = harness.guarded_run(max_slots=1)
    assert int(reservation["id"]) in report.recovered["released_reservations"]
    assert harness.ghosts() == 0
    assert harness.used("suggest_attempts") == 2.0, "补记崩溃那一次 + 本槽一次，两笔都是真实访问"
    crashed_rows = harness.rows(
        "SELECT attempt_no, reason FROM budget_ledger WHERE operation_id = ? AND external_access = 1"
        " ORDER BY id",
        (int(operation["id"]),),
    )
    assert len(crashed_rows) == 1, "同一 attempt 补记也只能有一笔，不得重复计费"
    assert crashed_rows[0]["attempt_no"] == 1 and "崩溃恢复" in crashed_rows[0]["reason"]


def test_restarting_after_a_charged_local_failure_charges_only_the_new_attempt(workers, monkeypatch):
    """⑤ 续：已计费的失败 attempt 重放不再扣第二次，重试是新 attempt、新一次访问、新的一笔。"""

    def boom(*args, **kwargs):
        raise OSError(28, "模拟：磁盘写入失败")

    harness = workers(seed="上海中考", suggest_attempts=6)
    monkeypatch.setattr("xhs_discovery.discovery.engine.write_artifact", boom)
    with pytest.raises(OSError):
        harness.guarded_run(max_slots=1)

    operation = harness.ops()[0]
    assert_one_charge(harness, int(operation["id"]))
    monkeypatch.undo()
    harness.restart()
    with leased(harness):
        outcome = _suggest_receipt(harness, revisit_seq=int(operation["revisit_seq"]))
    assert outcome.receipt.operation_id == int(operation["id"]), "同一逻辑键必须回到同一操作行"
    assert outcome.receipt.attempt_no == 2
    assert outcome.receipt.state is ResultState.completed
    assert harness.used("suggest_attempts") == 2.0, "第一次照计 + 第二次真访问，各一笔"
    rows = harness.rows(
        "SELECT attempt_no FROM budget_ledger WHERE operation_id = ? AND external_access = 1"
        " ORDER BY attempt_no",
        (int(operation["id"]),),
    )
    assert [row["attempt_no"] for row in rows] == [1, 2]
    assert [row["result_state"] for row in harness.attempts(int(operation["id"]))] == [
        "failed",
        "completed",
    ], "历史 attempt 的账与状态不得被重放改写"
    assert harness.ghosts() == 0


def test_failed_access_consumes_the_last_unit_without_overspend(workers, monkeypatch):
    """⑥ 额度只剩 1 次：本地失败照样把它花掉，之后的操作显式跳过，绝不越界、也不留免费额度。"""

    def boom(*args, **kwargs):
        raise OSError(28, "模拟：磁盘写入失败")

    harness = workers(seed="上海中考", suggest_attempts=1)
    monkeypatch.setattr("xhs_discovery.discovery.engine.write_artifact", boom)
    with pytest.raises(OSError):
        harness.guarded_run(max_slots=1)

    assert harness.used("suggest_attempts") == 1.0
    assert harness.ledger.view("suggest_attempts").remaining == 0.0
    assert_one_charge(harness, int(harness.ops()[0]["id"]))
    monkeypatch.undo()

    harness.swap_source(FixtureSearchSource(harness.source.dataset))
    with leased(harness):
        outcome = _suggest_receipt(harness, revisit_seq=1)
    assert outcome.receipt.state is ResultState.skipped
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == "BUDGET_EXHAUSTED"
    assert outcome.receipt.error.category.value == "budget"
    assert harness.used("suggest_attempts") == 1.0, "预留阶段就被挡住，不得越过上限发起访问"
    assert harness.rows(
        "SELECT COUNT(*) AS n FROM budget_ledger WHERE external_access = 1"
    )[0]["n"] == 1
    assert harness.ghosts() == 0


# ------------------------------------------------------------------ 重放与重启


def test_reopening_a_reliable_observation_replays_without_access(workers):
    """同一逻辑键已有 completed 观察：重放只读库、记一次缓存命中，绝不再访问平台。"""
    harness = workers(seed="上海中考", suggest_attempts=8)
    slot = harness.one_slot()
    spent = harness.used("suggest_attempts")
    items_before = harness.rows("SELECT COUNT(*) AS n FROM suggestion_items")[0]["n"]
    edges_before = harness.rows("SELECT COUNT(*) AS n FROM discovery_edges")[0]["n"]

    outcome = harness.engine._observe_suggestions(  # noqa: SLF001 - 重放分支只能同键重开才走得到
        keyword_id=slot.keyword_id, branch_key="b-root", revisit_seq=slot.suggest.revisit_seq,
        slot_kind=None, sample_kind=CalibrationSampleKind.active_exploration,
    )
    receipt = outcome.receipt
    assert receipt.operation_id == slot.suggest.operation_id
    assert receipt.replayed is True and receipt.cache_hit is True
    assert receipt.state is ResultState.completed
    assert receipt.settlement_ids == [], "重放不产生新的外部访问账"
    assert outcome.notes[0] == REPLAY_NOTE

    assert harness.used("suggest_attempts") == spent, "重放不得再扣访问额度"
    cache_rows = harness.rows(
        "SELECT * FROM budget_ledger WHERE external_access = 0 AND budget_key = 'suggest_attempts'"
        " AND reason = ?", (REPLAY_NOTE,)
    )
    assert len(cache_rows) == 1
    assert harness.rows("SELECT COUNT(*) AS n FROM suggestion_items")[0]["n"] == items_before
    assert harness.rows("SELECT COUNT(*) AS n FROM discovery_edges")[0]["n"] == edges_before
    assert len(outcome.ingest.children) == len(suggestion_children(harness, slot))
    assert "从库里重建" in outcome.notes[1]


def suggestion_children(harness: Harness, slot):
    """本次槽位实际观察到的联想子词（重放必须重建出同一批）。"""
    rows = harness.rows(
        "SELECT si.keyword_id FROM suggestion_items si JOIN suggestion_observations so"
        " ON so.id = si.observation_id WHERE so.operation_id = ? ORDER BY si.rank",
        (slot.suggest.operation_id,),
    )
    return [row["keyword_id"] for row in rows]


def test_restarted_worker_reads_its_budget_back_from_the_database(workers):
    """重启重建：新连接/新 Ledger 只从库里的账读出已花额度，也不重复导入已登记的产物。"""
    harness = workers(seed="上海中考", suggest_attempts=8, search_attempts=2)
    harness.guarded_run(max_slots=1)
    spent = harness.used("suggest_attempts")
    artifact_rows = harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"]

    harness.restart()
    assert harness.used("suggest_attempts") == spent, "重启后额度必须从库里读回"
    assert harness.limit("suggest_attempts") == 8.0
    report = harness.guarded_run(max_slots=1)
    assert report.recovered["imported_artifacts"] == 0, "已登记的产物不该被重复导入"
    assert harness.rows("SELECT COUNT(*) AS n FROM raw_artifacts")[0]["n"] > artifact_rows
    assert harness.used("suggest_attempts") == spent + 1.0
    assert harness.ghosts() == 0


def test_recovery_charges_a_crashed_access_instead_of_refunding_it(workers):
    """崩溃窗口里已发起的访问是既成事实：恢复要补记消耗并收掉预留，绝不退款。"""
    harness = workers(seed="上海中考", suggest_attempts=8, search_attempts=2)
    harness.guarded_run(max_slots=1)
    spent = harness.used("suggest_attempts")

    # 手工还原上一任死在访问中途的样子：操作在飞、attempt 已 started、预留未消费
    crashed = harness.repo.open_operation(
        harness.run_id, _seed_keyword_id(harness), OperationType.suggest, 7
    )
    harness.repo.mark_operation_started(crashed)
    harness.repo.start_attempt(crashed, 1)
    ghost = harness.ledger.reserve("suggest_attempts", 1, operation_id=crashed)
    assert harness.ghosts() == 1

    harness.restart()
    report = harness.guarded_run(max_slots=1)
    assert report.recovered["released_reservations"] == [ghost]
    assert harness.ghosts() == 0
    assert harness.used("suggest_attempts") == spent + 2.0, "补记一次既成访问 + 本槽一次访问"
    charged = harness.rows(
        "SELECT reason, external_access FROM budget_ledger WHERE operation_id = ?", (crashed,)
    )
    assert len(charged) == 1 and charged[0]["external_access"] == 1
    assert "崩溃恢复" in charged[0]["reason"]
    assert any("恢复时释放幽灵预留" in event for event in report.events)


# ------------------------------------------------------------------ 暂停、恢复与取消


def test_pause_request_takes_effect_at_the_next_safe_point(workers):
    """槽位跑到一半收到暂停意图：当前槽做完、账不回滚，下一轮在安全点退出。"""

    class PausingOnceSource(FixtureSearchSource):
        def __init__(self, dataset, harness) -> None:
            super().__init__(dataset)
            self.harness = harness
            self.signalled = False

        def get_suggestions(self, context):
            result = super().get_suggestions(context)
            if not self.signalled:
                self.signalled = True
                request_control(self.harness.conn, ControlRequest(
                    run_id=self.harness.run_id, action=ControlActionType.pause,
                    requested_at=utc_now(), note="用户点了暂停",
                ))
            return result

    harness = workers(seed="上海中考", suggest_attempts=8, search_attempts=2)
    harness.swap_source(PausingOnceSource(harness.source.dataset, harness))
    spent_during_slot = None
    report = harness.engine.run_guarded(max_slots=4)
    spent_during_slot = harness.used("suggest_attempts")

    assert len(report.slots) == 1, "暂停只在安全点生效，当前槽要做完"
    assert any("消费控制请求" in event and "pause" in event for event in report.events)
    assert any("worker 在安全点退出" in event for event in report.events)
    assert harness.run_row()["status"] == RunStatus.paused.value
    assert harness.run_row()["pause_reason"] == PauseReason.user.value
    assert report.termination.status is RunStatus.paused
    assert spent_during_slot > 0.0, "已扣预算不回滚"

    # 恢复：状态由控制请求写回，worker 从库里继续接着跑，预算不重置
    harness.restart()
    report2 = harness.engine.run_guarded(max_slots=1)
    assert any("消费控制请求" in event and "resume" in event for event in report2.events) is False
    assert harness.run_row()["status"] == RunStatus.paused.value
    assert report2.termination.pause_reason is PauseReason.user
    assert harness.used("suggest_attempts") == spent_during_slot, "暂停期间没有偷偷访问"


def test_resume_after_pause_continues_from_the_persisted_state(workers):
    """恢复不是重开：pause → resume 之后 worker 继续消费队列，预算与图状态都延续。"""
    harness = workers(seed="上海中考", suggest_attempts=8, search_attempts=2)
    harness.guarded_run(max_slots=1)
    request_control(harness.conn, ControlRequest(
        run_id=harness.run_id, action=ControlActionType.pause, requested_at=utc_now(), note="先停",
    ))
    paused = harness.guarded_run(max_slots=2)
    assert paused.slots == []
    assert harness.run_row()["status"] == RunStatus.paused.value

    harness.restart()
    request_control(harness.conn, ControlRequest(
        run_id=harness.run_id, action=ControlActionType.resume, requested_at=utc_now(),
    ))
    resumed = harness.guarded_run(max_slots=1)
    assert len(resumed.slots) == 1
    assert harness.run_row()["status"] == RunStatus.running.value
    assert harness.run_row()["pause_reason"] is None
    assert harness.used("suggest_attempts") == 2.0, "恢复后继续花额度，而不是重新计数"
    assert harness.ghosts() == 0


def test_extend_adds_quota_without_resetting_what_was_spent(workers):
    """预算耗尽 → 暂停；extend 只追加增量（总量不清零），恢复后探索继续。"""
    harness = workers(seed="上海中考", suggest_attempts=1, search_attempts=1)
    report = harness.guarded_run()
    assert harness.used("suggest_attempts") == 1.0
    assert report.termination.status is RunStatus.paused
    assert report.termination.pause_reason is PauseReason.fault
    assert "追加预算后可恢复" in report.termination.note
    assert report.termination.outcome is None, "预算未达不是收敛"

    harness.restart()
    request_control(harness.conn, ControlRequest(
        run_id=harness.run_id, action=ControlActionType.extend, requested_at=utc_now(),
        suggest_budget_delta=2, note="追加两次联想",
    ))
    request_control(harness.conn, ControlRequest(
        run_id=harness.run_id, action=ControlActionType.resume, requested_at=utc_now(),
    ))
    resumed = harness.guarded_run(max_slots=1)
    assert len(resumed.slots) == 1, "追加额度后必须能继续探索"
    assert any("extend" in event for event in resumed.events)
    assert harness.limit("suggest_attempts") == 3.0
    assert harness.used("suggest_attempts") == 2.0, "追加额度不得把已花掉的清零"
    revisions = harness.repo.budget_revisions(harness.run_id)
    assert len(revisions) == 1
    assert json.loads(revisions[0]["added_json"])["suggest_attempts"] == 2.0


def test_cancel_request_records_cancelled_and_stops_access(workers):
    """取消走终态：completed + outcome=cancelled，绝不写成收敛。"""
    harness = workers(seed="上海中考", suggest_attempts=8)
    request_control(harness.conn, ControlRequest(
        run_id=harness.run_id, action=ControlActionType.cancel, requested_at=utc_now(),
        note="用户取消",
    ))
    report = harness.guarded_run(max_slots=3)
    assert report.slots == []
    row = harness.run_row()
    assert row["status"] == RunStatus.completed.value
    assert row["outcome"] == TerminalOutcome.cancelled.value
    assert report.termination.outcome is TerminalOutcome.cancelled
    assert harness.used("suggest_attempts") == 0.0


# ------------------------------------------------------------------ 异常与停止反例


def test_auth_failure_pauses_the_run_and_never_counts_as_convergence(workers):
    """需要登录：不可重试，直接停在 paused/auth_required，并保留人可处理的说明。"""
    harness = workers(seed="上海中考认证暂停", suggest_attempts=8)
    report = harness.guarded_run(max_slots=3)

    assert harness.engine.halt_error is not None
    assert harness.engine.halt_error.category.value == "auth"
    assert report.termination.status is RunStatus.paused
    assert report.termination.pause_reason is PauseReason.auth_required
    assert report.termination.outcome is None
    assert "不得记为收敛" in report.termination.note
    assert harness.run_row()["pause_reason"] == PauseReason.auth_required.value
    slot = report.slots[0]
    assert slot.suggest.state is ResultState.failed
    assert slot.new_related_keywords == 0 and slot.suggest_reward == 0.0


def test_selector_drift_partials_are_observations_not_failures(workers):
    """partial（一词成功一词选择器漂移）仍是可靠观察：给关键词奖励，但不升级成暂停。"""
    harness = workers(seed="上海中考未知作者", suggest_attempts=6)
    slot = harness.one_slot()

    assert slot.suggest.state is ResultState.partial
    assert slot.suggest.succeeded is True
    assert harness.engine.halt_error is None
    attempt = harness.attempts(slot.suggest.operation_id)[0]
    assert attempt["result_state"] == ResultState.partial.value
    assert slot.new_related_keywords > 0
    assert slot.suggest_reward > 0.0
    assert len(harness.rows(
        "SELECT * FROM suggestion_observations WHERE operation_id = ? AND state = 'partial'",
        (slot.suggest.operation_id,),
    )) == 1


def test_declined_word_is_recorded_as_skipped_with_a_reason(workers):
    """fixture 显式 skipped（上游限频跳过）：记 skipped，不伪装成"平台没返回"。"""
    harness = workers(seed="上海中考跳过词", suggest_attempts=6)
    slot = harness.one_slot()

    assert slot.suggest.state is ResultState.skipped
    assert slot.suggest.succeeded is False
    assert slot.suggest.error is not None and slot.suggest.error.category.value == "budget"
    assert slot.suggest.error.code == "WORD_SKIPPED"
    assert slot.suggest.error.retryable is False
    assert "限频跳过" in slot.suggest.error.message
    assert harness.ops()[0]["status"] == OperationState.skipped.value
    assert slot.suggest_reward == 0.0 and slot.new_related_keywords == 0
    assert harness.used("suggest_attempts") == 1.0, " skipped 也是一次真实访问"
    assert harness.events("retry_backoff") == []


def test_empty_result_words_drain_the_queue_without_convergence(workers):
    """平台确实没有联想：completed + 空结果，队列排空后按 queue_exhausted 终止。"""
    harness = workers(seed="上海中考空结果", suggest_attempts=6, search_attempts=0,
                      max_posts_stored=0)
    report = harness.guarded_run()

    assert report.termination.status is RunStatus.completed
    assert report.termination.outcome is TerminalOutcome.queue_exhausted
    assert "未满足的收敛条件" in report.termination.note
    assert harness.run_row()["outcome"] == TerminalOutcome.queue_exhausted.value
    assert harness.used("suggest_attempts") == 1.0
    assert harness.frontier()[0]["status"] == "done"


def test_branch_exploration_is_not_hijacked_by_one_bucket(workers):
    """60/25/15 分配要真的轮到：候选不继承发现它的槽类型，三个桶按窗口交错取。"""
    harness = workers(seed="上海中考", suggest_attempts=24, search_attempts=4)
    report = harness.guarded_run(max_slots=8)
    wanted = plan_window(harness.settings)
    assert sum(1 for kind in wanted if kind is SlotKind.high_score) == 5
    assert sum(1 for kind in wanted if kind is SlotKind.branch_exploration) == 2

    kinds = [slot.slot_kind for slot in report.slots]
    assert kinds[0] is SlotKind.branch_exploration, "冷启动没有高分候选，回流到分支探索槽"
    assert SlotKind.high_score in kinds, "高分槽一直挑不到东西说明槽位分配退化了"
    assert any("空桶回流" in note for note in report.slots[0].notes)
    for kind, expected in zip(kinds, wanted, strict=True):
        assert kind in (expected, SlotKind.high_score, SlotKind.branch_exploration,
                        SlotKind.random_novelty)
    assert all(row["slot_kind"] is None or row["status"] == "done"
               for row in harness.frontier() if row["status"] == "pending"), \
        "入队候选不应被钉死在某一类槽上"


# ------------------------------------------------------------------ 图与多分支


def test_three_layers_and_cycles_expand_without_requeueing_explored_words(workers):
    """三层分支、多父与环路：observed/generated 分得清，已探索的词不回队、不重复记功。"""
    harness = workers(seed="上海中考", suggest_attempts=24, search_attempts=4)
    report = harness.guarded_run(max_slots=6)

    depths = {int(row["depth"]) for row in harness.rows(
        "SELECT depth FROM run_keywords WHERE run_id = ?", (harness.run_id,))}
    assert {0, 1, 2, 3} <= depths, "六槽跑不出三层以上说明扩展没有真的接上"
    origins = {row["origin"] for row in harness.rows(
        "SELECT DISTINCT origin FROM run_keywords WHERE run_id = ?", (harness.run_id,))}
    assert {"seed", "observed", "generated"} <= origins

    edges = harness.rows(
        "SELECT parent_keyword_id AS p, child_keyword_id AS c, origin FROM discovery_edges"
        " WHERE run_id = ?", (harness.run_id,),
    )
    assert edges and all(edge["origin"] in ("observed", "generated") for edge in edges)
    pairs = {(int(edge["p"]), int(edge["c"])) for edge in edges}
    assert any((c, p) in pairs for p, c in pairs), "fixture 里的互相联想要真的形成环路"
    assert all(p != c for p, c in pairs), "自环不得入库"

    parents: dict[int, set[int]] = {}
    for edge in edges:
        parents.setdefault(int(edge["c"]), set()).add(int(edge["p"]))
    assert max(len(value) for value in parents.values()) >= 2, "多父汇聚没体现出来"

    seed_id = _seed_keyword_id(harness)
    seed_rows = [row for row in harness.frontier() if row["keyword_id"] == seed_id]
    assert len(seed_rows) == 1, "种子词被回环指回时不得再入一次队"
    assert seed_rows[0]["status"] == "done"
    assert harness.repo.run_keyword(harness.run_id, seed_id)["status"] == "explored"
    # 同一 (run, keyword, branch) 只有一个队列身份，重复发现走 upsert 而不是新建第二个身份
    assert harness.rows(
        "SELECT keyword_id FROM frontier WHERE run_id = ? GROUP BY keyword_id, branch_id HAVING COUNT(*) > 1",
        (harness.run_id,),
    ) == []
    assert all(slot.suggest.attempt_no == 1 for slot in report.slots)


def test_generated_and_observed_origins_never_impersonate_each_other(workers):
    """generated 候选登记时不冒充平台观察边，observed 边由联想入库自己写并带会话。"""
    harness = workers(seed="上海中考志愿填报", suggest_attempts=24)
    harness.guarded_run(max_slots=3)
    edges = harness.rows(
        "SELECT origin, session_id, operation_id, COUNT(*) AS n FROM discovery_edges"
        " WHERE run_id = ? GROUP BY origin, session_id, operation_id",
        (harness.run_id,),
    )
    counts: dict[str, int] = {}
    for row in edges:
        counts[row["origin"]] = counts.get(row["origin"], 0) + int(row["n"])
    assert counts.get("observed", 0) > 0, "联想子词必须留下 observed 边"
    assert counts.get("generated", 0) > 0, "算子产出的候选必须留下 generated 边"
    for row in edges:
        if row["origin"] == "observed":
            assert row["session_id"], "observed 边没有会话就无法复核是谁在什么时候看到的"
        else:
            assert row["session_id"] is None, "算子推导不是平台观察，不得写会话"


# ------------------------------------------------------------------ 校准衔接


def test_calibration_sample_uses_its_own_key_and_sequence(workers):
    """校准槽：独立预算键、CALIBRATION_SEQ_BASE 之后的序号段、不给探索记功、不做扩展。"""
    harness = workers(seed="上海中考", suggest_attempts=8, calibration_suggest_attempts=2)
    for text in ("上海中考名额分配", "上海中考自招", "上海中考分数线"):
        keyword_id = harness.repo.ensure_keyword(text, text, seen_at=utc_now())
        harness.repo.upsert_run_keyword(harness.run_id, keyword_id, depth=1, origin="observed",
                                        status="queued", seen_at=utc_now())
    harness.repo.enqueue(harness.run_id, keyword_id, "b-root",
                         selection_reason="校准候选入队（测试用）", queued_at=utc_now())

    report = harness.guarded_run(max_slots=2)
    assert report.calibration_samples == 2
    assert harness.engine.pool is not None and harness.engine.pool.size == 2
    assert harness.events(calib.POOL_EVENT)

    calibration_ops = harness.rows(
        "SELECT * FROM operations WHERE run_id = ? AND sample_kind = 'coverage_calibration'"
        " ORDER BY id", (harness.run_id,),
    )
    assert len(calibration_ops) == 2
    assert all(int(row["revisit_seq"]) >= CALIBRATION_SEQ_BASE for row in calibration_ops)
    assert all(row["slot_kind"] is None for row in calibration_ops)
    assert harness.used("calibration_suggest_attempts") == 2.0
    assert harness.used("suggest_attempts") == 2.0, "校准不能借用主动探索的额度"
    assert harness.ghosts() == 0

    sampled = harness.events("calibration_sampled")
    assert len(sampled) == 2
    assert "不给主动探索记功" in sampled[0]["detail"]["note"]
    window = stopping.collect_signals(harness.repo, harness.run_id, harness.settings, harness.ledger)
    assert window.window.observations == 2, "校准观察不得进收敛窗口"


def test_calibration_without_budget_never_touches_the_pool(workers):
    """没配校准预算就是 0 次校准：不冻结池、不占额度、不产生校准操作。"""
    harness = workers(seed="上海中考", suggest_attempts=8)
    report = harness.guarded_run(max_slots=1)
    assert report.calibration_samples == 0
    assert harness.engine.pool is None
    assert harness.events(calib.POOL_EVENT) == []
    assert harness.rows(
        "SELECT * FROM operations WHERE sample_kind = 'coverage_calibration'"
    ) == []


# ------------------------------------------------------------------ 契约守卫


def test_engine_refuses_a_ledger_belonging_to_another_run(workers):
    harness = workers(seed="上海中考")
    foreign = Ledger.from_settings(harness.conn, "run-other", harness.settings)
    with pytest.raises(ValueError, match="拒绝跨 run 记账"):
        DiscoveryEngine(
            paths=harness.paths, settings=harness.settings, run_id=harness.run_id,
            source=harness.source, repo=harness.repo, ledger=foreign, control=harness.control,
        )


def test_prepare_run_refuses_to_reuse_a_run_id(workers):
    harness = workers(seed="上海中考")
    with pytest.raises(ValueError, match="不允许用同一个 id 覆盖历史轨迹"):
        prepare_run(harness.repo, harness.settings, run_id=harness.run_id, created_at=utc_now())


def test_rebuilt_engine_from_engine_setup_matches_the_injected_one(workers):
    """EngineSetup 是重启与首启动共用的构造：Ledger 从库里读额度，WorkerControl 归同一把锁。"""
    harness = workers(seed="上海中考", suggest_attempts=8, search_attempts=2)
    harness.guarded_run(max_slots=1)
    spent = harness.used("suggest_attempts")

    setup = EngineSetup(
        paths=harness.paths, settings=harness.settings, run_id=harness.run_id,
        source=make_source(harness.settings), domain_terms=DOMAIN_TERMS,
        ambiguous_seeds=("上海",), locations=LOCATIONS,
    )
    harness.restart()
    rebuilt = setup.build(harness.repo)
    assert rebuilt.ledger.run_id == harness.run_id
    assert rebuilt.ledger.view("suggest_attempts").used == spent
    assert rebuilt.anchors == (harness.run_row()["seed_normalized"],)
    assert rebuilt.normalizer is not None and rebuilt.merger.analysis_version
    assert rebuilt.pool is None and rebuilt.slot_index == 0
    assert rebuilt.halt_error is None


def test_same_config_produces_the_same_trace_in_two_databases(workers):
    """确定性 fixture：同一份配置在两套库里跑出同一条轨迹（含预算、状态与簇计数）。"""
    first = workers(seed="上海中考", name="a", run_id="run-twin")
    second = workers(seed="上海中考", name="b", run_id="run-twin")
    reports = [first.guarded_run(max_slots=4), second.guarded_run(max_slots=4)]

    def trace(harness: Harness) -> list[tuple]:
        rows = harness.rows(
            "SELECT k.normalized_text, o.operation_type, o.revisit_seq, o.status,"
            " a.attempt_no, a.result_state FROM operations o"
            " JOIN keywords k ON k.id = o.keyword_id"
            " LEFT JOIN operation_attempts a ON a.operation_id = o.id"
            " WHERE o.run_id = ?"
            " ORDER BY k.normalized_text, o.operation_type, o.revisit_seq, a.attempt_no",
            (harness.run_id,),
        )
        return [tuple(sorted(row.items())) for row in rows]

    assert trace(first) == trace(second)
    assert [slot.as_dict() for slot in reports[0].slots] == [
        slot.as_dict() for slot in reports[1].slots
    ]
    for key in ("suggest_attempts", "search_attempts", "max_posts_stored"):
        assert first.used(key) == second.used(key)
    assert first.rows("SELECT COUNT(*) AS n FROM needs")[0]["n"] == \
        second.rows("SELECT COUNT(*) AS n FROM needs")[0]["n"]
