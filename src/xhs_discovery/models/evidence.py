"""证据、问题、需求与归并契约。

规则（DEVELOPMENT_PLAN.md 第 4 节、第 5.4 节）：
- 证据必须带 source_field、quote、字符位置与所指内容版本，且能在对应版本原文定位；
- 显性问题、推断需求、方案构想、搜索意图候选四类严格区分，方案构想不得当作已验证需求；
- 归并是可修改的关系，原句永远保留；属性冲突不自动合并。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from xhs_discovery.models.common import StrictModel, sha256_text
from xhs_discovery.models.enums import (
    CandidateOrigin,
    PostSourceType,
    SourceField,
    StatementKind,
)
from xhs_discovery.models.scoring import WeightedScore
from xhs_discovery.models.source import KeywordText, OperationContext


class EvidenceProvenance(StrictModel):
    """证据回溯到具体 run、操作、尝试与会话。"""

    run_id: str = Field(min_length=1)
    operation: OperationContext
    session_id: str = Field(min_length=1)
    keyword: KeywordText
    note_id: str | None = None
    suggestion_rank: int | None = Field(default=None, ge=1)


class Evidence(StrictModel):
    """可在对应内容版本原文定位的证据引用。"""

    evidence_id: int | None = Field(default=None, ge=1)
    source_field: SourceField
    quote: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    container_text: str
    container_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_version_id: int | None = Field(default=None, ge=1)
    suggestion_version_id: int | None = Field(default=None, ge=1)
    provenance: EvidenceProvenance
    extractor_version: str = Field(min_length=1)
    analysis_version: str = Field(min_length=1)
    synthetic: bool

    @model_validator(mode="after")
    def _must_locate(self) -> "Evidence":
        if sha256_text(self.container_text) != self.container_sha256:
            raise ValueError("证据容器文本哈希不一致，无法证明定位可信")
        if self.container_text[self.char_start : self.char_end] != self.quote:
            raise ValueError(
                f"quote 无法在对应内容版本原文 [{self.char_start}:{self.char_end}] 定位"
            )
        if self.provenance.run_id != self.provenance.operation.run_id:
            raise ValueError("证据 provenance 的 run_id 与操作上下文不一致")
        if self.content_version_id is not None and self.suggestion_version_id is not None:
            raise ValueError("证据只能指向一张版本表：content_version_id 与 suggestion_version_id 互斥")
        if self.source_field is SourceField.suggestion and self.provenance.note_id is not None:
            raise ValueError("suggestion 证据不应携带 note_id")
        if self.source_field is not SourceField.suggestion and self.provenance.note_id is None:
            raise ValueError("title/body 证据必须携带 note_id 才能回溯帖子")
        return self

    @classmethod
    def locate(
        cls,
        *,
        container_text: str,
        quote: str,
        source_field: SourceField,
        provenance: EvidenceProvenance,
        extractor_version: str,
        analysis_version: str,
        synthetic: bool,
        occurrence: int = 0,
        content_version_id: int | None = None,
        suggestion_version_id: int | None = None,
        evidence_id: int | None = None,
    ) -> "Evidence":
        """由原文与片段构造证据：位置由容器文本计算，不接受手写偏移。"""
        if not quote:
            raise ValueError("quote 不得为空")
        start = -1
        for _ in range(occurrence + 1):
            start = container_text.find(quote, start + 1)
            if start == -1:
                raise ValueError(
                    f"quote 在对应内容版本原文中不存在（第 {occurrence + 1} 次出现），拒绝入库"
                )
        return cls(
            evidence_id=evidence_id,
            source_field=source_field,
            quote=quote,
            char_start=start,
            char_end=start + len(quote),
            container_text=container_text,
            container_sha256=sha256_text(container_text),
            content_version_id=content_version_id,
            suggestion_version_id=suggestion_version_id,
            provenance=provenance,
            extractor_version=extractor_version,
            analysis_version=analysis_version,
            synthetic=synthetic,
        )

    def locates_in(self, container_text: str) -> bool:
        return (
            sha256_text(container_text) == self.container_sha256
            and container_text[self.char_start : self.char_end] == self.quote
        )


class ProblemAttributes(StrictModel):
    """缺失属性不等于任意兼容：只有双方都存在且冲突时才判定不可合并。"""

    topic: str | None = None
    object_: str | None = Field(default=None, alias="object")
    intent: str | None = None
    pain_point: str | None = None
    location: str | None = None
    year: str | None = None
    polarity: str | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def conflicts_with(self, other: "ProblemAttributes") -> list[str]:
        conflicts = []
        for name in ("topic", "object_", "intent", "pain_point", "location", "year", "polarity"):
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine and theirs and mine != theirs:
                conflicts.append(name)
        return conflicts


class ProblemCandidate(StrictModel):
    """显性问题候选：原句永远保留。"""

    problem_id: int | None = Field(default=None, ge=1)
    canonical_text: str = Field(min_length=1)
    source_type: PostSourceType
    attributes: ProblemAttributes
    evidence: Evidence

    @property
    def counts_as_user_help(self) -> bool:
        """营销、机构发布与经验分享不自动算用户求助。"""
        return self.source_type is PostSourceType.user_help


class NeedStatement(StrictModel):
    """需求表达：显性/推断/方案/意图候选分区，只有前两类可参与有效需求计数。"""

    need_id: int | None = Field(default=None, ge=1)
    statement_kind: StatementKind
    canonical_text: str = Field(min_length=1)
    attributes: ProblemAttributes
    evidence_ids: list[int] = Field(default_factory=list)
    analysis_version: str = Field(min_length=1)
    synthetic: bool
    verified: bool = False

    @model_validator(mode="after")
    def _check(self) -> "NeedStatement":
        if self.statement_kind is StatementKind.solution_idea and self.verified:
            raise ValueError("方案构想不能标记为已验证需求")
        if (
            self.statement_kind
            in (StatementKind.explicit_problem, StatementKind.inferred_need)
            and not self.evidence_ids
        ):
            raise ValueError(f"{self.statement_kind} 需求必须至少引用一条 evidence_id")
        if self.statement_kind is StatementKind.search_intent_candidate and self.verified:
            raise ValueError("联想意图候选只是搜索意图线索，不能冒充帖子事实")
        return self

    @property
    def counts_as_valid_need(self) -> bool:
        return (
            self.statement_kind in (StatementKind.explicit_problem, StatementKind.inferred_need)
            and len(self.evidence_ids) > 0
        )


class ClusterAssignment(StrictModel):
    """归并是可撤销的关系记录，不覆盖历史结果。"""

    assignment_id: int | None = Field(default=None, ge=1)
    problem_id: int = Field(ge=1)
    need_id: int = Field(ge=1)
    method: str = Field(min_length=1)
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    attributes_conflict: list[str] = Field(default_factory=list)
    auto_merged: bool = False
    analysis_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "ClusterAssignment":
        if self.auto_merged and self.attributes_conflict:
            raise ValueError("存在属性冲突时禁止自动合并")
        if self.auto_merged and self.similarity is None:
            raise ValueError("自动合并必须记录相似度，便于复核阈值")
        return self


class ContentFamily(StrictModel):
    """近重复文本归入同一内容家族：评分抑制重复贡献，但不删除帖子或作者。"""

    family_id: int | None = Field(default=None, ge=1)
    representative_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    method: str = Field(min_length=1)
    member_count: int = Field(ge=1)
    analysis_version: str = Field(min_length=1)


class KeywordCandidate(StrictModel):
    """候选词：generated 与 observed 分开，未验证候选不得建 SUGGESTS 边。"""

    candidate_id: int | None = Field(default=None, ge=1)
    keyword: KeywordText
    origin: CandidateOrigin
    operator: str = Field(min_length=1)
    parents: list[KeywordText] = Field(default_factory=list)
    branch_id: str = Field(min_length=1)
    depth: int = Field(ge=0)
    source_evidence_ids: list[int] = Field(default_factory=list)
    priority: WeightedScore | None = None
    selection_reason: str | None = None
    sample_kind: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "KeywordCandidate":
        if self.origin is CandidateOrigin.observed and not self.parents:
            raise ValueError("observed 候选必须有至少一个真实父词，用于保留发现路径")
        if (
            self.origin is CandidateOrigin.generated
            and self.priority is not None
            and not self.source_evidence_ids
        ):
            raise ValueError("generated 候选必须带来源片段引用，说明探针从何而来")
        return self


__all__ = [
    "EvidenceProvenance",
    "Evidence",
    "ProblemAttributes",
    "ProblemCandidate",
    "NeedStatement",
    "ClusterAssignment",
    "ContentFamily",
    "KeywordCandidate",
]
