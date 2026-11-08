"""错误分类：把"没采到"表达成结构化事实，而不是空结果。

分类表来自 `小红书搜索关键词反查工具_USAGE.md` 与 DEVELOPMENT_PLAN.md 第 6 节 P1 错误策略。
未登记的退出码/逐词状态一律归入 unknown 并要求人工介入，禁止静默当作"无结果"。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from xhs_discovery.models.enums import ErrorCategory


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    category: ErrorCategory
    retryable: bool
    human_action_required: bool
    exit_code: int | None = None
    detail: dict[str, str] = Field(default_factory=dict)


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: ErrorCategory
    retryable: bool
    human_action_required: bool


CLI_EXIT_TABLE: dict[int, _Rule] = {
    0: _Rule(category=ErrorCategory.system, retryable=False, human_action_required=False),
    1: _Rule(category=ErrorCategory.system, retryable=False, human_action_required=True),
    2: _Rule(category=ErrorCategory.usage, retryable=False, human_action_required=True),
    3: _Rule(category=ErrorCategory.auth, retryable=False, human_action_required=True),
    4: _Rule(category=ErrorCategory.action_required, retryable=False, human_action_required=True),
    # 5：浏览器/网络/页面/选择器错误的复合退出码，具体处置必须结合逐词 error.code
    5: _Rule(category=ErrorCategory.page, retryable=False, human_action_required=True),
    6: _Rule(category=ErrorCategory.profile_busy, retryable=True, human_action_required=False),
    7: _Rule(category=ErrorCategory.system, retryable=False, human_action_required=False),
    130: _Rule(category=ErrorCategory.cancelled, retryable=False, human_action_required=False),
}

WORD_STATUS_TABLE: dict[str, _Rule] = {
    "OK": _Rule(category=ErrorCategory.system, retryable=False, human_action_required=False),
    "EMPTY": _Rule(category=ErrorCategory.system, retryable=False, human_action_required=False),
    "AUTH_REQUIRED": _Rule(
        category=ErrorCategory.auth, retryable=False, human_action_required=True
    ),
    "ACTION_REQUIRED": _Rule(
        category=ErrorCategory.action_required, retryable=False, human_action_required=True
    ),
    "PROFILE_BUSY": _Rule(category=ErrorCategory.profile_busy, retryable=True, human_action_required=False),
    "NETWORK_ERROR": _Rule(category=ErrorCategory.network, retryable=True, human_action_required=False),
    "PAGE_TIMEOUT": _Rule(category=ErrorCategory.network, retryable=True, human_action_required=False),
    "SELECTOR_MISMATCH": _Rule(
        category=ErrorCategory.page, retryable=False, human_action_required=True
    ),
    "PAGE_ERROR": _Rule(category=ErrorCategory.page, retryable=False, human_action_required=True),
    "OUTPUT_ERROR": _Rule(category=ErrorCategory.output, retryable=False, human_action_required=True),
    "BROWSER_CLOSED": _Rule(category=ErrorCategory.cancelled, retryable=False, human_action_required=False),
    "SKIPPED": _Rule(category=ErrorCategory.budget, retryable=False, human_action_required=False),
    # 真实 spec 逐词 error.code 中尚未登记、但已在机器可读协议里出现的码：
    # 补进现有分类表，不另起一套并行错误体系。
    "INVALID_ARGUMENT": _Rule(category=ErrorCategory.usage, retryable=False, human_action_required=True),
    "LOGIN_TIMEOUT": _Rule(category=ErrorCategory.auth, retryable=False, human_action_required=True),
    "BROWSER_UNAVAILABLE": _Rule(
        category=ErrorCategory.page, retryable=False, human_action_required=True
    ),
    "SEARCH_NAVIGATION_FAILED": _Rule(
        category=ErrorCategory.page, retryable=True, human_action_required=False
    ),
    # RESULTS_UNAVAILABLE：结果未加载且无法确认是空——必须可重试，绝不能当"没有需求"
    "RESULTS_UNAVAILABLE": _Rule(category=ErrorCategory.page, retryable=True, human_action_required=False),
    # PARTIAL_RESULT：有可解析部分，检查逐词并保留已有数据；本身不是失败
    "PARTIAL_RESULT": _Rule(category=ErrorCategory.system, retryable=False, human_action_required=False),
    "CANCELLED": _Rule(category=ErrorCategory.cancelled, retryable=False, human_action_required=False),
    "INTERNAL_ERROR": _Rule(category=ErrorCategory.system, retryable=False, human_action_required=True),
}

UNKNOWN_CODE = "UNKNOWN_CLASSIFICATION"

NETWORK_CODES = ("NETWORK_ERROR", "PAGE_TIMEOUT")
PAUSE_CODES = ("AUTH_REQUIRED", "ACTION_REQUIRED", "SELECTOR_MISMATCH", "PAGE_ERROR", "OUTPUT_ERROR")
# 进程级认证/验证退出码：不能被逐词网络抖动降级成"可重试"
HARD_STOP_EXITS = (3, 4)


def error_from_exit_code(exit_code: int, message: str, *, stderr_excerpt: str = "") -> ErrorInfo:
    """未知退出码不猜测语义：标记为需要人工核查的协议兼容性问题。"""
    rule = CLI_EXIT_TABLE.get(exit_code)
    if rule is None:
        return ErrorInfo(
            code=UNKNOWN_CODE,
            message=f"未登记的 CLI 退出码 {exit_code}：{message}",
            category=ErrorCategory.system,
            retryable=False,
            human_action_required=True,
            exit_code=exit_code,
            detail={"reason": "unknown_exit_code", "stderr_excerpt": stderr_excerpt[:400]},
        )
    if exit_code in (0, 7):
        # 0 是成功、7 是 partial，都不应造作为 failed 错误；调用方按状态处理。
        return ErrorInfo(
            code=UNKNOWN_CODE,
            message=f"退出码 {exit_code} 不属于失败分类，调用方应使用状态而非错误表达",
            category=rule.category,
            retryable=False,
            human_action_required=False,
            exit_code=exit_code,
        )
    return ErrorInfo(
        code=f"CLI_EXIT_{exit_code}",
        message=message,
        category=rule.category,
        retryable=rule.retryable,
        human_action_required=rule.human_action_required,
        exit_code=exit_code,
        detail={"stderr_excerpt": stderr_excerpt[:400]} if stderr_excerpt else {},
    )


def error_from_word_status(status: str, message: str) -> ErrorInfo:
    rule = WORD_STATUS_TABLE.get(status.upper())
    if rule is None:
        return ErrorInfo(
            code=UNKNOWN_CODE,
            message=f"未登记的逐词状态 {status}：{message}",
            category=ErrorCategory.system,
            retryable=False,
            human_action_required=True,
            detail={"reason": "unknown_word_status", "status": status},
        )
    return ErrorInfo(
        code=f"WORD_{status.upper()}",
        message=message,
        category=rule.category,
        retryable=rule.retryable,
        human_action_required=rule.human_action_required,
        detail={"status": status},
    )


def classify_cli_failure(
    exit_code: int,
    message: str,
    *,
    error_code: str | None = None,
    stderr_excerpt: str = "",
) -> ErrorInfo:
    """退出码与逐词 error.code 组合定性：认证/验证优先暂停，短暂网络故障才允许有限重试。

    退出码 5 同时覆盖浏览器、网络、页面与选择器错误，单看退出码无法区分，
    一律标为可重试或一律标为未知协议错误都不正确。规则：
    - 进程级 3/4（登录、验证）优先：不得被逐词状态降级成可重试；
    - 其余情况以可识别的逐词 error.code 定性（网络类有限重试，页面/选择器类暂停诊断）；
    - 只有退出码时按退出码表；两者都不认识时保持 UNKNOWN_CLASSIFICATION 并要人工核查。
    """
    status = (error_code or "").upper()
    fallback = error_from_exit_code(exit_code, message, stderr_excerpt=stderr_excerpt)
    if not status:
        return fallback
    if status not in WORD_STATUS_TABLE:
        unknown = error_from_word_status(status, message)
        detail = dict(unknown.detail)
        detail["exit_code_signal"] = str(exit_code)
        detail["exit_category_fallback"] = fallback.category.value
        if stderr_excerpt:
            detail["stderr_excerpt"] = stderr_excerpt[:400]
        return unknown.model_copy(update={"exit_code": exit_code, "detail": detail})
    if exit_code in HARD_STOP_EXITS:
        detail = dict(fallback.detail)
        detail.update({"word_status": status, "exit_code_signal": str(exit_code)})
        return fallback.model_copy(update={"detail": detail})
    info = error_from_word_status(status, message)
    detail = dict(info.detail)
    detail.update({"word_status": status, "exit_code_signal": str(exit_code)})
    if stderr_excerpt:
        detail["stderr_excerpt"] = stderr_excerpt[:400]
    return info.model_copy(update={"exit_code": exit_code, "detail": detail})


__all__ = [
    "ErrorInfo",
    "CLI_EXIT_TABLE",
    "WORD_STATUS_TABLE",
    "UNKNOWN_CODE",
    "NETWORK_CODES",
    "PAUSE_CODES",
    "HARD_STOP_EXITS",
    "error_from_exit_code",
    "error_from_word_status",
    "classify_cli_failure",
]
