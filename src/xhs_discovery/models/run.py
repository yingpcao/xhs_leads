"""运行、预算与分支统计契约（P0-B/P0-C 的实现输入）。

预算与奖励的记账规则：
- 一次实际发起的外部尝试（含失败重试）占对应预算；缓存读取不占外部访问预算但单独统计；
- 单次操作奖励只归实际选中的分支，跨分支重复词保留发现边但不重复记功；
- 控制请求由界面写入、worker 消费，避免 UI 直接改运行状态。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from xhs_discovery.models.common import StrictModel, utc_now
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    ControlActionType,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    SlotKind,
    TerminalOutcome,
)
from xhs_discovery.models.source import KeywordText

# 预算键分两类：外部访问按"一次尝试"计量，配额类只计量事实不发生外部访问。
ACCESS_BUDGET_KEYS = (
    "suggest_attempts",
    "search_attempts",
    "body_attempts",
    "author_attempts",
    "calibration_suggest_attempts",
)
QUOTA_BUDGET_KEYS = (
    "max_posts_stored",
    "max_runtime_minutes",
    "model_calls",
    "model_tokens",
    "model_cost_usd",
)
BUDGET_KEYS = ACCESS_BUDGET_KEYS + QUOTA_BUDGET_KEYS


class RunIdentity(StrictModel):
    run_id: str = Field(min_length=1)
    seed: KeywordText
    mode: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    random_seed: int
    created_at: datetime
    synthetic_fixture: bool

    @model_validator(mode="after")
    def _check(self) -> "RunIdentity":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at 必须带时区，运行时间戳不接受 naive 值")
        if self.mode not in ("fixture", "live"):
            raise ValueError("mode 只能是 fixture 或 live")
        if self.mode == "fixture" and not self.synthetic_fixture:
            raise ValueError("fixture 运行必须标记 synthetic，防止模拟数据伪装真实来源")
        return self


class BudgetLedgerEntry(StrictModel):
    run_id: str = Field(min_length=1)
    budget_key: str = Field(min_length=1)
    operation_type: OperationType | None = None
    attempt_no: int | None = Field(default=None, ge=1)
    reservation_id: int | None = Field(default=None, ge=1)
    consumed_units: float = Field(ge=0.0)
    external_access: bool
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "BudgetLedgerEntry":
        if self.external_access:
            if self.operation_type is None or self.attempt_no is None:
                raise ValueError("外部访问预算事件必须带 operation_type 与 attempt_no（失败重试同样占预算）")
            if self.consumed_units < 1:
                raise ValueError("外部访问事件至少消耗 1 个单位")
            if self.budget_key not in ACCESS_BUDGET_KEYS:
                raise ValueError(f"{self.budget_key} 不是外部访问预算键")
        elif self.consumed_units != 0:
            if self.budget_key not in QUOTA_BUDGET_KEYS:
                raise ValueError(
                    "缓存读取不占外部访问预算，只做单独统计；只有非访问配额键可以记消耗"
                )
        return self


class BranchStat(StrictModel):
    """分支早期使用稳定 ID，不随主题展示标签变化。"""

    run_id: str = Field(min_length=1)
    branch_id: str = Field(min_length=1)
    display_label: str | None = None
    trials: int = Field(ge=0)
    total_reward: float = Field(ge=0.0)
    stale_streak: int = Field(ge=0)
    min_validation_share_left: int = Field(default=0, ge=0)

    @property
    def mean_reward(self) -> float:
        return self.total_reward / self.trials if self.trials else 0.0

    @property
    def needs_first_coverage(self) -> bool:
        return self.trials == 0

    @model_validator(mode="after")
    def _check(self) -> "BranchStat":
        if self.trials == 0 and self.total_reward != 0.0:
            raise ValueError("未试验分支的累计奖励必须为 0")
        return self


class RewardUpdate(StrictModel):
    """单次奖励 =（新增有效问题簇 + 0.2×新增相关关键词）/ 标准化操作成本。"""

    run_id: str = Field(min_length=1)
    keyword_id: int = Field(ge=1)
    operation_type: OperationType
    revisit_seq: int = Field(ge=0)
    attempt_no: int = Field(ge=1)
    attributed_branch_id: str = Field(min_length=1)
    new_valid_problem_clusters: int = Field(ge=0)
    new_related_keywords: int = Field(ge=0)
    normalized_cost: float = Field(gt=0.0)
    cluster_keyword_weight: float = Field(gt=0.0, le=1.0)
    result_state: ResultState
    valid_cluster_evidence_ids: list[int] = Field(default_factory=list)
    slot_kind: SlotKind | None = None
    sample_kind: CalibrationSampleKind | None = None

    @property
    def reward(self) -> float:
        return (
            self.new_valid_problem_clusters
            + self.cluster_keyword_weight * self.new_related_keywords
        ) / self.normalized_cost

    @model_validator(mode="after")
    def _check(self) -> "RewardUpdate":
        if self.new_valid_problem_clusters and not self.valid_cluster_evidence_ids:
            raise ValueError("有效问题簇必须至少有一条可定位的显性问题证据引用，否则不计奖励")
        if self.result_state is ResultState.failed:
            if self.new_valid_problem_clusters or self.new_related_keywords:
                raise ValueError("失败操作不得记新增奖励，失败样本不是需求缺失的证据")
        if self.operation_type is OperationType.suggest and self.new_valid_problem_clusters:
            raise ValueError("只做联想的操作只获得关键词奖励，内容簇需来自内容采样")
        if self.reward < 0:
            raise ValueError("奖励不得为负")
        return self


class ControlRequest(StrictModel):
    run_id: str = Field(min_length=1)
    action: ControlActionType
    requested_at: datetime = Field(default_factory=utc_now)
    applied_at: datetime | None = None
    suggest_budget_delta: int | None = Field(default=None, ge=1)
    search_budget_delta: int | None = Field(default=None, ge=1)
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ControlRequest":
        if self.action is ControlActionType.extend:
            if not (self.suggest_budget_delta or self.search_budget_delta):
                raise ValueError("extend 表示追加额度，必须给出正数增量而不是重置总量")
        elif self.suggest_budget_delta or self.search_budget_delta:
            raise ValueError("只有 extend 可以携带预算增量")
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at 必须带时区")
        return self


class RunTermination(StrictModel):
    status: RunStatus
    outcome: TerminalOutcome | None = None
    pause_reason: PauseReason | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "RunTermination":
        if self.status is RunStatus.paused and self.pause_reason is None:
            raise ValueError("paused 必须记录原因（用户、认证、验证、忙碌、故障）")
        if self.status is RunStatus.completed and self.outcome is None:
            raise ValueError("completed 必须区分 budget_exhausted / converged / queue_exhausted")
        if self.status in (RunStatus.queued, RunStatus.running) and (
            self.outcome or self.pause_reason
        ):
            raise ValueError("进行中状态不得携带终态原因")
        if self.outcome is TerminalOutcome.converged and not self.note:
            raise ValueError("converged 必须写明触发的收敛条件，失败与验证码不得伪装收敛")
        return self


__all__ = [
    "ACCESS_BUDGET_KEYS",
    "BUDGET_KEYS",
    "QUOTA_BUDGET_KEYS",
    "RunIdentity",
    "BudgetLedgerEntry",
    "BranchStat",
    "RewardUpdate",
    "ControlRequest",
    "RunTermination",
]
