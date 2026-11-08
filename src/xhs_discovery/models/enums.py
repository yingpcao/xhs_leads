"""状态与分类枚举：结果状态必须能区分"确实没有"与"没采到"。"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - 便于日志与导出稳定序列化
        return self.value


class ResultState(StrEnum):
    """数据源一次操作的终态。"""

    completed = "completed"
    partial = "partial"
    failed = "failed"
    skipped = "skipped"
    unsupported = "unsupported"


class Capability(StrEnum):
    suggestions = "suggestions"
    search_cards = "search_cards"
    post_body = "post_body"
    author_posts = "author_posts"


class CapabilityState(StrEnum):
    supported = "supported"
    unsupported = "unsupported"
    blocked = "blocked"
    unknown = "unknown"


class OperationType(StrEnum):
    suggest = "suggest"
    search = "search"
    body = "body"
    author_posts = "author_posts"


class OperationState(StrEnum):
    """operations 表状态。uncertain 专用于进程中断且无可靠结果。"""

    pending = "pending"
    in_flight = "in_flight"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    skipped = "skipped"
    unsupported = "unsupported"
    uncertain = "uncertain"


class CandidateOrigin(StrEnum):
    """generated 候选绝不伪造 SUGGESTS 边；只有平台返回才记 observed。"""

    observed = "observed"
    generated = "generated"


class SourceField(StrEnum):
    suggestion = "suggestion"
    title = "title"
    body = "body"


class PostSourceType(StrEnum):
    user_help = "user_help"
    experience_share = "experience_share"
    institution = "institution"
    marketing = "marketing"
    unknown = "unknown"


class StatementKind(StrEnum):
    """显性问题 / 推断需求 / 方案构想 / 搜索意图候选严格分离。"""

    explicit_problem = "explicit_problem"
    inferred_need = "inferred_need"
    solution_idea = "solution_idea"
    search_intent_candidate = "search_intent_candidate"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"


class TerminalOutcome(StrEnum):
    """run 到达终态的原因；与 status 分开，避免把失败当收敛。"""

    budget_exhausted = "budget_exhausted"
    converged = "converged"
    queue_exhausted = "queue_exhausted"
    no_actions_available = "no_actions_available"
    cancelled = "cancelled"
    fault = "fault"


class ControlActionType(StrEnum):
    pause = "pause"
    resume = "resume"
    extend = "extend"
    cancel = "cancel"


class PauseReason(StrEnum):
    user = "user"
    auth_required = "auth_required"
    action_required = "action_required"
    profile_busy = "profile_busy"
    fault = "fault"
    selector_mismatch = "selector_mismatch"
    network = "network"


class ErrorCategory(StrEnum):
    auth = "auth"
    action_required = "action_required"
    profile_busy = "profile_busy"
    network = "network"
    page = "page"
    output = "output"
    system = "system"
    usage = "usage"
    cancelled = "cancelled"
    budget = "budget"


class SlotKind(StrEnum):
    """调度槽类型；随机槽只支持探索，不自动视为无偏校准样本。"""

    high_score = "high_score"
    branch_exploration = "branch_exploration"
    random_novelty = "random_novelty"


class CalibrationSampleKind(StrEnum):
    active_exploration = "active_exploration"
    coverage_calibration = "coverage_calibration"


class IntentCode(StrEnum):
    """八类意图（PROJECT_SPEC.md 6.2）。"""

    information_query = "information_query"
    method_guidance = "method_guidance"
    comparison_decision = "comparison_decision"
    risk_avoidance = "risk_avoidance"
    resource_acquisition = "resource_acquisition"
    cost_constraint = "cost_constraint"
    effect_improvement = "effect_improvement"
    experience_validation = "experience_validation"


EXPANSION_OPERATORS = (
    "suggest_vertical",
    "phrase_fallback",
    "content_lateral",
    "problem_probe",
)


__all__ = [
    "StrEnum",
    "ResultState",
    "Capability",
    "CapabilityState",
    "OperationType",
    "OperationState",
    "CandidateOrigin",
    "SourceField",
    "PostSourceType",
    "StatementKind",
    "RunStatus",
    "TerminalOutcome",
    "ControlActionType",
    "PauseReason",
    "ErrorCategory",
    "SlotKind",
    "CalibrationSampleKind",
    "IntentCode",
    "EXPANSION_OPERATORS",
]
