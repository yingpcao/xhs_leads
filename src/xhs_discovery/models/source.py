"""数据源契约：能力声明、操作上下文与四类返回。

设计约束（DEVELOPMENT_PLAN.md 第 3.1 节）：
- 不支持的能力必须返回 unsupported，不能伪装成空结果；
- 失败样本不得计为需求缺失，因此 failed 结果强制 items 为空且 error 非空；
- 每次操作带 run/keyword/operation/revisit/attempt 逻辑键，供幂等导入与预算结算使用。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from xhs_discovery.models.common import (
    RawArtifactRef,
    SessionRef,
    StrictModel,
    sha256_text,
)
from xhs_discovery.models.enums import (
    Capability,
    CapabilityState,
    OperationType,
    ResultState,
)
from xhs_discovery.models.errors import ErrorInfo


class KeywordText(StrictModel):
    """原始写法与规范化写法同时保留；规范化只处理空白与全半角。"""

    raw: str = Field(min_length=1)
    normalized: str = Field(min_length=1)


class Capabilities(StrictModel):
    """不支持的能力必须显式表达，不能伪装为空结果。"""

    suggestions: CapabilityState
    search_cards: CapabilityState
    post_body: CapabilityState
    author_posts: CapabilityState

    def state_of(self, capability: Capability) -> CapabilityState:
        return getattr(self, capability.value)

    def is_supported(self, capability: Capability) -> bool:
        return self.state_of(capability) is CapabilityState.supported

    @classmethod
    def title_only(cls) -> "Capabilities":
        """当前 xhs-search CLI 的已核实能力边界。"""
        return cls(
            suggestions=CapabilityState.supported,
            search_cards=CapabilityState.supported,
            post_body=CapabilityState.unsupported,
            author_posts=CapabilityState.unsupported,
        )


class OperationContext(StrictModel):
    """操作逻辑键 = (run_id, keyword 标识, operation_type, revisit_seq)，尝试由 attempt_no 表示。

    keyword 标识优先使用入库后的 keyword_id；预入库（尚未取得 id）时必须显式设置
    allow_pre_registry，此时用规范化文本作稳定身份，避免两个不同关键词共享幂等键。
    """

    run_id: str = Field(min_length=1)
    keyword_id: int | None = Field(default=None, ge=1)
    keyword: KeywordText
    operation_type: OperationType
    revisit_seq: int = Field(ge=0)
    attempt_no: int = Field(ge=1)
    budget_reservation_id: int | None = Field(default=None, ge=1)
    allow_pre_registry: bool = False

    @model_validator(mode="after")
    def _check_identity(self) -> "OperationContext":
        if self.keyword_id is None and not self.allow_pre_registry:
            raise ValueError(
                "调度与预算扣减前必须已取得 keyword_id；"
                "预入库上下文必须显式设置 allow_pre_registry 并使用规范化文本身份"
            )
        return self

    @property
    def keyword_identity(self) -> str | int:
        return self.keyword_id if self.keyword_id is not None else f"norm:{self.keyword.normalized}"

    @property
    def logical_key(self) -> tuple[str, str | int, str, int]:
        return (self.run_id, self.keyword_identity, self.operation_type.value, self.revisit_seq)


class PerWordOutcome(StrictModel):
    """batch 模式逐词状态：partial 必须能定位到具体失败词。"""

    keyword: KeywordText
    state: ResultState
    error: ErrorInfo | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "PerWordOutcome":
        if self.state is ResultState.failed and self.error is None:
            raise ValueError("per-word failed 必须携带 error 分类，不能只报告状态")
        if self.state is ResultState.completed and self.error is not None:
            raise ValueError("per-word completed 不能同时携带 error")
        if self.state is ResultState.skipped and not self.warnings:
            raise ValueError("per-word skipped 必须用 warnings 说明跳过原因")
        return self


def _check_envelope(
    *,
    state: ResultState,
    error: ErrorInfo | None,
    items: list[object],
    label: str,
    partial_needs_per_word: bool,
    per_word: list[PerWordOutcome] | None = None,
    items_label: str = "items",
) -> None:
    """五态语义的唯一来源：completed 可以是有效的空结果，failed 不是"没有需求"。"""
    if state is ResultState.completed and error is not None:
        raise ValueError(f"{label}: completed 不得携带 error；部分失败请用 partial")
    if state is ResultState.failed:
        if error is None:
            raise ValueError(f"{label}: failed 必须携带 error 分类")
        if items:
            raise ValueError(f"{label}: failed 不得返回 {items_label}；可解析部分请用 partial")
    if state is ResultState.unsupported:
        if items:
            raise ValueError(f"{label}: unsupported 不得返回 {items_label}")
        if error is None:
            raise ValueError(f"{label}: unsupported 必须携带说明该能力不可用的 error")
    if state is ResultState.skipped and items:
        raise ValueError(f"{label}: skipped 不得返回 {items_label}")
    if partial_needs_per_word and state is ResultState.partial and not (per_word or []):
        raise ValueError(f"{label}: partial 必须提供逐词状态，才能区分有效与失败部分")


class SuggestionItem(StrictModel):
    text: str = Field(min_length=1)
    rank: int = Field(ge=1)


class _EnvelopeBase(StrictModel):
    state: ResultState
    context: OperationContext
    session: SessionRef
    error: ErrorInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    raw_artifacts: list[RawArtifactRef] = Field(default_factory=list)


class SuggestionResult(_EnvelopeBase):
    """联想观察：保留平台返回的原始顺序与会话标识。"""

    items: list[SuggestionItem] = Field(default_factory=list)
    per_word: list[PerWordOutcome] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_result(self) -> "SuggestionResult":
        if self.context.operation_type is not OperationType.suggest:
            raise ValueError("SuggestionResult 只能用于 suggest 操作")
        ranks = [item.rank for item in self.items]
        if ranks != sorted(ranks):
            raise ValueError("联想 items 必须保持平台返回的原始顺序")
        if len(set(ranks)) != len(ranks):
            raise ValueError("同一会话内联想 rank 不得重复")
        _check_envelope(
            state=self.state,
            error=self.error,
            items=self.items,
            per_word=self.per_word,
            partial_needs_per_word=True,
            label="SuggestionResult",
        )
        return self


class AuthorRef(StrictModel):
    """作者引用缺失时保留 unknown，不能把每篇当不同作者。"""

    kind: Literal["public_ref", "unknown"]
    public_ref: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "AuthorRef":
        if self.kind == "public_ref" and not self.public_ref:
            raise ValueError("author kind=public_ref 时 public_ref 必填")
        if self.kind == "unknown" and self.public_ref is not None:
            raise ValueError("author kind=unknown 时不得填写 public_ref")
        return self


class PostCard(StrictModel):
    """搜索卡片：标题、完整访问链接、可见字段与缺失标记。"""

    note_id: str = Field(min_length=1)
    access_url: str = Field(min_length=1)
    title: str | None = None
    title_missing: bool = False
    author: AuthorRef
    published_at_text: str | None = None
    published_at_missing: bool = False
    likes_text: str | None = None
    result_rank: int = Field(ge=1)
    search_keyword: KeywordText

    @model_validator(mode="after")
    def _check(self) -> "PostCard":
        if self.title is None and not self.title_missing:
            raise ValueError("title 为空时必须显式设置 title_missing")
        if self.published_at_text is None and not self.published_at_missing:
            raise ValueError("发布时间缺失时必须显式标记 published_at_missing")
        if not self.access_url.startswith(("http://", "https://")):
            raise ValueError("access_url 必须是可直接访问的完整链接")
        return self


class PostSearchResult(_EnvelopeBase):
    items: list[PostCard] = Field(default_factory=list)
    per_word: list[PerWordOutcome] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_result(self) -> "PostSearchResult":
        if self.context.operation_type is not OperationType.search:
            raise ValueError("PostSearchResult 只能用于 search 操作")
        note_ids = [item.note_id for item in self.items]
        if len(set(note_ids)) != len(note_ids):
            raise ValueError("单次结果内 note_id 必须去重")
        ranks = [item.result_rank for item in self.items]
        if ranks != sorted(ranks):
            raise ValueError("卡片必须保持结果页原始顺序")
        _check_envelope(
            state=self.state,
            error=self.error,
            items=self.items,
            per_word=self.per_word,
            partial_needs_per_word=True,
            label="PostSearchResult",
        )
        return self


class ContentVersionRef(StrictModel):
    """正文与标题变化通过内容版本保存，避免新文本覆盖旧证据。"""

    content_version_id: int | None = Field(default=None, ge=1)
    text: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _hash_matches(self) -> "ContentVersionRef":
        if sha256_text(self.text) != self.text_sha256:
            raise ValueError("内容版本哈希与文本不一致，证据定位不可信")
        return self


class PostBodyResult(_EnvelopeBase):
    """可选能力：正文。确实为空与不可访问必须分别表达。"""

    body: str | None = None
    body_is_empty: bool = False
    content_version: ContentVersionRef | None = None

    @model_validator(mode="after")
    def _check_result(self) -> "PostBodyResult":
        if self.context.operation_type is not OperationType.body:
            raise ValueError("PostBodyResult 只能用于 body 操作")
        items: list[object] = [self.body] if self.body else []
        _check_envelope(
            state=self.state,
            error=self.error,
            items=items,
            per_word=None,
            partial_needs_per_word=False,
            label="PostBodyResult",
            items_label="正文",
        )
        if self.state is ResultState.completed and self.body is None and not self.body_is_empty:
            raise ValueError("正文缺失时不能记 completed：不可访问应标 unsupported 或 blocked")
        if self.body:
            if self.content_version is None:
                raise ValueError("有正文时必须带内容版本，证据需要指向具体版本")
            if self.content_version.text != self.body:
                raise ValueError("内容版本文本必须与返回正文一致")
        return self


class AuthorPostsResult(_EnvelopeBase):
    """可选能力：作者公开笔记列表。"""

    items: list[PostCard] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_result(self) -> "AuthorPostsResult":
        if self.context.operation_type is not OperationType.author_posts:
            raise ValueError("AuthorPostsResult 只能用于 author_posts 操作")
        _check_envelope(
            state=self.state,
            error=self.error,
            items=self.items,
            per_word=None,
            partial_needs_per_word=False,
            label="AuthorPostsResult",
        )
        return self


__all__ = [
    "KeywordText",
    "Capabilities",
    "OperationContext",
    "PerWordOutcome",
    "SuggestionItem",
    "SuggestionResult",
    "AuthorRef",
    "PostCard",
    "PostSearchResult",
    "ContentVersionRef",
    "PostBodyResult",
    "AuthorPostsResult",
]
