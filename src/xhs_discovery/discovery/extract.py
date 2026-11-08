"""初步规则抽取（P0-18 的证据来源；P0-23 的完整意图抽取仍属 P0-D）。

这里的抽取只做到"能支撑在线奖励判定"的程度，刻意不覆盖 P0-D 的分析面：
- 从搜索标题里取一个含疑问/意图线索的子句作为显性问题候选（quote 可在内容版本原文定位）；
- 从联想文本里挑出疑问式表达，作为**搜索意图候选**单独登记，不建证据、不计簇奖励（P0-26）；
- 来源类型判定保守：营销、机构、经验分享都不自动算用户求助，因此不产生有效问题簇。

纯函数、不写库：证据 id 由 `merge.py` 落库后产生，抽取层不参与记账，
否则同一份抽取结果在两种记账顺序下会算出不同奖励。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from xhs_discovery.models.enums import PostSourceType, SourceField, StatementKind
from xhs_discovery.models.evidence import (
    Evidence,
    EvidenceProvenance,
    ProblemAttributes,
    ProblemCandidate,
)
from xhs_discovery.models.source import PostSearchResult, SuggestionResult
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.normalize import KeywordNormalizer, normalize_text

EXTRACTOR_VERSION = "p0-preliminary-extract.1"
CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?\s]+")
TRAILING_PUNCT = "？?。！!，,、；;：:"

HELP_INTENTS = {
    "method_guidance",
    "comparison_decision",
    "risk_avoidance",
    "information_query",
    "cost_constraint",
}


@dataclass(frozen=True)
class ProblemRecord:
    """一条候选 + 它落库需要的整数引用（模型层只带文本与哈希，不带主键）。"""

    candidate: ProblemCandidate
    keyword_id: int
    post_id: int | None
    operation_id: int
    attempt_no: int
    counts_as_valid: bool


@dataclass(frozen=True)
class IntentCandidateRecord:
    text: str
    canonical_text: str
    rank: int
    observation_id: int
    keyword_id: int
    statement_kind: StatementKind = StatementKind.search_intent_candidate


def _clauses(text: str) -> list[str]:
    return [clause for clause in CLAUSE_SPLIT.split(normalize_text(text)) if clause]


def select_quote(title: str, matcher: IntentMatcher) -> str:
    """优先取"真正提出问题"的子句；没有子句命中时整条标题作为引用（仍然可定位）。

    切分必须在原文上做：`normalize_text` 走 NFKC，会把全角 `，？` 折成半角，
    拿规范化后的子句当 quote 就不再是原文的子串，证据定位会直接失败。
    """
    for clause in (part for part in CLAUSE_SPLIT.split(title) if part):
        if matcher.has_help_marker(clause) or matcher.match(clause):
            return clause
    return title


def core_text(quote: str) -> str:
    """问题簇的规范文本：只做空白/半全角归一并去掉尾部标点，不改写词序或内容。"""
    body = normalize_text(quote)
    return body.strip(TRAILING_PUNCT)


def attributes_for(
    quote: str, *, topic: str, matcher: IntentMatcher, normalizer: KeywordNormalizer
) -> tuple[ProblemAttributes, str | None]:
    norm = normalizer.normalize(quote)
    marks = matcher.marks(norm.normalized)
    primary = matcher.primary(quote)
    intent = primary.intent.value if primary else None
    pain = primary.matched[0] if primary and primary.matched else None
    polarity = "negative" if (marks.negations or norm.has_negation) else None
    attributes = ProblemAttributes(
        topic=topic,
        intent=intent,
        pain_point=pain,
        location=norm.locations[0] if norm.locations else None,
        year=norm.years[0] if norm.years else None,
        polarity=polarity,
    )
    return attributes, intent


def extract_problems(
    run_id: str,
    result: PostSearchResult,
    posts: list,
    *,
    keyword_id: int,
    operation_id: int,
    attempt_no: int,
    matcher: IntentMatcher,
    normalizer: KeywordNormalizer,
    analysis_version: str,
) -> list[ProblemRecord]:
    """从本次搜索的标题里抽取显性问题候选（只对有标题版本、且判定为用户求助的帖子计簇）。"""
    records: list[ProblemRecord] = []
    for post in posts:
        if not post.title or post.title_version_id is None:
            continue
        quote = select_quote(post.title, matcher)
        provenance = EvidenceProvenance(
            run_id=run_id,
            operation=result.context,
            session_id=result.session.session_id,
            keyword=result.context.keyword,
            note_id=post.note_id,
        )
        evidence = Evidence.locate(
            container_text=post.title,
            quote=quote,
            source_field=SourceField.title,
            provenance=provenance,
            extractor_version=EXTRACTOR_VERSION,
            analysis_version=analysis_version,
            synthetic=result.session.synthetic,
            content_version_id=post.title_version_id,
        )
        attributes, intent = attributes_for(
            quote, topic=result.context.keyword.normalized, matcher=matcher, normalizer=normalizer
        )
        source_type = matcher.source_type(post.title)
        counts_as_valid = (
            source_type is PostSourceType.user_help
            and intent in HELP_INTENTS
            and bool(_clauses(quote))
        )
        records.append(
            ProblemRecord(
                candidate=ProblemCandidate(
                    canonical_text=core_text(quote),
                    source_type=source_type,
                    attributes=attributes,
                    evidence=evidence,
                ),
                keyword_id=keyword_id,
                post_id=post.post_id,
                operation_id=operation_id,
                attempt_no=attempt_no,
                counts_as_valid=counts_as_valid,
            )
        )
    return records


def extract_intent_candidates(
    result: SuggestionResult,
    children: list,
    *,
    observation_id: int,
    matcher: IntentMatcher,
) -> list[IntentCandidateRecord]:
    """联想里的疑问式表达登记为搜索意图候选：它不是帖子事实，也不参与有效簇计数。"""
    by_rank = {child.rank: child for child in children}
    records: list[IntentCandidateRecord] = []
    for item in result.items:
        child = by_rank.get(item.rank)
        if child is None:
            continue
        if not matcher.has_help_marker(item.text) and not matcher.match(item.text):
            continue
        records.append(
            IntentCandidateRecord(
                text=item.text,
                canonical_text=core_text(item.text),
                rank=item.rank,
                observation_id=observation_id,
                keyword_id=child.keyword_id,
            )
        )
    return records


__all__ = [
    "EXTRACTOR_VERSION",
    "HELP_INTENTS",
    "IntentCandidateRecord",
    "ProblemRecord",
    "attributes_for",
    "core_text",
    "extract_intent_candidates",
    "extract_problems",
    "select_quote",
]
