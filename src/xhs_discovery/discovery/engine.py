"""P0-C worker 主循环：把采集、入库、评分、调度、归并与停止串起来（P0-22 衔接点）。

一次槽位的固定顺序（顺序本身就是记账顺序，颠倒会算错奖励）：

    预留预算 → 开 attempt → 绑定逻辑键并访问 → 落原始产物 → 幂等入库 → 结算（显式 reservation_id）
    → 规则归并 → 该次操作奖励 → 算子扩展 → 候选评分入队

几条纪律：
- **重启重建**：`Repository`/`Ledger` 由进程启动时按 run 配置重建，额度只认库里的
  `budget_revisions`；先跑 `recover_run`（补导入 + 事实回填 + 释放幽灵预留），
  已有可靠观察的联想/卡片操作**不再发起外部访问**，只记一条缓存命中；
- **所有权先于访问**：每次外部访问前刷新租约心跳，租约被接走或 OS 互斥丢失立刻停下，
  僵尸 worker 不能继续花预算；`run_guarded()` 的 `finally` 一定释放两把锁；
- **失败重试要计费**：重试是新 attempt、新预留、新结算，`attempt_no` 进入成本分母，
  所以反复重试会压低奖励，但预算照样扣；不可重试的错误（登录、验证码）直接升级为暂停；
- **账跟着访问阶段走（C-10）**：一次 attempt 分 `not_started`（请求还没交出）/ `access`
  （已交出、结果没回来）/ `local`（结果已回来，只是落盘·入库失败）三段。只有前者或数据源
  明确抛 `AccessNotStarted` 才允许把预留退回可用池；**访问已发生或无法证明未发生一律照计**，
  落盘/入库失败与"源在调用中途炸了"都不能变成免费额度。每 attempt 至多一笔访问账
  （结算幂等 + 部分唯一索引兜底），崩溃恢复只对"已发起未记账"的 attempt 补记；
- **奖励只归本次选中的分支**，跨分支重复词保留发现边但不重复记功；联想操作只拿关键词奖励，
  失败/跳过的操作不得记任何新增奖励（模型层也会拒绝）；
- **校准与探索隔离**：校准样本走独立预算键、`sample_kind=coverage_calibration`，
  不给主动探索记功、不进收敛窗口。

数据源由调用方注入：P0-C 全程 synthetic fixture；P1-B 起同一套循环也驱动真实 CLI 数据源
（`run.mode=live`）。引擎不按模式分叉算法——只有两处按模式不同：真实模式的退避要真的等，
数据源交回的原始产物必须校验哈希/归属后登记（fixture 不产原始文件，自然跳过）。
重试/暂停的判据本身与模式无关，只看错误的类别与码。不接模型。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from xhs_discovery.config import Settings, load_intent_rules
from xhs_discovery.discovery import calibration as calib
from xhs_discovery.discovery import stopping
from xhs_discovery.discovery.cost import OperationCost, judge_relevance, score_tier
from xhs_discovery.discovery.extract import (
    EXTRACTOR_VERSION,
    extract_problems,
)
from xhs_discovery.discovery.graph import DiscoveryGraph
from xhs_discovery.discovery.ingest import (
    IngestedChild,
    PostIngest,
    SuggestionIngest,
    ingest_author_posts,
    ingest_body,
    ingest_search,
    ingest_suggestion,
    replayed_posts,
)
from xhs_discovery.discovery.merge import PreliminaryMerger
from xhs_discovery.discovery.operators import (
    ExpansionInput,
    OperatorSettings,
    expand,
    register_candidates,
)
from xhs_discovery.discovery.priority import (
    PriorityBatch,
    PriorityInput,
    evaluate_batch,
    with_branch_ucb,
)
from xhs_discovery.discovery.scheduler import (
    Scheduler,
    ScheduledSlot,
    load_branch_stats,
    normalize_ucb,
    ucb_values,
)
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    Capability,
    ErrorCategory,
    OperationState,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    SlotKind,
    TerminalOutcome,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.run import BranchStat, RewardUpdate, RunIdentity, RunTermination
from xhs_discovery.models.source import (
    AuthorPostsResult,
    KeywordText,
    OperationContext,
    PostBodyResult,
    PostSearchResult,
    SuggestionResult,
)
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.sources.base import AccessNotStarted, SearchSource
from xhs_discovery.storage.artifacts import LandedArtifact, recover_run, write_artifact
from xhs_discovery.storage.ledger import BudgetExhausted, Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import WorkerAlreadyRunning, WorkerControl, consume_all_controls
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.normalize import KeywordNormalizer

REPLAY_NOTE = "重放/恢复：该操作已有可靠观察，不再发起外部访问（只记缓存命中，不重复扣访问预算）"

# C22：落盘的规范化结果补入库（离线，不重发、不二次扣费）
REINGEST_NOTE = "崩溃恢复：规范化结果已落盘但未入库，离线补入库，复用原 attempt，不重发、不重复扣访问预算"
LANDED_INTEGRITY_CODE = "LANDED_RESULT_INVALID"

# 规范化信封（`{operation_type.value}.json`）的解析契约：按操作类型还原成对应模型，
# 绝不把 CLI 原始 stdout（`cli/cli_stdout.json`）当作规范化结果——两者不同名、不同目录，
# 这里只认按契约模型可解析、且 context/session 身份都对得上的规范化信封。
_NORMALIZED_CONTRACT_BY_OP: dict[OperationType, type] = {
    OperationType.suggest: SuggestionResult,
    OperationType.search: PostSearchResult,
    OperationType.body: PostBodyResult,
    OperationType.author_posts: AuthorPostsResult,
}

# 校准与主动探索可能碰同一个关键词的同一个逻辑键（operations 的 UNIQUE 不含 sample_kind），
# 所以校准用独立的序号段，两边才不会互相冒充对方的操作行。
CALIBRATION_SEQ_BASE = 1000

# 需要人处理或环境阻塞的错误：重试解决不了，直接把 run 停在 paused 并给出原因
HALT_CATEGORIES = frozenset(
    {ErrorCategory.auth, ErrorCategory.action_required, ErrorCategory.profile_busy}
)

# 只有"网络短暂抖动"才允许有限重试（每次重试都是新 attempt、新预留、新结算，照样占预算）。
# profile 占用虽在分类表里标了可重试，但重试等于跟上一个还没退出浏览器抢同一个 profile；
# 页面/选择器/输出类错误重试只会反复撞同一处坏页面——这些都直接进暂停。
RETRY_CATEGORIES = frozenset({ErrorCategory.network})

# 进程级超时/取消：无法确认 CLI 派生的那个浏览器已经退出，重试就是立刻起第二个浏览器。
# 一律不重试、显式暂停，保留严格核验过的 partial 与原始响应等人处置。
UNCERTAIN_PROCESS_CODES = frozenset({"CLI_PROCESS_TIMEOUT", "CLI_CANCELLED"})

# 异常发生在哪个阶段，就按哪个阶段的性质归类（C-10）：
# 本地派发失败是用法问题；访问途中炸掉是系统故障；结果返回后落盘/入库失败是产出故障。
FAILURE_PHASE_CATEGORIES = {
    "not_started": ErrorCategory.usage,
    "access": ErrorCategory.system,
    "local": ErrorCategory.output,
}


@dataclass
class OperationReceipt:
    """一次操作的记账凭证：状态、尝试数、结算行与产物路径都要能追到。"""

    operation_id: int
    keyword_id: int
    operation_type: OperationType
    revisit_seq: int
    branch_key: str
    slot_kind: SlotKind | None
    sample_kind: CalibrationSampleKind
    budget_key: str
    attempt_no: int = 0
    state: ResultState = ResultState.skipped
    settlement_ids: list[int] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    cache_hit: bool = False
    replayed: bool = False
    error: ErrorInfo | None = None

    @property
    def succeeded(self) -> bool:
        return self.state in (ResultState.completed, ResultState.partial)


@dataclass
class OperationOutcome:
    receipt: OperationReceipt
    ingest: object | None = None
    result: object | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class SlotOutcome:
    index: int
    frontier_id: int
    keyword_id: int
    normalized: str
    branch_key: str
    slot_kind: SlotKind
    suggest: OperationReceipt
    search: OperationReceipt | None
    bodies: list[OperationReceipt]
    author: OperationReceipt | None
    suggest_reward: float
    content_reward: float
    new_valid_clusters: int
    new_related_keywords: int
    registered: int
    dropped_irrelevant: list[str]
    notes: list[str]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "keyword_id": self.keyword_id,
            "normalized": self.normalized,
            "branch_key": self.branch_key,
            "slot_kind": self.slot_kind.value,
            "suggest_state": self.suggest.state.value,
            "suggest_attempts": self.suggest.attempt_no,
            "search_state": None if self.search is None else self.search.state.value,
            "body_ops": len(self.bodies),
            "author_ops": 0 if self.author is None else 1,
            "suggest_reward": self.suggest_reward,
            "content_reward": self.content_reward,
            "new_valid_clusters": self.new_valid_clusters,
            "new_related_keywords": self.new_related_keywords,
            "registered": self.registered,
            "dropped_irrelevant": list(self.dropped_irrelevant),
            "notes": list(self.notes),
        }


@dataclass
class EngineReport:
    run_id: str
    slots: list[SlotOutcome] = field(default_factory=list)
    calibration_samples: int = 0
    termination: stopping.StopDecision | None = None
    recovered: dict = field(default_factory=dict)
    events: list[str] = field(default_factory=list)

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    def as_dict(self) -> dict:
        decision = self.termination
        return {
            "run_id": self.run_id,
            "slots": self.slot_count,
            "calibration_samples": self.calibration_samples,
            "status": None if decision is None else decision.status.value,
            "outcome": None if decision is None or decision.outcome is None else decision.outcome.value,
            "pause_reason": (
                None if decision is None or decision.pause_reason is None else decision.pause_reason.value
            ),
            "note": None if decision is None else decision.note,
            "signals": None if decision is None else decision.signals.as_dict(),
            "recovered": dict(self.recovered),
            "events": list(self.events),
        }


def _json_payload(envelope) -> str:
    """原始产物写数据源返回的可解析表示，字段与契约模型一致，不另造私有格式。"""
    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)


class DiscoveryEngine:
    """一个 run 一个实例，且必须在持有 worker 锁的临界区内运行。"""

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        settings: Settings,
        run_id: str,
        source: SearchSource,
        repo: Repository,
        ledger: Ledger,
        control: WorkerControl,
        domain_terms: tuple[str, ...] = (),
        anchors: tuple[str, ...] | None = None,
        ambiguous_seeds: tuple[str, ...] = (),
        locations: tuple[str, ...] = (),
        matcher: IntentMatcher | None = None,
        retry_sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if ledger.run_id != run_id:
            raise ValueError("Ledger 属于另一个 run，引擎拒绝跨 run 记账")
        self.paths = paths
        self.settings = settings
        self.run_id = run_id
        self.live_mode = settings.run.mode == "live"
        # 真实模式的退避必须真的等（否则"有限重试"变成对着同一台浏览器连续猛敲）；
        # fixture/单测注入一个记录用的 sleeper，不靠真实时间。
        self._retry_sleeper = retry_sleeper or time.sleep
        self.source = source
        self.repo = repo
        self.ledger = ledger
        self.control = control
        self.conn = repo.conn
        self.domain_terms = tuple(domain_terms)
        run = repo.require_run(run_id)
        self.normalizer = KeywordNormalizer(ambiguous_seeds=ambiguous_seeds, locations=locations)
        self.anchors = anchors or (str(run["seed_normalized"]),)
        self.matcher = matcher or IntentMatcher(
            load_intent_rules(Path(settings.nlp.intent_rules_path))
        )
        self.graph = DiscoveryGraph(repo, run_id)
        self.merger = PreliminaryMerger(repo, run_id, extractor_version=EXTRACTOR_VERSION)
        self.scheduler = Scheduler(repo, run_id, settings)
        self.operator_settings = OperatorSettings.from_settings(settings, self.domain_terms)
        self.pool: calib.CalibrationPool | None = None
        self.slot_index = 0
        self.halt_error: ErrorInfo | None = None

    # ------------------------------------------------------------------ 基础设施

    def _assert_ownership(self) -> None:
        """外部访问前的所有权检查：心跳失败即停止，僵尸 worker 不得继续花预算。"""
        self.control.heartbeat()

    def _budget_key(self, operation_type: OperationType, sample_kind: CalibrationSampleKind) -> str:
        if sample_kind is CalibrationSampleKind.coverage_calibration:
            return calib.CALIBRATION_BUDGET_KEY
        return stopping.ACTION_BUDGET_KEYS[operation_type]

    def _next_attempt_no(self, operation_id: int) -> int:
        rows = self.repo.attempts(operation_id)
        return max((int(row["attempt_no"]) for row in rows), default=0) + 1

    def _next_seq_for(self, keyword_id: int, operation_type: OperationType) -> int:
        """同一关键词的同类多次操作（如多篇正文）各占一个逻辑键序号。

        `operations` 的逻辑键是 (run, keyword, operation_type, revisit_seq)，不含 note_id；
        正文按篇发起，所以用该关键词下该类操作的下一个序号，避免两篇正文撞同一操作行、
        被误判成"已有可靠观察"而跳过真实采集。
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(revisit_seq), -1) AS m FROM operations"
            " WHERE run_id = ? AND keyword_id = ? AND operation_type = ?",
            (self.run_id, keyword_id, operation_type.value),
        ).fetchone()
        return int(row["m"]) + 1

    def _existing_observation(self, operation_id: int) -> tuple[str, dict] | None:
        """该操作是否已有可靠观察：决定重放要不要真的访问平台。

        正文也要查：v3 起 `body_observations` 带 operation_id，崩溃恢复后同一操作再被驱动时，
        已经落库的正文观察应当走缓存命中（0 单位、不再访问），否则一次读取会记两条观察、扣两次额度。
        """
        for table in ("suggestion_observations", "post_observations", "body_observations"):
            row = self.conn.execute(
                f"SELECT id, state FROM {table} WHERE operation_id = ?"
                " AND state IN ('completed', 'partial') ORDER BY id LIMIT 1",
                (operation_id,),
            ).fetchone()
            if row is not None:
                return table, dict(row)
        return None

    def _load_landed_normalized(
        self, operation_id: int, *, operation_type: OperationType,
        keyword_id: int, revisit_seq: int,
    ) -> tuple[str, object | None, int, int, str] | None:
        """找回本操作已落盘但未入库的规范化信封（`{op}.json`），不碰 CLI 原始 stdout。

        返回 (状态, 结果或 None, attempt_id, attempt_no, rel_path)；状态：
        - `no_candidate`/None：没有规范化信封 → 走正常访问；
        - `missing_file`：库里有登记但文件已不可用 → 记不可用后交上层标 uncertain，绝不重发；
        - `invalid`：文件被改、身份归属对不上或不可按契约解析 → 交上层标 uncertain，绝不重发；
        - `ok`：拿到可核验的规范化结果。
        只认 basename 恰为 `{operation_type.value}.json` 且不在 `cli/` 目录下的 stdout_json 行：
        `cli/cli_stdout.json` 是原始工具响应，永不作为规范化结果解析。
        身份归属以**库内登记**为唯一权威（绝不拿产物自述当基准）：操作行必须就是本 run 本键、
        登记的 attempt 行必须存在且属于该操作、落盘路径必须逐字等于该 attempt 的规范目录
        （`runs/<run>/operations/k<词>/<op>/r<重探>/a<尝试>/<op>.json`）；信封里声明的尝试序号
        是否与登记一致，交 `_landed_integrity_reason` 用这里的登记值核验。
        """
        want = f"{operation_type.value}.json"
        rows = self.conn.execute(
            "SELECT attempt_id, rel_path, sha256, size_bytes FROM raw_artifacts"
            " WHERE operation_id = ? AND kind = 'stdout_json' AND attempt_id IS NOT NULL"
            " ORDER BY id",
            (operation_id,),
        ).fetchall()
        candidates = [
            row for row in rows
            if str(row["rel_path"]).rsplit("/", 1)[-1] == want
            and "/cli/" not in str(row["rel_path"])
        ]
        if not candidates:
            return None
        op_row = self.conn.execute(
            "SELECT run_id, keyword_id, operation_type, revisit_seq FROM operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        if (
            op_row is None
            or str(op_row["run_id"]) != self.run_id
            or int(op_row["keyword_id"]) != keyword_id
            or str(op_row["operation_type"]) != operation_type.value
            or int(op_row["revisit_seq"]) != revisit_seq
        ):
            # 驱动键与库中操作行都对不上：这块"证据"根本不属于本次操作
            return ("invalid", None, int(candidates[-1]["attempt_id"]), 0,
                    str(candidates[-1]["rel_path"]))
        row = candidates[-1]
        rel = str(row["rel_path"])
        attempt_id = int(row["attempt_id"])
        attempt = self.conn.execute(
            "SELECT attempt_no, operation_id FROM operation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if attempt is None or int(attempt["operation_id"]) != operation_id:
            # 登记的 attempt 行不存在或不属于本操作：产物是孤行/外来引用，身份无从核对
            return ("invalid", None, attempt_id, 0, rel)
        attempt_no = int(attempt["attempt_no"])
        canonical_rel = self.paths.relative(
            self.paths.attempt_dir(self.run_id, keyword_id, operation_type, revisit_seq, attempt_no)
        ) + "/" + want
        if rel != canonical_rel:
            # 路径不在本 run/操作/尝试的规范目录：挪了位置的字节再"完好"也不是本次操作的结果
            return ("invalid", None, attempt_id, attempt_no, rel)
        try:
            target = self.paths.resolve_within_run(rel, self.run_id)
        except ValueError:
            return ("invalid", None, attempt_id, attempt_no, rel)
        if not target.is_file():
            self.repo.record_event(
                self.run_id, "normalized_artifact_unavailable",
                detail={"operation_id": operation_id, "rel_path": rel}, at=utc_now(),
            )
            return ("missing_file", None, attempt_id, attempt_no, rel)
        body = target.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != str(row["sha256"]) or len(body) != int(row["size_bytes"]):
            return ("invalid", None, attempt_id, attempt_no, rel)
        model = _NORMALIZED_CONTRACT_BY_OP[operation_type]
        try:
            result = model.model_validate(json.loads(body.decode("utf-8")))
        except Exception:  # noqa: BLE001 - 不可按契约解析即视为证据损坏，交上层判 uncertain
            return ("invalid", None, attempt_id, attempt_no, rel)
        return ("ok", result, attempt_id, attempt_no, rel)

    def _attempt_raw_response_bodies(self, attempt_id: int) -> tuple[list[bytes], str | None]:
        """读回本 attempt 登记的原始 stdout 响应字节，作为恢复核验的来源版本证据。

        只认本 attempt 名下 `cli/` 目录里的 stdout_json（`cli_stdout.json` 等工具原始响应）；
        规范化信封本身不是证据。库里登记（sha/size/路径/attempt 归属）是唯一权威：任何一份
        登记响应文件缺失、路径解析不回本 run、或字节与登记哈希/大小不符，都是证据损坏——
        返回原因交上层标 uncertain，核验本身绝不建立在可疑证据上"尽量通过"。
        """
        rows = self.conn.execute(
            "SELECT rel_path, sha256, size_bytes FROM raw_artifacts"
            " WHERE attempt_id = ? AND kind = 'stdout_json' ORDER BY id",
            (attempt_id,),
        ).fetchall()
        bodies: list[bytes] = []
        for row in rows:
            rel = str(row["rel_path"])
            if "/cli/" not in rel:
                continue
            try:
                target = self.paths.resolve_within_run(rel, self.run_id)
            except ValueError:
                return [], f"本 attempt 登记的原始响应路径无法解析回本 run：{rel}"
            if not target.is_file():
                return [], f"本 attempt 登记的原始响应文件已不可用：{rel}"
            body = target.read_bytes()
            if hashlib.sha256(body).hexdigest() != str(row["sha256"]) or len(body) != int(row["size_bytes"]):
                return [], f"本 attempt 的原始响应字节与登记的哈希/大小不符：{rel}"
            bodies.append(body)
        return bodies, None

    def _landed_integrity_reason(
        self, result, *, keyword_id: int, operation_type: OperationType,
        revisit_seq: int, attempt_id: int, attempt_no: int
    ) -> str | None:
        """核验落盘结果的 context 身份与 session 来源；任何对不上都是不可信证据，不得补入库。

        基准全部来自**库里登记与恢复后重建的数据源实例**（快照已核对来源）：产物自述的
        run/关键词/操作/重探/尝试序号与来源标识只有与基准逐字一致才可信；来源版本不校验格式，
        而是由本 attempt 登记的原始响应证据重新派生（引擎已按登记 sha/size 核对字节）。
        来源口径由各适配器 的 `session_matches_source` 定义，引擎不写死任何来源字面量。
        """
        ctx = result.context
        if ctx.run_id != self.run_id:
            return "落盘结果的 run_id 与本 run 不一致"
        if ctx.keyword_id != keyword_id:
            return "落盘结果的 keyword_id 与操作归属不一致"
        if ctx.operation_type is not operation_type:
            return "落盘结果的操作类型与操作归属不一致"
        if ctx.revisit_seq != revisit_seq:
            return "落盘结果的重探序号与操作归属不一致"
        if ctx.attempt_no != attempt_no:
            return "落盘结果声明的尝试序号与登记的 attempt 不一致"
        session = result.session
        if not session.source_name or not session.source_version:
            return "落盘结果缺少来源标识（source_name/source_version）"
        raw_bodies, evidence_reason = self._attempt_raw_response_bodies(attempt_id)
        if evidence_reason is not None:
            return evidence_reason
        if not self.source.session_matches_source(session, raw_responses=raw_bodies):
            return "落盘结果的来源标识与固化快照重建数据源、本 attempt 登记原始响应派生的身份不一致"
        expected_synthetic = bool(int(self.repo.require_run(self.run_id)["synthetic_fixture"]))
        if bool(session.synthetic) is not expected_synthetic:
            return "落盘结果的 session.synthetic 与 run 的合成标记不一致"
        return None

    def _apply_operation_status_from_state(self, operation_id: int, receipt) -> None:
        """按结果状态落 operation：与正常访问尾部同一套映射，恢复路径不另判一套。"""
        if receipt.state is ResultState.failed:
            self.repo.set_operation_status(operation_id, OperationState.failed)
        elif receipt.succeeded:
            self.repo.set_operation_status(operation_id, OperationState.completed)
        elif receipt.state is ResultState.skipped:
            self.repo.set_operation_status(operation_id, OperationState.skipped)
        elif receipt.state is ResultState.unsupported:
            self.repo.set_operation_status(operation_id, OperationState.unsupported)

    def _attempt_has_observation(self, operation_id: int, attempt_id: int) -> bool:
        """该 attempt 是否已经产出观察行：有则落盘信封只是历史证据，不属于"落盘未入库"的崩溃窗口。"""
        for table in ("suggestion_observations", "post_observations", "body_observations"):
            row = self.conn.execute(
                f"SELECT 1 FROM {table} WHERE operation_id = ? AND attempt_id = ? LIMIT 1",
                (operation_id, attempt_id),
            ).fetchone()
            if row is not None:
                return True
        return False

    def _maybe_reingest_landed(
        self, *, operation_id: int, keyword_id: int, operation_type: OperationType,
        revisit_seq: int, attach, budget_key: str, receipt, outcome,
    ):
        """崩溃时规范化结果已落盘却没入库：离线补入库，复用原 attempt，不重发、不二次扣费。

        只在没有可靠观察、也还没进入访问循环时触发；找到坏证据就标 uncertain 并暂停，
        宁可停住也绝不按当前口径重发覆盖旧批次。
        """
        loaded = self._load_landed_normalized(
            operation_id, operation_type=operation_type,
            keyword_id=keyword_id, revisit_seq=revisit_seq,
        )
        if loaded is None:
            return None
        status, result, attempt_id, attempt_no, rel = loaded
        if status == "no_candidate":
            return None
        # 已登记信封不可用（missing_file）或损坏/身份不符（invalid / ok 但 integrity 失败）：
        # 一律按不确定停住，绝不重发覆盖旧批次——"原始结果不可用"要的是承认不确定 + 走 C10 既有
        # 记账，而不是把它当成"没人占着"再敲一次 CLI、二次扣费。
        if status == "missing_file" or status == "invalid" or (
            status == "ok"
            and (reason := self._landed_integrity_reason(
                result, keyword_id=keyword_id, operation_type=operation_type,
                revisit_seq=revisit_seq, attempt_id=attempt_id, attempt_no=attempt_no,
            )) is not None
        ):
            if status == "missing_file":
                reason = "已登记的规范化结果文件已不可用，且无同名可核验副本"
            elif status == "invalid":
                reason = "落盘的规范化结果损坏或与操作身份不一致"
            self.repo.set_operation_status(operation_id, OperationState.uncertain)
            self.halt_error = ErrorInfo(
                code=LANDED_INTEGRITY_CODE,
                message=f"{reason}：{rel}",
                category=ErrorCategory.output,
                retryable=False,
                human_action_required=True,
                detail={"operation_id": str(operation_id), "rel_path": rel},
            )
            self.repo.record_event(
                self.run_id, "landed_result_invalid",
                detail={"operation_id": operation_id, "rel_path": rel, "reason": reason,
                        "status": status},
                at=utc_now(),
            )
            receipt.state = ResultState.failed
            receipt.error = self.halt_error
            receipt.cache_hit = True
            outcome.notes = [reason]
            return outcome
        if self._attempt_has_observation(operation_id, attempt_id):
            # 该 attempt 已经入库：落盘信封只是历史证据，不是"落盘未入库"的崩溃窗口，交正常流程
            return None
        ingest, notes = attach(result.context, result, operation_id, attempt_id)
        self.repo.mark_operation_started(operation_id)
        self.ledger.record_cache_hit(
            key=budget_key, reason=REINGEST_NOTE, operation_id=operation_id
        )
        receipt.attempt_no = attempt_no
        receipt.state = result.state
        receipt.error = result.error
        receipt.cache_hit = True
        outcome.ingest = ingest
        outcome.result = result
        outcome.notes = [REINGEST_NOTE, *notes]
        self._apply_operation_status_from_state(operation_id, receipt)
        self.repo.record_event(
            self.run_id, "landed_result_reingested",
            detail={"operation_id": operation_id, "attempt_id": attempt_id,
                    "attempt_no": attempt_no, "state": result.state.value,
                    "rel_path": rel, "access": False},
            at=utc_now(),
        )
        if result.error is not None and self._blocking_error(result):
            self.halt_error = result.error
        return outcome

    def _keyword_text(self, keyword_id: int) -> KeywordText:
        """原始写法从 keyword_aliases 取（平台原样保留），没有别名就用规范化文本。"""
        row = self.conn.execute(
            "SELECT raw_text FROM keyword_aliases WHERE keyword_id = ? ORDER BY id DESC LIMIT 1",
            (keyword_id,),
        ).fetchone()
        normalized = self.conn.execute(
            "SELECT normalized_text FROM keywords WHERE id = ?", (keyword_id,)
        ).fetchone()
        if normalized is None:
            raise ValueError(f"关键词 {keyword_id} 不在全局身份层")
        raw = str(row["raw_text"]) if row is not None else str(normalized["normalized_text"])
        return KeywordText(raw=raw, normalized=str(normalized["normalized_text"]))

    # ------------------------------------------------------------------ 单次操作

    def _register_source_artifacts(
        self,
        result,
        *,
        operation_id: int,
        attempt_id: int,
        keyword_id: int,
        operation_type: OperationType,
        revisit_seq: int,
        attempt_no: int,
    ) -> list[LandedArtifact]:
        """把数据源交回的原始产物校验后登记到本 run 本 attempt（P1-B：DB 归属接线）。

        只认落在**本 attempt 的 `cli/` 目录**下、路径可解析回本 run、且哈希与字节数都与数据源
        声明一致的文件：任何一个条件不满足都抛异常 → 按 C-10 的 `local` 阶段照计这笔访问
        （结果已经回来了，本地登记失败不能变成免费额度）。登记走内容寻址的 `write_artifact`，
        同一 attempt 重复登记同内容天然幂等，跨 run/重探/重试各在自己的目录，不覆盖历史。

        fixture 数据源不产出原始文件（`raw_artifacts` 为空），这里自然什么都不做。
        """
        refs = list(getattr(result, "raw_artifacts", None) or [])
        if not refs:
            return []
        attempt_dir = self.paths.attempt_dir(
            self.run_id, keyword_id, operation_type, revisit_seq, attempt_no
        )
        required_prefix = self.paths.relative(attempt_dir / "cli") + "/"
        registered: list[LandedArtifact] = []
        for ref in refs:
            rel = str(ref.path)
            if not rel.startswith(required_prefix):
                raise ValueError(
                    f"数据产物 {rel} 不在本 attempt 的 cli 目录（{required_prefix}）下："
                    "归属不明的文件不登记，更不能当作本次访问的结果"
                )
            target = self.paths.resolve_within_run(rel, self.run_id)
            if not target.is_file():
                raise ValueError(f"数据产物声明的文件不存在：{rel}")
            body = target.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if digest != ref.sha256 or len(body) != int(ref.size_bytes):
                raise ValueError(
                    f"数据产物与声明不一致：{rel}（实测哈希 {digest[:12]}… ≠ 声明 "
                    f"{str(ref.sha256)[:12]}…，实测 {len(body)} 字节 ≠ 声明 {ref.size_bytes}）"
                )
            registered.append(
                write_artifact(
                    self.conn,
                    self.paths,
                    self.run_id,
                    kind=ref.kind,
                    payload=body,
                    rel_path=rel,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                )
            )
        return registered

    def _execute(
        self,
        *,
        operation_type: OperationType,
        keyword_id: int,
        branch_key: str,
        revisit_seq: int,
        slot_kind: SlotKind | None,
        sample_kind: CalibrationSampleKind,
        access: Callable[[OperationContext], object],
        attach: Callable[[OperationContext | None, object, int, int], tuple[object | None, list[str]]],
        reason: str,
        skip_reason: str | None = None,
    ) -> OperationOutcome:
        """跑一次操作的全套记账；`attach` 负责入库并返回 (ingest, notes)。

        `skip_reason` 非空表示本轮决定不发起该操作（预算/能力/档位），显式记 skipped，
        不伪装成"平台没返回"。
        """
        operation_id = self.repo.open_operation(
            self.run_id,
            keyword_id,
            operation_type,
            revisit_seq,
            branch_key=branch_key,
            slot_kind=slot_kind,
            sample_kind=sample_kind,
        )
        budget_key = self._budget_key(operation_type, sample_kind)
        receipt = OperationReceipt(
            operation_id=operation_id,
            keyword_id=keyword_id,
            operation_type=operation_type,
            revisit_seq=revisit_seq,
            branch_key=branch_key,
            slot_kind=slot_kind,
            sample_kind=sample_kind,
            budget_key=budget_key,
        )
        outcome = OperationOutcome(receipt=receipt)

        if skip_reason is not None:
            self.repo.set_operation_status(operation_id, OperationState.skipped)
            receipt.error = ErrorInfo(
                code="ACTION_NOT_PERMITTED",
                message=skip_reason,
                category=ErrorCategory.budget,
                retryable=False,
                human_action_required=False,
            )
            outcome.notes = [skip_reason]
            return outcome

        replay = self._existing_observation(operation_id)
        if replay is not None:
            self.ledger.record_cache_hit(
                key=budget_key, reason=REPLAY_NOTE, operation_id=operation_id
            )
            self.repo.mark_operation_started(operation_id)
            receipt.cache_hit = True
            receipt.replayed = True
            receipt.state = ResultState(str(replay[1]["state"]))
            receipt.attempt_no = max(self._next_attempt_no(operation_id) - 1, 1)
            ingest, notes = attach(None, replay[1], operation_id, 0)
            outcome.ingest = ingest
            outcome.notes = [REPLAY_NOTE, *notes]
            self.repo.set_operation_status(operation_id, OperationState.completed)
            return outcome

        reingested = self._maybe_reingest_landed(
            operation_id=operation_id,
            keyword_id=keyword_id,
            operation_type=operation_type,
            revisit_seq=revisit_seq,
            attach=attach,
            budget_key=budget_key,
            receipt=receipt,
            outcome=outcome,
        )
        if reingested is not None:
            return reingested

        max_attempts = 1 + self.settings.retry.max_retries
        attempt_no = self._next_attempt_no(operation_id)
        if attempt_no > max_attempts:
            self.repo.set_operation_status(operation_id, OperationState.skipped)
            outcome.notes = [
                f"操作 {operation_id} 历史尝试已达上限 {max_attempts}，本次不再发起外部访问；"
                "要重做得有新的重探序号"
            ]
            return outcome

        self.repo.mark_operation_started(operation_id)
        keyword = self._keyword_text(keyword_id)
        while attempt_no <= max_attempts:
            reservation_id: int | None = None
            try:
                self._assert_ownership()
                reservation_id = self.ledger.reserve(budget_key, 1, operation_id=operation_id)
            except BudgetExhausted as exc:
                receipt.state = ResultState.skipped
                receipt.error = ErrorInfo(
                    code="BUDGET_EXHAUSTED",
                    message=str(exc),
                    category=ErrorCategory.budget,
                    retryable=False,
                    human_action_required=False,
                )
                self.repo.set_operation_status(operation_id, OperationState.skipped)
                outcome.notes = [f"预算 {budget_key} 已耗尽，本次操作未发起外部访问：{exc}"]
                return outcome

            attempt_id = self.repo.start_attempt(operation_id, attempt_no)
            context = OperationContext(
                run_id=self.run_id,
                keyword_id=keyword_id,
                keyword=keyword,
                operation_type=operation_type,
                revisit_seq=revisit_seq,
                attempt_no=attempt_no,
                budget_reservation_id=reservation_id,
            )
            phase = "not_started"
            landed: LandedArtifact | None = None
            source_artifacts: list[LandedArtifact] = []
            try:
                # C-10：一次 attempt 分三个阶段，账必须跟着阶段走，不能"一视同仁地退款"：
                #   not_started —— 请求还没交出去（绑定会话前的本地派发失败，或数据源用
                #                `AccessNotStarted` 明确证明没发出）：能证明没访问 → 退回预留；
                #   access      —— 请求已交出、结果没回来或回来前炸了：无法证明平台没收到
                #                那次访问 → 按计划保守计费，退款等于把真实成本抹掉；
                #   local       —— 结果已返回，只是落盘/入库失败：访问确实发生了 → 照计，
                #                本地故障绝不能变成"免费的第二次机会"。
                # 结算按 (operation_id, attempt_no) 幂等，库里还有部分唯一索引兜底，
                # 所以一次 attempt 至多一笔访问账；崩溃恢复只对"已发起未记账"的 attempt 补记。
                if hasattr(self.source, "bind"):
                    self.source.bind(context)  # 只做本地会话绑定，不发起访问
                phase = "access"
                result = access(context)
                phase = "local"
                # 先按数据源交回的清单逐个校验并登记（原始 stdout/stderr/工具 out/元数据），
                # 再写规范化信封：两类产物各占一行，规范化结果永远不能冒充原始响应。
                source_artifacts = self._register_source_artifacts(
                    result,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    keyword_id=keyword_id,
                    operation_type=operation_type,
                    revisit_seq=revisit_seq,
                    attempt_no=attempt_no,
                )
                receipt.artifact_paths.extend(item.rel_path for item in source_artifacts)
                landed = write_artifact(
                    self.conn,
                    self.paths,
                    self.run_id,
                    kind="stdout_json",
                    payload=_json_payload(result),
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    filename=f"{operation_type.value}.json",
                )
                receipt.artifact_paths.append(landed.rel_path)
                ingest, notes = attach(context, result, operation_id, attempt_id)
            except Exception as exc:
                self._close_failed_attempt(
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    attempt_no=attempt_no,
                    operation_type=operation_type,
                    budget_key=budget_key,
                    reservation_id=reservation_id,
                    reason=reason,
                    phase=phase,
                    exc=exc,
                    landed=landed,
                    landed_source_count=sum(1 for item in source_artifacts if item.row_inserted),
                )
                raise
            if result.error is not None and result.error.detail.get("external_access") == "not_started":
                # The adapter's local profile guard proved that no child was
                # started. Keep the failed observation and pause, but refund.
                self.ledger.release(reservation_id, operation_id=operation_id, key=budget_key)
                settled = None
                self.repo.record_event(self.run_id, "attempt_refunded_not_accessed", detail={
                    "operation_id": operation_id, "attempt_id": attempt_id,
                    "attempt_no": attempt_no, "budget_key": budget_key,
                    "access_phase": "local_preflight", "access_charged": False,
                })
            else:
                settled = self._settle_access(
                    operation_id=operation_id,
                    operation_type=operation_type,
                    attempt_no=attempt_no,
                    budget_key=budget_key,
                    reason=reason,
                    reservation_id=reservation_id,
                )
            if settled is not None:
                receipt.settlement_ids.append(settled)
            self.repo.finish_attempt(
                attempt_id,
                result_state=result.state,
                error_code=None if result.error is None else result.error.code,
                error=None if result.error is None else result.error.model_dump(mode="json"),
                exit_code=None if result.error is None else result.error.exit_code,
                raw_artifact_count=sum(1 for item in source_artifacts if item.row_inserted)
                + (1 if landed.row_inserted else 0),
            )
            receipt.attempt_no = attempt_no
            receipt.state = result.state
            receipt.error = result.error
            outcome.ingest = ingest
            outcome.result = result
            outcome.notes = list(notes)

            planned_backoff = round(
                self.settings.retry.backoff_base_seconds * (2 ** (attempt_no - 1)), 3
            )
            retryable = (
                result.state is ResultState.failed
                and result.error is not None
                and result.error.retryable
                and attempt_no < max_attempts
                and not self._retry_blocked(result.error)
            )
            if retryable:
                # 有限退避：真实模式**必须真的等够**再发起下一次访问（fixture 注入 sleeper，
                # 不靠真实时间）；计划与实际等待都留在事件里可复核。
                slept = 0.0
                if self.live_mode and planned_backoff > 0:
                    self._retry_sleeper(planned_backoff)
                    slept = planned_backoff
                self.repo.record_event(
                    self.run_id,
                    "retry_backoff",
                    detail={
                        "operation_id": operation_id,
                        "attempt_no": attempt_no,
                        "planned_seconds": planned_backoff,
                        "slept_seconds": slept,
                        "mode": self.settings.run.mode,
                        "error_code": result.error.code,
                        "note": (
                            "真实模式已实际等待退避后才发起下一次访问"
                            if slept
                            else "fixture/注入时钟不真的睡；重试同样占预算（新 attempt、新预留、新结算）"
                        ),
                    },
                )
                attempt_no = self._next_attempt_no(operation_id)
                continue
            if result.error is not None and result.state in (
                ResultState.failed,
                ResultState.partial,
            ):
                if self._blocking_error(result):
                    self.halt_error = result.error
            break
        if receipt.state is ResultState.failed:
            self.repo.set_operation_status(operation_id, OperationState.failed)
        elif receipt.succeeded:
            self.repo.set_operation_status(operation_id, OperationState.completed)
        elif receipt.state is ResultState.skipped:
            self.repo.set_operation_status(operation_id, OperationState.skipped)
        elif receipt.state is ResultState.unsupported:
            self.repo.set_operation_status(operation_id, OperationState.unsupported)
        return outcome

    def _retry_blocked(self, error: ErrorInfo) -> bool:
        """这次失败允不允许再试一次：只允许"网络短暂抖动"。

        分类表里还有几项 `retryable=True`，但它们重试解决不了问题：
        - `profile_busy`：上一个浏览器还占着同一个 profile，重试就是跟它抢；
        - 页面/选择器类：同一处坏页面重敲一遍还是坏；
        - 进程超时/取消：无法确认 CLI 派生的浏览器是否已退出，重试等于立刻起第二个浏览器。
        """
        if error.code in UNCERTAIN_PROCESS_CODES:
            return True
        return error.category not in RETRY_CATEGORIES

    def _blocking_error(self, result) -> bool:
        """这个错误要不要把 run 停在 paused：等人、等环境，或无法确认浏览器已退出。

        partial 也照样判：一次带阻断错误的 partial 里，已经拿到的证据仍然入库并保留，
        但整个 run 不能当作"一切正常"继续往下敲。
        """
        error = result.error
        if error is None:
            return False
        if error.human_action_required or error.category in HALT_CATEGORIES:
            return True
        if error.code in UNCERTAIN_PROCESS_CODES:
            return True
        return error.category is ErrorCategory.page

    def _settle_access(
        self,
        *,
        operation_id: int,
        operation_type: OperationType,
        attempt_no: int,
        budget_key: str,
        reason: str,
        reservation_id: int | None,
    ) -> int | None:
        """结算一次外部访问；同一 (操作, attempt) 幂等，库里还有部分唯一索引兜底。"""
        return self.ledger.settle_external(
            operation_id=operation_id,
            operation_type=operation_type,
            attempt_no=attempt_no,
            key=budget_key,
            units=1,
            reason=reason,
            reservation_id=reservation_id,
        )

    def _close_failed_attempt(
        self,
        *,
        operation_id: int,
        attempt_id: int,
        attempt_no: int,
        operation_type: OperationType,
        budget_key: str,
        reservation_id: int | None,
        reason: str,
        phase: str,
        exc: Exception,
        landed: LandedArtifact | None,
        landed_source_count: int = 0,
    ) -> None:
        """attempt 异常收口：账跟着"访问有没有发生"走，只有关键证据能把预留退回可用池。

        C-10 的口径 —— 只有 `not_started`（请求还没交出），或 `access` 阶段数据源抛
        `AccessNotStarted`（源自己证明没碰平台）才退款；其余一律照计一笔访问。结果已返回后的
        落盘/入库失败尤其不能退款：`release` 只写 `consumed_at`，退掉的预留对崩溃恢复的补账
        （只看"已发起未记账"）就此隐身，真实成本会被永久抹平。

        结算本身失败（如账实冲突）不能盖掉原始异常：这笔预留保持未消费，交给恢复按既成事实补记。
        """
        refundable = phase == "not_started" or (
            phase == "access" and isinstance(exc, AccessNotStarted)
        )
        charged = not refundable
        billing_error: str | None = None
        if refundable:
            self.ledger.release(reservation_id, operation_id=operation_id, key=budget_key)
        else:
            try:
                self._settle_access(
                    operation_id=operation_id,
                    operation_type=operation_type,
                    attempt_no=attempt_no,
                    budget_key=budget_key,
                    reason=reason,
                    reservation_id=reservation_id,
                )
            except Exception as settle_exc:  # noqa: BLE001 - 记账故障不得掩盖原始异常
                billing_error = f"{type(settle_exc).__name__}: {settle_exc}"
        category = ErrorCategory.usage if refundable else FAILURE_PHASE_CATEGORIES[phase]
        self.repo.finish_attempt(
            attempt_id,
            result_state=ResultState.failed,
            error_code=type(exc).__name__,
            error={
                "message": str(exc),
                "category": category.value,
                "access_phase": phase,
                "access_charged": 1 if charged else 0,
                "billing_error": billing_error,
            },
            raw_artifact_count=landed_source_count
            + (1 if landed is not None and landed.row_inserted else 0),
        )
        self.repo.set_operation_status(operation_id, OperationState.failed)
        self.repo.record_event(
            self.run_id,
            "attempt_charged_after_failure" if charged else "attempt_refunded_not_accessed",
            detail={
                "operation_id": operation_id,
                "attempt_id": attempt_id,
                "attempt_no": attempt_no,
                "budget_key": budget_key,
                "reservation_id": reservation_id,
                "access_phase": phase,
                "access_charged": charged,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "artifact_landed": landed is not None,
                "billing_error": billing_error,
            },
        )

    # ------------------------------------------------------------------ 四类采集

    def _observe_suggestions(
        self, *, keyword_id: int, branch_key: str, revisit_seq: int,
        slot_kind: SlotKind | None, sample_kind: CalibrationSampleKind,
        skip_reason: str | None = None,
    ) -> OperationOutcome:
        def attach(context, result, operation_id: int, attempt_id: int):
            if context is None:
                return self._reload_suggestion(operation_id)
            ingest = ingest_suggestion(
                self.repo,
                self.run_id,
                result,
                normalizer=self.normalizer,
                operation_id=operation_id,
                attempt_id=attempt_id,
                branch_key=branch_key,
            )
            note = (
                f"联想观察 {ingest.state.value}：{len(ingest.children)} 条建议，"
                f"新入库 {len(ingest.new_children)} 个词"
            )
            if ingest.self_loop_ranks:
                note += f"，自环 {len(ingest.self_loop_ranks)} 条（保留观察、不建发现边）"
            return ingest, [note]

        return self._execute(
            operation_type=OperationType.suggest,
            keyword_id=keyword_id,
            branch_key=branch_key,
            revisit_seq=revisit_seq,
            slot_kind=slot_kind,
            sample_kind=sample_kind,
            access=lambda context: self.source.get_suggestions(context),
            attach=attach,
            reason="一次联想观察",
            skip_reason=skip_reason,
        )

    def _reload_suggestion(self, operation_id: int) -> tuple[SuggestionIngest, list[str]]:
        header = self.conn.execute(
            "SELECT id, state FROM suggestion_observations WHERE operation_id = ? ORDER BY id LIMIT 1",
            (operation_id,),
        ).fetchone()
        rows = self.conn.execute(
            "SELECT rank, keyword_id, text_raw, text_normalized FROM suggestion_items"
            " WHERE observation_id = ? ORDER BY rank",
            (int(header["id"]),),
        ).fetchall()
        ingest = SuggestionIngest(
            observation_id=int(header["id"]),
            created=False,
            state=ResultState(str(header["state"])),
            children=tuple(
                IngestedChild(
                    rank=int(row["rank"]),
                    keyword_id=int(row["keyword_id"]),
                    raw=str(row["text_raw"]),
                    normalized=str(row["text_normalized"]),
                    is_new_in_run=False,
                )
                for row in rows
            ),
        )
        return ingest, [f"从库里重建 {len(ingest.children)} 条联想，不重复入库、不重复建边"]

    def _reload_posts(self, operation_id: int) -> tuple[PostIngest, list[str]]:
        header = self.conn.execute(
            "SELECT id, state, observed_at FROM post_observations WHERE operation_id = ?"
            " ORDER BY id LIMIT 1",
            (operation_id,),
        ).fetchone()
        if header is None:
            return PostIngest(None, False, ResultState.skipped, ()), ["恢复路径找不到卡片观察行"]
        # 与重放同一份代码：卡片可见字段（发布时间/点赞原文）必须从库里读回，
        # 恢复出来的观察和当初入库的观察才是同一件事实。
        posts = replayed_posts(self.conn, int(header["id"]), str(header["observed_at"]))
        return PostIngest(
            int(header["id"]), False, ResultState(str(header["state"])), posts
        ), [f"从库里重建 {len(posts)} 篇卡片观察，不重复入库"]

    def _has_content_observation(self, keyword_id: int) -> bool:
        """该词是否已有一次内容采样观察落库（不论有没有采到卡片）。首采保底只补第一次。"""
        return (
            self.repo.conn.execute(
                "SELECT 1 FROM post_observations WHERE run_id = ? AND keyword_id = ? LIMIT 1",
                (self.run_id, keyword_id),
            ).fetchone()
            is not None
        )

    def _collect_content(
        self, *, keyword_id: int, branch_key: str, revisit_seq: int,
        slot_score: float | None, slot_kind: SlotKind | None,
        sample_kind: CalibrationSampleKind,
    ) -> tuple[OperationOutcome, list, list[str]]:
        tier, post_limit = score_tier(slot_score, self.settings)
        notes = [f"内容采样档位 {tier}（配置上限 {post_limit} 篇）"]
        floor = self.settings.sampling.first_sample_posts
        first_sample = floor > post_limit and not self._has_content_observation(keyword_id)
        if first_sample:
            # 档位门用绝对分数，而 E 分量要有实际证据才能把分数抬过那个绝对阈值：
            # 两头互为前提时，一整轮探索会退化成"只联想、不验证"，低分词永远拿不到证据。
            notes.append(
                f"首采保底：该词还没有内容采样记录，本次按 sampling.first_sample_posts={floor} 篇采，"
                f"不等档位 {tier} 的 {post_limit} 篇"
            )
            post_limit = floor
        skip: str | None = None
        allowed = 0
        basis = f"档位 {tier}" + ("+首采保底" if first_sample else "")
        if post_limit > 0:
            remaining = int(self.ledger.view("max_posts_stored").remaining)
            allowed = min(post_limit, remaining)
            if allowed <= 0:
                skip = (
                    f"max_posts_stored 剩余 {remaining}，本轮不采内容"
                    "（预算未达，帖子数不得为凑指标而超上限）"
                )
        else:
            skip = f"档位 {tier} 不采内容：E 分量继续用中性先验而不是实际证据"

        def attach(context, result, operation_id: int, attempt_id: int):
            if context is None:
                return self._reload_posts(operation_id)
            ingest = ingest_search(
                self.repo, self.run_id, result, operation_id=operation_id, attempt_id=attempt_id
            )
            stored = ingest.stored_post_count
            if stored:
                # 帖子已经落库，超支必须记账并显式暴露，不能静默吞掉也不能反悔删数据
                self.ledger.record_quota(
                    "max_posts_stored",
                    stored,
                    reason=f"搜索卡片入库 {stored} 篇（{basis}，本次上限 {allowed}）",
                    operation_id=operation_id,
                )
            return ingest, [
                f"卡片观察 {ingest.state.value}：{len(ingest.posts)} 篇，新入库 {stored} 篇"
            ]

        outcome = self._execute(
            operation_type=OperationType.search,
            keyword_id=keyword_id,
            branch_key=branch_key,
            revisit_seq=revisit_seq,
            slot_kind=slot_kind,
            sample_kind=sample_kind,
            access=lambda context: self.source.search_posts(context, limit=allowed),
            attach=attach,
            reason=f"一次内容采样（{basis}，最多 {allowed} 篇）",
            skip_reason=skip,
        )
        posts = list(outcome.ingest.posts) if isinstance(outcome.ingest, PostIngest) else []
        return outcome, posts, notes

    def _collect_bodies(
        self, *, posts: list, keyword_id: int, branch_key: str,
        slot_kind: SlotKind | None, sample_kind: CalibrationSampleKind,
    ) -> tuple[list[OperationReceipt], list[str]]:
        """正文是可选能力：能力不支持就整批记 unsupported，不伪装成"正文为空"。"""
        notes: list[str] = []
        receipts: list[OperationReceipt] = []
        if not self.source.capabilities().is_supported(Capability.post_body):
            notes.append("数据源不支持正文能力：本轮不做正文验证（unsupported，不是空正文）")
            return receipts, notes
        budget = int(self.ledger.view("body_attempts").remaining)
        quota = self.ledger.view("max_posts_stored")
        if budget <= 0 or quota.remaining <= 0:
            notes.append(
                f"body_attempts 剩余 {budget}、帖子配额剩余 {quota.remaining}：跳过正文验证（预算未达，不是收敛）"
            )
            return receipts, notes
        candidates = posts[:budget]
        notes.append(f"本次最多取 {len(candidates)} 篇正文（受 body_attempts 剩余约束）")
        for post in candidates:
            def attach(context, result, operation_id: int, attempt_id: int, post=post):
                if context is None:
                    return None, [f"{post.note_id}：正文观察已在库里（重放不再入库）"]
                ingest = ingest_body(
                    self.repo,
                    self.run_id,
                    result,
                    note_id=post.note_id,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                )
                tail = "正文确实为空" if ingest.body_is_empty else f"版本 {ingest.content_version_id}"
                return ingest, [f"正文 {ingest.state.value}（post {ingest.post_id}，{tail}）"]

            outcome = self._execute(
                operation_type=OperationType.body,
                keyword_id=keyword_id,
                branch_key=branch_key,
                revisit_seq=self._next_seq_for(keyword_id, OperationType.body),
                slot_kind=slot_kind,
                sample_kind=sample_kind,
                access=lambda context, post=post: self.source.fetch_post_body(
                    context, note_id=post.note_id
                ),
                attach=attach,
                reason=f"一次正文读取（{post.note_id}）",
            )
            receipts.append(outcome.receipt)
            notes.extend(outcome.notes)
        return receipts, notes

    def _collect_author(
        self, *, posts: list, keyword_id: int, branch_key: str, revisit_seq: int,
        slot_kind: SlotKind | None, sample_kind: CalibrationSampleKind,
    ) -> tuple[OperationReceipt | None, list[str]]:
        if not self.source.capabilities().is_supported(Capability.author_posts):
            return None, ["数据源不支持作者公开笔记：跳过（unsupported，不伪造作者广度）"]
        refs = [post.author_public_ref for post in posts if post.author_public_ref]
        if not refs:
            return None, ["卡片未带作者公开引用：不做作者采样（作者引用缺失不能把每篇当不同作者）"]
        if not self.ledger.can_afford("author_attempts"):
            return None, ["author_attempts 预算已耗尽：跳过作者采样"]

        def attach(context, result, operation_id: int, attempt_id: int):
            if context is None:
                return None, ["作者列表观察已在库里（重放不再入库）"]
            ingest = ingest_author_posts(self.repo, self.run_id, result)
            return ingest, [f"作者公开笔记 {ingest.state.value}：{len(ingest.posts)} 篇"]

        outcome = self._execute(
            operation_type=OperationType.author_posts,
            keyword_id=keyword_id,
            branch_key=branch_key,
            revisit_seq=self._next_seq_for(keyword_id, OperationType.author_posts),
            slot_kind=slot_kind,
            sample_kind=sample_kind,
            access=lambda context, ref=refs[0]: self.source.get_author_posts(
                context, author_ref=ref, limit=5
            ),
            attach=attach,
            reason="一次作者公开笔记采样",
        )
        return outcome.receipt, list(outcome.notes)

    # ------------------------------------------------------------------ 归并与奖励

    def _merge_problems(self, outcome: OperationOutcome, posts: list, keyword_id: int) -> tuple:
        """标题 → 证据 → 问题 → 簇：只在本次真的拿到卡片与原文结果时执行。"""
        if not outcome.receipt.succeeded or not posts or outcome.result is None:
            return 0, [], ["无内容证据：本次不给问题簇奖励（E 仍是中性先验）"]
        records = extract_problems(
            self.run_id,
            outcome.result,
            posts,
            keyword_id=keyword_id,
            operation_id=outcome.receipt.operation_id,
            attempt_no=outcome.receipt.attempt_no,
            matcher=self.matcher,
            normalizer=self.normalizer,
            analysis_version=self.merger.analysis_version,
        )
        if not records:
            return 0, [], ["标题里没抽出可定位的问题片段"]
        merge_outcome = self.merger.merge(records, seen_at=utc_now())
        return (
            merge_outcome.new_valid_clusters,
            list(merge_outcome.valid_evidence_ids),
            [
                f"初步归并：证据 {len(merge_outcome.evidence_ids)} 条（复用 {merge_outcome.reused}）、"
                f"问题 {len(merge_outcome.problem_ids)} 条、新增有效簇 {merge_outcome.new_valid_clusters}、"
                f"属性冲突分裂 {len(merge_outcome.conflicts)}"
            ],
        )

    def _reward(
        self,
        outcome: OperationOutcome,
        *,
        new_clusters: int,
        cluster_evidence_ids: list[int],
        new_keywords: int,
        posts_stored: int,
    ) -> tuple[BranchStat, float]:
        """奖励只归本次选中的分支；联想永远拿 0 个问题簇奖励（模型层也会拒绝）。"""
        receipt = outcome.receipt
        is_suggest = receipt.operation_type is OperationType.suggest
        counts = receipt.succeeded
        clusters = new_clusters if (counts and not is_suggest) else 0
        evidence_ids = list(cluster_evidence_ids) if clusters else []
        keywords = new_keywords if counts else 0
        cost = OperationCost(
            operation_type=receipt.operation_type,
            attempts=max(receipt.attempt_no, 1),
            posts_stored=posts_stored if counts else 0,
        )
        update = RewardUpdate(
            run_id=self.run_id,
            keyword_id=receipt.keyword_id,
            operation_type=receipt.operation_type,
            revisit_seq=receipt.revisit_seq,
            attempt_no=max(receipt.attempt_no, 1),
            attributed_branch_id=receipt.branch_key,
            new_valid_problem_clusters=clusters,
            new_related_keywords=keywords,
            normalized_cost=cost.normalized,
            cluster_keyword_weight=self.settings.scheduler.cluster_keyword_weight,
            result_state=receipt.state,
            valid_cluster_evidence_ids=evidence_ids,
            slot_kind=receipt.slot_kind,
            sample_kind=receipt.sample_kind,
        )
        stat = self.repo.apply_reward(update)
        return stat, round(update.reward, 6)

    # ------------------------------------------------------------------ 扩展与入队

    def _explored_normalized(self) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT k.normalized_text FROM run_keywords rk JOIN keywords k ON k.id = rk.keyword_id"
            " WHERE rk.run_id = ? ORDER BY rk.keyword_id",
            (self.run_id,),
        ).fetchall()
        return tuple(str(row["normalized_text"]) for row in rows)

    def _branch_ucb(self) -> dict[str, float]:
        return normalize_ucb(
            ucb_values(
                load_branch_stats(self.repo, self.run_id),
                coefficient=self.settings.scheduler.ucb_exploration_coefficient,
            )
        )

    def _expand_and_enqueue(
        self, *, keyword_id: int, branch_key: str, depth: int,
        ingest: SuggestionIngest | None, posts: list, evidence_ids: list[int],
    ) -> tuple[int, list[str], list[str]]:
        children = ingest.children if ingest is not None else ()
        exhausted = tuple(
            str(row["text_normalized"])
            for row in self.conn.execute(
                "SELECT DISTINCT si.text_normalized FROM suggestion_items si"
                " JOIN suggestion_observations so ON so.id = si.observation_id"
                " JOIN run_keywords rk ON rk.run_id = so.run_id AND rk.keyword_id = si.keyword_id"
                " WHERE so.run_id = ? AND so.keyword_id = ? AND rk.status IN ('explored', 'irrelevant')",
                (self.run_id, keyword_id),
            ).fetchall()
        )
        keyword_text = self._keyword_text(keyword_id)
        # 本次观察到的联想子词是"待入队的新材料"，既不能算进算子的已见集合，也不能算进"已探索"：
        # 入库在前、扩展在后，若不剔除，suggest_vertical 会把自己刚登记的子词全部吞掉，
        # 队列里只剩自造词，真正的平台联想永远排不进探索；N 分量也会拿候选跟自己比。
        fresh_children = {child.normalized for child in children}
        known_texts = tuple(text for text in self._explored_normalized() if text not in fresh_children)
        context = ExpansionInput(
            run_id=self.run_id,
            keyword_id=keyword_id,
            keyword=keyword_text,
            normalized=self.normalizer.normalize(keyword_text.raw),
            branch_key=branch_key,
            depth=depth,
            children=children,
            posts=tuple(post for post in posts if post.title),
            evidence_ids=tuple(evidence_ids),
            exhausted_children=exhausted,
            seen_normalized=frozenset(known_texts),
        )
        rejected: list[str] = []
        candidates = expand(
            context,
            self.operator_settings,
            enabled=tuple(self.settings.scheduler.operators),
            rejected=rejected,
        )
        if not candidates:
            return 0, rejected or ["四类算子本次没有产出新候选"], []
        ucb = self._branch_ucb()
        batch = PriorityBatch(
            anchors=self.anchors,
            explored=known_texts,
            weights=self.settings.scheduler.weights,
            matcher=self.matcher,
            unknown_evidence_prior=self.settings.scheduler.unknown_evidence_prior,
            char_ngram_min=self.settings.nlp.char_ngram_min,
            char_ngram_max=self.settings.nlp.char_ngram_max,
        )
        entries: list[PriorityInput] = []
        for candidate in candidates:
            own_id = self.repo.keyword_id(candidate.keyword.normalized)
            rank = next((child.rank for child in children if child.keyword_id == own_id), None)
            entries.append(
                with_branch_ucb(
                    PriorityInput(
                        text=candidate.keyword.normalized,
                        origin=candidate.origin,
                        branch_key=branch_key,
                        observed_parents=0 if own_id is None else self.graph.independent_parent_count(own_id),
                        best_suggestion_rank=rank,
                        sampled=bool(posts),
                        explicit_evidence_count=(
                            self._evidence_count(candidate.keyword.normalized) if posts else 0
                        ),
                    ),
                    ucb.get(branch_key),
                    note=(
                        None
                        if branch_key in ucb
                        else f"分支 {branch_key} 尚无试验记录，U 分量按缺失处理而不是 0"
                    ),
                )
            )
        scored = evaluate_batch(batch, tuple(entries))
        # 评分结果与候选同序（entries 由 candidates 一一对应构造），登记必须用候选本身：
        # PriorityInput 只有文本，拿它当 KeywordCandidate 会丢掉 origin/operator 与证据引用。
        registered = register_candidates(
            self.repo,
            self.run_id,
            candidates,
            parent_keyword_id=keyword_id,
            branch_key=branch_key,
            seen_at=utc_now(),
        )
        dropped: list[str] = []
        queued = 0
        for (registered_id, candidate), (_entry, score) in zip(registered, scored, strict=True):
            judgment = judge_relevance(
                candidate.keyword.normalized,
                anchors=self.anchors,
                domain_terms=self.domain_terms,
                normalizer=self.normalizer,
            )
            if not judgment.related:
                self.repo.upsert_run_keyword(
                    self.run_id,
                    registered_id,
                    status="irrelevant",
                    irrelevant_reason=judgment.reason,
                    seen_at=utc_now(),
                )
                dropped.append(f"{candidate.keyword.normalized}：{judgment.reason}")
                continue
            breakdown = score.model_dump(mode="json")
            breakdown["operator"] = candidate.operator
            breakdown["origin"] = candidate.origin.value
            self.repo.enqueue(
                self.run_id,
                registered_id,
                branch_key,
                score=score.score,
                score_breakdown=breakdown,
                # 不继承"发现它的那个槽"的类型：候选一律留空表示三个桶都能认领，
                # 否则一次分支探索槽产出的词会全被打成 branch_exploration，
                # 高分槽与新颖槽永远挑不到东西，60/25/15 的分配名存实亡。
                slot_kind=None,
                selection_reason=(
                    f"{candidate.selection_reason}；优先级分解见 score_breakdown_json"
                    f"（有效权重 {score.effective_weight_total:.3f}）"
                ),
                queued_at=utc_now(),
            )
            queued += 1
        return (
            queued,
            [f"算子扩展登记候选 {len(candidates)} 个，入队 {queued} 个", *rejected],
            dropped,
        )

    def _evidence_count(self, normalized: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT e.id) AS n FROM evidence e"
            " JOIN keywords k ON k.id = e.keyword_id WHERE e.run_id = ? AND k.normalized_text = ?",
            (self.run_id, normalized),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ 校准

    def _run_calibration_sample(self) -> int:
        """校准：独立预算键、不进收敛窗口、不给主动探索记功、不做算子扩展。"""
        if self.pool is None:
            return 0
        if not self.ledger.can_afford(calib.CALIBRATION_BUDGET_KEY):
            return 0
        member = calib.plan_next(self.repo, self.run_id, self.pool, self.ledger)
        if member is None:
            return 0
        # 校准池成员的 keyword_id 一定在 run_keywords 里（池就是从候选冻结的），可直接开操作
        outcome = self._observe_suggestions(
            keyword_id=member.keyword_id,
            branch_key=member.branch_key or "b-root",
            revisit_seq=CALIBRATION_SEQ_BASE
            + [item.keyword_id for item in self.pool.members].index(member.keyword_id),
            slot_kind=None,
            sample_kind=CalibrationSampleKind.coverage_calibration,
        )
        self.repo.record_event(
            self.run_id,
            "calibration_sampled",
            detail={
                "pool_id": self.pool.pool_id,
                "keyword_id": member.keyword_id,
                "normalized": member.normalized,
                "operation_id": outcome.receipt.operation_id,
                "state": outcome.receipt.state.value,
                "budget_key": calib.CALIBRATION_BUDGET_KEY,
                "note": "校准观察不给主动探索记功，也不进收敛窗口",
            },
        )
        return 1

    # ------------------------------------------------------------------ 主循环

    def run(self, *, max_slots: int | None = None, calibration: bool = True) -> EngineReport:
        """跑主循环直到停止策略判定结束，或达到本次调用的槽位上限/被暂停。"""
        report = EngineReport(run_id=self.run_id)
        recovery = recover_run(self.conn, self.paths, self.run_id, ledger=self.ledger)
        report.recovered = {
            "imported_artifacts": recovery.imports.imported_count,
            "orphaned": list(recovery.imports.orphaned),
            "reconciled": list(recovery.reconciled),
            "uncertain_operations": list(recovery.uncertain),
            "released_reservations": list(recovery.released_reservations),
        }
        if recovery.released_reservations:
            report.events.append(
                f"恢复时释放幽灵预留 {len(recovery.released_reservations)} 笔：{recovery.released_reservations}"
            )
        if calibration and self.settings.budget.calibration_suggest_attempts:
            self.pool = calib.freeze_pool(self.repo, self.run_id, self.settings)
            report.events.append(
                f"校准池冻结：{self.pool.pool_id}（{self.pool.size} 个成员，种子 {self.pool.seed}）"
            )
        while True:
            for item in consume_all_controls(self.conn, self.run_id, ledger=self.ledger):
                report.events.append(
                    f"消费控制请求 {item['request_id']}：{item['action']} → {item['status_after']}"
                )
            status = str(self.repo.require_run(self.run_id)["status"])
            if status != RunStatus.running.value:
                report.events.append(
                    f"run 状态为 {status}，worker 在安全点退出（暂停/取消都可恢复，已扣预算不回滚）"
                )
                report.termination = self._decision_from_run_row()
                break
            if max_slots is not None and self.slot_index >= max_slots:
                report.events.append(f"达到本次调用的槽位上限 {max_slots}（预算与收敛判定不变）")
                break
            if self._reclaim_unfinished(report):
                # 本轮先消化一个接管来的未完成槽位；处理完重新评估（可能已完成或已停在阻断）
                continue
            decision = stopping.evaluate(
                self.repo, self.run_id, self.settings, self.ledger, error=self.halt_error
            )
            if decision.status is not RunStatus.running:
                self._finalize(decision)
                report.termination = decision
                report.events.append(f"停止策略：{decision.status.value} —— {decision.note}")
                break
            stopping.apply_branch_stalls(self.repo, self.run_id, self.settings, decision.signals)
            slot = self.scheduler.next_slot(
                index=self.slot_index, pending=self._pending_grouped(skip=set(decision.signals.stalled))
            )
            if slot is None:
                decision = self._completion_without_candidates(decision)
                self._finalize(decision)
                report.termination = decision
                report.events.append(f"无候选退出：{decision.note}")
                break
            try:
                self._execute_slot(slot, report)
            except WorkerAlreadyRunning as exc:
                report.events.append(
                    f"租约所有权已丢失（{exc}），worker 立即停止外部访问；"
                    "在飞操作留给下一个持有者按事实回填"
                )
                break
            if calibration and self.pool is not None:
                report.calibration_samples += self._run_calibration_sample()
        if report.termination is None:
            report.termination = stopping.evaluate(
                self.repo, self.run_id, self.settings, self.ledger, error=self.halt_error
            )
            # 达到本次槽位上限退出时，若本轮有阻断（halt_error）使停止策略判为非 running，必须落库：
            # 库里留 running 等于向下一个 worker 谎报"这 run 还活着"，僵尸接管会再扣预算。
            # 仅在有阻断时补这一笔——租约丢失（WorkerAlreadyRunning）不设 halt_error，僵尸 worker
            # 无权改写它已不再拥有的 run 状态。
            if self.halt_error is not None and report.termination.status is not RunStatus.running:
                self._finalize(report.termination)
        return report

    def run_guarded(self, *, max_slots: int | None = None, calibration: bool = True) -> EngineReport:
        """在 worker 锁的临界区内跑主循环：无论正常结束还是抛异常，`finally` 一定放锁。"""
        self.control.acquire()
        try:
            return self.run(max_slots=max_slots, calibration=calibration)
        finally:
            self.control.release()

    def _decision_from_run_row(self) -> stopping.StopDecision:
        """库里的状态才是事实：控制请求（暂停/取消）在安全点生效后据实回报，不另判一套。"""
        run = self.repo.require_run(self.run_id)
        signals = stopping.collect_signals(
            self.repo, self.run_id, self.settings, self.ledger
        )
        outcome = run["outcome"]
        pause_reason = run["pause_reason"]
        return stopping.StopDecision(
            status=RunStatus(str(run["status"])),
            outcome=None if outcome is None else TerminalOutcome(str(outcome)),
            pause_reason=None if pause_reason is None else PauseReason(str(pause_reason)),
            note=f"库里登记的终态/暂停：{run['status']}",
            signals=signals,
        )

    def _completion_without_candidates(
        self, decision: stopping.StopDecision
    ) -> stopping.StopDecision:
        """调度器挑不出候选：区分"队列真空"与"全部动作不可执行"，两种都不是收敛。"""
        signals = decision.signals
        missing = stopping.convergence_failures(signals, self.settings)
        if signals.pending_items == 0:
            all_blocked = bool(signals.budget.blocked_actions) and not signals.budget.runnable_actions
            return stopping.StopDecision(
                status=RunStatus.completed,
                outcome=(
                    TerminalOutcome.budget_exhausted
                    if all_blocked or signals.budget.runtime_limit_hit
                    else TerminalOutcome.queue_exhausted
                ),
                pause_reason=None,
                note=(
                    f"调度器无候选：待办 {signals.pending_items} 项"
                    f"{'，且全部动作预算耗尽' if all_blocked else ''}；"
                    f"未满足的收敛条件：{'；'.join(missing)}"
                ),
                signals=signals,
            )
        return stopping.StopDecision(
            status=RunStatus.paused,
            outcome=None,
            pause_reason=PauseReason.fault,
            note=(
                f"队列尚有 {signals.pending_items} 项，但停滞分支被暂缓后没有可调度候选；"
                f"这是暂停不是收敛（未满足：{'；'.join(missing)}）"
            ),
            signals=signals,
        )

    def _finalize(self, decision: stopping.StopDecision) -> None:
        self.repo.set_run_status(self.run_id, decision.as_termination())
        self.repo.record_event(
            self.run_id,
            f"stop_{decision.status.value}",
            detail={"note": decision.note, "signals": decision.signals.as_dict()},
        )

    def _pending_grouped(self, *, skip: set[str]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for row in self.repo.frontier_rows(self.run_id, status="pending"):
            key = str(row["branch_key"])
            if key in skip:
                continue
            grouped.setdefault(key, []).append(row)
        return grouped

    def _slot_from_frontier_row(self, row: dict) -> ScheduledSlot:
        """从 in_flight frontier 行重建可执行槽位（接管上一任留下的未完成槽位）。"""
        slot_kind_value = row["slot_kind"]
        return ScheduledSlot(
            frontier_id=int(row["id"]),
            keyword_id=int(row["keyword_id"]),
            normalized=str(row["normalized_text"]),
            branch_key=str(row["branch_key"]),
            slot_kind=SlotKind(str(slot_kind_value)) if slot_kind_value else SlotKind.high_score,
            sample_kind=CalibrationSampleKind(str(row["sample_kind"])),
            score=None if row["score"] is None else float(row["score"]),
            selection_reason=f"接管未完成槽位：{row['selection_reason']}",
            selection_probability=(
                None if row["selection_probability"] is None else float(row["selection_probability"])
            ),
        )

    def _reclaim_unfinished(self, report: EngineReport) -> bool:
        """接管上一任 worker 留下的未完成槽位（C22 交付 1/3）。

        判据来自 `frontier.active_revisit_seq`（认领时与状态、revisit 递增同事务落定）：
        - 有序号：沿用原重探序号执行 `_execute_slot`（不重复认领、不重复递增），落盘结果在
          `_execute` 里离线补入库，缺的后续动作才真的访问；执行过就返回 True 让主循环重评估。
        - 无序号（v7 之前的历史 in_flight 行）：保守处理——不猜序号、不重发，保留 in_flight
          并显式记 uncertain，交人工处置；绝不因判空而误当队列已排空/收敛。
        每轮至多接管一个非历史槽位；处理完返回 True，由主循环下一轮重新评估停止策略。
        """
        rows = self.repo.frontier_rows(self.run_id, status="in_flight")
        for row in rows:
            seq = row.get("active_revisit_seq")
            if seq is None:
                self.repo.record_event(
                    self.run_id, "frontier_legacy_uncertain",
                    detail={"frontier_id": int(row["id"]), "keyword_id": int(row["keyword_id"]),
                            "reason": "in_flight 槽位没有 active_revisit_seq：无法确定其占用的重探序号，"
                                      "保守保留在飞状态并标待核查，不猜序号也不重发"},
                    at=utc_now(),
                )
                report.recovered.setdefault("uncertain_frontier", []).append(int(row["id"]))
                continue
            slot = self._slot_from_frontier_row(row)
            self.repo.record_event(
                self.run_id, "frontier_reclaimed",
                detail={"frontier_id": slot.frontier_id, "keyword_id": slot.keyword_id,
                        "active_revisit_seq": int(seq),
                        "note": "接管未完成槽位，沿用原重探序号，不重复递增、不重发已落盘结果"},
                at=utc_now(),
            )
            report.events.append(
                f"接管未完成槽位 {slot.frontier_id}（关键词 {slot.keyword_id}，重探序号 {int(seq)}）"
            )
            try:
                self._execute_slot(slot, report, reclaim_seq=int(seq))
            except WorkerAlreadyRunning as exc:
                report.events.append(
                    f"接管槽位时租约所有权已丢失（{exc}）：立即停止外部访问，未完成槽位留给下一个持有者"
                )
            return True
        return False

    def _execute_slot(self, slot: ScheduledSlot, report: EngineReport,
                      *, reclaim_seq: int | None = None) -> None:
        keyword_id = slot.keyword_id
        node = self.graph.node(keyword_id)
        if node is None:
            raise ValueError(f"调度返回了不在发现图里的关键词 {keyword_id}")
        branch_key = slot.branch_key
        if reclaim_seq is None:
            # 认领：状态、active_revisit_seq 与 revisit 递增在同一事务里落定；
            # 非 pending（并发已被认领或已在飞）直接跳过，绝不重复递增成下一轮重探。
            claimed = self.repo.claim_frontier_slot(
                slot.frontier_id, self.run_id, keyword_id, at=utc_now()
            )
            if claimed is None:
                return
            revisit_seq = claimed
        else:
            # 接管上一任留下的未完成槽位：沿用原重探序号，不再递增、不再认领。
            revisit_seq = reclaim_seq
        self.slot_index += 1
        index = self.slot_index - 1
        notes = [f"槽位 {index}：{slot.selection_reason}"]

        suggest = self._observe_suggestions(
            keyword_id=keyword_id,
            branch_key=branch_key,
            revisit_seq=revisit_seq,
            slot_kind=slot.slot_kind,
            sample_kind=slot.sample_kind,
        )
        notes.extend(suggest.notes)
        ingest = suggest.ingest if isinstance(suggest.ingest, SuggestionIngest) else None

        # C22/复核补充：一旦某一步出现阻断信号（认证/验证/占用/进程不确定/坏证据），
        # 同一槽位绝不继续下一步的外部访问——不能只在外层下一轮才看 halt_error。
        search_outcome = None
        posts: list = []
        if self.halt_error is None:
            search_outcome, posts, content_notes = self._collect_content(
                keyword_id=keyword_id,
                branch_key=branch_key,
                revisit_seq=revisit_seq,
                slot_score=slot.score,
                slot_kind=slot.slot_kind,
                sample_kind=slot.sample_kind,
            )
            notes.extend(content_notes)
            if not suggest.receipt.succeeded:
                notes.append(
                    f"联想结果 {suggest.receipt.state.value}：仍尝试已有卡片，但不给联想关键词奖励"
                )
        bodies: list = []
        if self.halt_error is None:
            bodies, body_notes = self._collect_bodies(
                posts=posts,
                keyword_id=keyword_id,
                branch_key=branch_key,
                slot_kind=slot.slot_kind,
                sample_kind=slot.sample_kind,
            )
            notes.extend(body_notes)
        author = None
        if self.halt_error is None:
            author, author_notes = self._collect_author(
                posts=posts,
                keyword_id=keyword_id,
                branch_key=branch_key,
                revisit_seq=revisit_seq,
                slot_kind=slot.slot_kind,
                sample_kind=slot.sample_kind,
            )
            notes.extend(author_notes)

        new_clusters, cluster_evidence = 0, []
        if search_outcome is not None and self.halt_error is None:
            new_clusters, cluster_evidence, merge_notes = self._merge_problems(
                search_outcome, posts, keyword_id
            )
            notes.extend(merge_notes)
        elif search_outcome is None:
            notes.append("上游阻断，本槽未发起内容访问：不合并问题簇，未完成槽位保留待处置")

        _suggest_stat, suggest_reward = self._reward(
            suggest,
            new_clusters=0,
            cluster_evidence_ids=[],
            new_keywords=0 if ingest is None else len(ingest.new_children),
            posts_stored=0,
        )
        content_reward = 0.0
        if search_outcome is not None and search_outcome.receipt.succeeded:
            _content_stat, content_reward = self._reward(
                search_outcome,
                new_clusters=new_clusters,
                cluster_evidence_ids=cluster_evidence,
                new_keywords=0,
                posts_stored=len(posts),
            )
        elif search_outcome is not None:
            notes.append(
                f"内容操作结果 {search_outcome.receipt.state.value}：不给问题簇奖励，"
                "已发生的成本仍按结算记录"
            )

        registered, expansion_notes, dropped = self._expand_and_enqueue(
            keyword_id=keyword_id,
            branch_key=branch_key,
            depth=node.depth,
            ingest=ingest,
            posts=posts,
            evidence_ids=cluster_evidence,
        )
        notes.extend(expansion_notes)
        if dropped:
            self.repo.record_event(
                self.run_id, "candidates_dropped", detail={"items": dropped}, at=utc_now()
            )

        if self.halt_error is None:
            self.repo.set_frontier_status(slot.frontier_id, "done")
            # run_keywords 的状态词表与 frontier 不同：探索完成是 explored，done 会被 CHECK 拒绝
            self.repo.upsert_run_keyword(
                self.run_id, keyword_id, status="explored", seen_at=utc_now()
            )
        elif self.halt_error.code == LANDED_INTEGRITY_CODE:
            # 坏证据：留在 in_flight 交人工核查，既不重发也不清序号，避免把篡改当"没人占着"
            self.repo.record_event(
                self.run_id, "frontier_halted_integrity",
                detail={"frontier_id": slot.frontier_id, "keyword_id": keyword_id,
                        "active_revisit_seq": revisit_seq, "code": self.halt_error.code},
                at=utc_now(),
            )
        else:
            # 阻断错误：保留未完成槽位、退回 pending 清序号，人工处置后按新一轮重探再采；
            # 历史 attempt/预算/观察不动，恢复用同一套账本，不重复扣。
            self.repo.reset_frontier_for_retry(slot.frontier_id)
            self.repo.record_event(
                self.run_id, "frontier_halted_retry_pending",
                detail={"frontier_id": slot.frontier_id, "keyword_id": keyword_id,
                        "halted_after_revisit_seq": revisit_seq, "code": self.halt_error.code,
                        "note": "阻断信号停在 paused，未完成槽位保留待人工处置"},
                at=utc_now(),
            )
        report.slots.append(
            SlotOutcome(
                index=index,
                frontier_id=slot.frontier_id,
                keyword_id=keyword_id,
                normalized=slot.normalized,
                branch_key=branch_key,
                slot_kind=slot.slot_kind,
                suggest=suggest.receipt,
                search=None if search_outcome is None else search_outcome.receipt,
                bodies=bodies,
                author=author,
                suggest_reward=suggest_reward,
                content_reward=content_reward,
                new_valid_clusters=new_clusters,
                new_related_keywords=0 if ingest is None else len(ingest.new_children),
                registered=registered,
                dropped_irrelevant=dropped,
                notes=notes,
            )
        )
        if self.halt_error is None:
            report.events.append(
                f"槽位 {index} 完成：{slot.normalized}"
                f"（联想 {suggest.receipt.state.value}/{suggest.receipt.attempt_no} 次尝试，"
                f"内容 {search_outcome.receipt.state.value}，"
                f"奖励 {suggest_reward + content_reward:.4f}）"
            )
        else:
            report.events.append(
                f"槽位 {index} 停在阻断：{slot.normalized}"
                f"（联想 {suggest.receipt.state.value}，阻断码 {self.halt_error.code}；"
                "同一槽未继续后续访问）"
            )


class EngineSetup:
    """按 run 配置重建 Repository/Ledger/WorkerControl：重启路径与首启动路径共用同一份构造。"""

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        settings: Settings,
        run_id: str,
        source: SearchSource,
        domain_terms: tuple[str, ...] = (),
        ambiguous_seeds: tuple[str, ...] = (),
        locations: tuple[str, ...] = (),
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.run_id = run_id
        self.source = source
        self.domain_terms = domain_terms
        self.ambiguous_seeds = ambiguous_seeds
        self.locations = locations

    def build(self, repo: Repository) -> DiscoveryEngine:
        ledger = Ledger.from_settings(repo.conn, self.run_id, self.settings)
        control = WorkerControl(repo.conn, self.paths, self.run_id)
        return DiscoveryEngine(
            paths=self.paths,
            settings=self.settings,
            run_id=self.run_id,
            source=self.source,
            repo=repo,
            ledger=ledger,
            control=control,
            domain_terms=self.domain_terms,
            ambiguous_seeds=self.ambiguous_seeds,
            locations=self.locations,
        )


def prepare_run(
    repo: Repository,
    settings: Settings,
    *,
    run_id: str,
    created_at: datetime,
    branch_key: str = "b-root",
) -> int:
    """建 run、登记种子词、开首分支并入队：歧义种子只标记不改写用户输入。"""
    if repo.get_run(run_id) is not None:
        raise ValueError(f"run {run_id} 已存在：P0-C 不允许用同一个 id 覆盖历史轨迹")
    normalizer = KeywordNormalizer()
    seed = normalizer.normalize(settings.run.seed_raw)
    keyword_id = repo.ensure_keyword(
        seed.normalized, seed.raw, seen_at=created_at, is_ambiguous_seed=seed.is_ambiguous_seed
    )
    identity = RunIdentity(
        run_id=run_id,
        seed=seed.as_keyword_text(),
        mode=settings.run.mode,
        config_hash=settings.config_hash(),
        random_seed=settings.run.random_seed,
        created_at=created_at,
        synthetic_fixture=settings.run.mode == "fixture",
    )
    repo.create_run(identity, config_json=settings.canonical_json())
    repo.ensure_branch(
        run_id, branch_key, min_validation_share_left=settings.sampling.branch_min_validation_share
    )
    repo.upsert_run_keyword(
        run_id,
        keyword_id,
        depth=0,
        branch_id=repo.branch_id(run_id, branch_key),
        origin="seed",
        status="queued",
        seen_at=created_at,
    )
    repo.enqueue(
        run_id,
        keyword_id,
        branch_key,
        # 未评分的种子不预占高分槽：槽位类型留空表示任何桶都能认领，
        # 由调度器按冷启动（新分支零试验优先覆盖）选它，而不是让 0 号槽永远挑不出候选。
        slot_kind=None,
        selection_reason="种子词首探：来自用户输入直接入队，未评分（分数缺失按冷启动处理）",
        queued_at=created_at,
    )
    repo.set_run_status(run_id, RunTermination(status=RunStatus.running), at=created_at)
    return keyword_id


__all__ = [
    "CALIBRATION_SEQ_BASE",
    "EngineReport",
    "EngineSetup",
    "OperationOutcome",
    "OperationReceipt",
    "DiscoveryEngine",
    "SlotOutcome",
    "prepare_run",
]
