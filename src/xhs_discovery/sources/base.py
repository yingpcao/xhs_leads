"""数据源接口：能力与浏览器实现分离，不要求每个数据源都支持正文和作者访问。

不支持的能力返回 unsupported（带说明），而不是伪装成空结果。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from xhs_discovery.models.enums import (
    Capability,
    CapabilityState,
    ErrorCategory,
    OperationType,
    ResultState,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.source import (
    AuthorPostsResult,
    Capabilities,
    OperationContext,
    PostBodyResult,
    PostSearchResult,
    SuggestionResult,
)
from xhs_discovery._version import PROTOCOL_VERSION


class AccessNotStarted(ValueError):
    """数据源**证明**这次调用没碰平台：请求在发出之前就失败（本地校验、会话未绑定、参数非法）。

    只有这个异常能让引擎把预留退回可用池。其它任何异常——包括"源在调用中途炸了"——
    都无法证明平台没收到过那次访问，按计划的保守口径照计（C-10）。
    仍继承 `ValueError`：它本来就是"调用方/数据集参数不对"这一类错误。
    """


def unsupported_error(capability: Capability, source_name: str, note: str) -> ErrorInfo:
    return ErrorInfo(
        code="CAPABILITY_UNSUPPORTED",
        message=f"{source_name} 不支持 {capability.value}：{note}",
        category=ErrorCategory.usage,
        retryable=False,
        human_action_required=False,
        detail={"capability": capability.value},
    )


class SearchSource(ABC):
    """一个数据源实例只服务一个 worker 进程；浏览器型数据源内部串行。"""

    name: str
    version: str
    synthetic: bool = False
    protocol_version: str = PROTOCOL_VERSION

    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    @abstractmethod
    def new_session(self) -> SessionRef:
        ...

    @abstractmethod
    def get_suggestions(self, context: OperationContext) -> SuggestionResult:
        ...

    def _unsupported_note(self, capability: Capability) -> str:
        return f"数据源 {self.name} 声明该能力为 unsupported/blocked"

    def session_matches_source(
        self, session: SessionRef, raw_responses: Sequence[bytes] = ()
    ) -> bool:
        """恢复路径核验落盘信封的 session 来源身份是否出自本数据源，默认与 name/version 精确相等。

        引擎在崩溃恢复时只有**恢复后重建的数据源实例**（由固化快照装配、来源已被核对）与
        **同一 attempt 登记的原始响应**（引擎已按登记的 sha/size 逐字节核对）可作权威；
        落盘信封里的 source_name/source_version 是产物自述，绝不反过来当作基准，
        也绝不按"格式像不像"放行——格式合法不等于就是那次采集的版本。各适配器若会话版本
        在采集时按工具自报版本精炼过（如 CLI），必须从 `raw_responses` 重新派生来核对；
        引擎不写死任何来源字面量。
        """
        return session.source_name == self.name and session.source_version == self.version

    def search_posts(self, context: OperationContext, *, limit: int) -> PostSearchResult:
        cap = self.capabilities()
        session = self.new_session()
        if not cap.is_supported(Capability.search_cards):
            return PostSearchResult(
                state=ResultState.unsupported,
                context=context.model_copy(update={"operation_type": OperationType.search}),
                session=session,
                error=unsupported_error(
                    Capability.search_cards, self.name, self._unsupported_note(Capability.search_cards)
                ),
            )
        return self._search_posts(context, limit=limit, session=session)

    def fetch_post_body(self, context: OperationContext, *, note_id: str) -> PostBodyResult:
        cap = self.capabilities()
        session = self.new_session()
        if cap.state_of(Capability.post_body) is not CapabilityState.supported:
            return PostBodyResult(
                state=ResultState.unsupported,
                context=context.model_copy(update={"operation_type": OperationType.body}),
                session=session,
                body=None,
                error=unsupported_error(
                    Capability.post_body, self.name, self._unsupported_note(Capability.post_body)
                ),
            )
        return self._fetch_post_body(context, note_id=note_id, session=session)

    def get_author_posts(
        self, context: OperationContext, *, author_ref: str, limit: int
    ) -> AuthorPostsResult:
        cap = self.capabilities()
        session = self.new_session()
        if cap.state_of(Capability.author_posts) is not CapabilityState.supported:
            return AuthorPostsResult(
                state=ResultState.unsupported,
                context=context.model_copy(update={"operation_type": OperationType.author_posts}),
                session=session,
                error=unsupported_error(
                    Capability.author_posts,
                    self.name,
                    self._unsupported_note(Capability.author_posts),
                ),
            )
        return self._get_author_posts(
            context, author_ref=author_ref, limit=limit, session=session
        )

    def _search_posts(
        self, context: OperationContext, *, limit: int, session: SessionRef
    ) -> PostSearchResult:
        raise NotImplementedError("数据源声明支持 search_cards 但未实现 _search_posts")

    def _fetch_post_body(
        self, context: OperationContext, *, note_id: str, session: SessionRef
    ) -> PostBodyResult:
        raise NotImplementedError("数据源声明支持 post_body 但未实现 _fetch_post_body")

    def _get_author_posts(
        self, context: OperationContext, *, author_ref: str, limit: int, session: SessionRef
    ) -> AuthorPostsResult:
        raise NotImplementedError("数据源声明支持 author_posts 但未实现 _get_author_posts")


__all__ = ["AccessNotStarted", "SearchSource", "unsupported_error"]
