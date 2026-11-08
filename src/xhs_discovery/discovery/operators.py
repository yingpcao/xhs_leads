"""四类扩展算子（P0-15）：联想纵向、完整词组回退、内容横向、问题探针。

generated 与 observed 的分开是硬性的：
- observed 只来自平台真实返回（联想 items），由 `ingest_suggestion` 建 SUGGESTS 边；
- generated 是本地探针，入图时 origin=generated，并且必须带来源片段（证据 id），
  说明"这个词是从哪条观察推出来的"。没有来源片段的候选不评分，只作为回退线索入队。

裁剪与探针都是确定性字符串操作：不用模型、不用随机、模板固定，
因此"关闭所有模型也能完整运行"（P0-36）与重放一致性同时成立。
"""

from __future__ import annotations

from dataclasses import dataclass

from xhs_discovery.models.enums import CandidateOrigin
from xhs_discovery.models.evidence import KeywordCandidate
from xhs_discovery.models.source import KeywordText
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.normalize import NormalizedKeyword, normalize_text

# 结构词一旦裁掉就改变问题含义（"上海2025中考" 裁成 "上海中考" 是另一个事实），所以裁剪跳过它们
STRUCTURAL_SUBSTRINGS = ("年", "年级", "不", "没", "别", "无")


@dataclass(frozen=True)
class ExpansionInput:
    """一次扩展的输入：父词、它观察到的联想子词与内容、以及可用证据引用。"""

    run_id: str
    keyword_id: int
    keyword: KeywordText
    normalized: NormalizedKeyword
    branch_key: str
    depth: int
    children: tuple = ()
    posts: tuple = ()
    evidence_ids: tuple[int, ...] = ()
    # 已经探索过（或已判定不再产出）的子词规范化文本，用于回退触发与去重
    exhausted_children: tuple[str, ...] = ()
    seen_normalized: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OperatorSettings:
    rank_cutoff: int = 3
    max_candidates_per_operator: int = 3
    max_depth: int = 5
    max_segment_repeats: int = 2
    domain_terms: tuple[str, ...] = ()
    probe_templates: tuple[str, ...] = (
        "{topic} 怎么选",
        "{topic} 需要什么条件",
        "{topic} 有哪些坑",
    )

    @classmethod
    def from_settings(cls, settings, domain_terms: tuple[str, ...]) -> "OperatorSettings":
        """算子阈值走 scheduler 配置（全部显式可配，不留隐含常量）；rank_cutoff 固定为计划里的"首层前 3 位联想"。"""
        scheduler = settings.scheduler
        return cls(
            max_depth=scheduler.max_depth,
            max_segment_repeats=scheduler.max_segment_repeats,
            domain_terms=tuple(domain_terms),
        )


@dataclass(frozen=True)
class Proposal:
    operator: str
    text: str
    origin: CandidateOrigin
    rationale: str
    source_evidence_ids: tuple[int, ...] = ()

    def as_candidate(self, context: ExpansionInput, *, priority=None) -> KeywordCandidate:
        normalized = normalize_text(self.text)
        return KeywordCandidate(
            keyword=KeywordText(raw=self.text.strip(), normalized=normalized),
            origin=self.origin,
            operator=self.operator,
            parents=[context.keyword],
            branch_id=context.branch_key,
            depth=context.depth + 1,
            source_evidence_ids=list(self.source_evidence_ids),
            priority=priority,
            selection_reason=self.rationale,
        )


def is_degenerate_query(text: str, *, max_repeats: int) -> bool:
    """同一 2~3 字片段反复出现即判为退化查询。

    平台联想返回的是"输入前缀 + 补全"，把联想原文直接当下一层查询词时，链条会每层
    追加同一个片段（"…推荐菜推荐菜推荐菜"）。这类串既没有检索价值，也是风控验证码的
    直接诱因（run-20261031T021531Z 的 4 个失败操作全部落在 27~28 字的退化词上）。
    """
    if max_repeats <= 0:
        return False
    for size in (2, 3):
        counts: dict[str, int] = {}
        for start in range(len(text) - size + 1):
            segment = text[start : start + size]
            counts[segment] = counts.get(segment, 0) + 1
            if counts[segment] > max_repeats:
                return True
    return False


def suggest_vertical(context: ExpansionInput, settings: OperatorSettings) -> list[Proposal]:
    """联想纵向：把平台返回的高排名联想词作为下一层探索目标（observed）。

    深度上限只卡纵向：回退算子是裁短而不是加深，卡它会堵掉链条的出口。
    """
    if context.depth >= settings.max_depth:
        return []
    proposals: list[Proposal] = []
    for child in context.children:
        if child.rank > settings.rank_cutoff:
            continue
        if child.keyword_id == context.keyword_id:
            continue
        if child.normalized in context.exhausted_children:
            continue
        proposals.append(
            Proposal(
                operator="suggest_vertical",
                text=child.raw,
                origin=CandidateOrigin.observed,
                rationale=f"联想纵向：平台第 {child.rank} 位联想，继续向下探索",
            )
        )
        if len(proposals) >= settings.max_candidates_per_operator:
            break
    return proposals


def phrase_fallback(context: ExpansionInput, settings: OperatorSettings) -> list[Proposal]:
    """完整词组回退：裁掉尾部修饰词，保留一个仍然完整的词组。

    触发条件刻意保守——只有"这个父词没有任何可继续的联想"（空结果或子词都已耗尽）才回退，
    否则每个词都裁一遍会把队列淹在自造词里。
    """
    if context.children and not context.exhausted_children:
        return []
    tokens = context.normalized.normalized.split(" ")
    proposals: list[Proposal] = []
    for size in range(len(tokens) - 1, 0, -1):
        candidate = " ".join(tokens[:size])
        if len(candidate) < 2 or candidate == context.normalized.normalized:
            continue
        if any(term in candidate[-2:] for term in STRUCTURAL_SUBSTRINGS):
            # 裁到结构词尾巴上会留下"…不"这种半截表达，不是完整词组
            continue
        if candidate in context.seen_normalized:
            continue
        proposals.append(
            Proposal(
                operator="phrase_fallback",
                text=candidate,
                origin=CandidateOrigin.generated,
                rationale=f"完整词组回退：联想子词已耗尽，裁剪尾部修饰词保留父词前缀",
                source_evidence_ids=context.evidence_ids,
            )
        )
        if len(proposals) >= settings.max_candidates_per_operator:
            break
    return proposals


def content_lateral(context: ExpansionInput, settings: OperatorSettings) -> list[Proposal]:
    """内容横向：观察到的标题里出现的其他领域词，作为同层主题候选。"""
    if not settings.domain_terms:
        return []
    anchor = context.normalized.normalized
    titles = [post.title for post in context.posts if post.title]
    if not titles:
        return []
    proposals: list[Proposal] = []
    for term in sorted(set(settings.domain_terms)):
        term_norm = normalize_text(term)
        if not term_norm or term_norm in anchor:
            continue
        hits = [title for title in titles if term_norm in normalize_text(title)]
        if not hits:
            continue
        candidate = f"{anchor} {term_norm}"
        if candidate in context.seen_normalized:
            continue
        proposals.append(
            Proposal(
                operator="content_lateral",
                text=candidate,
                origin=CandidateOrigin.generated,
                rationale=f"内容横向：{len(hits)} 篇观察标题出现领域词「{term_norm}」",
                source_evidence_ids=context.evidence_ids,
            )
        )
        if len(proposals) >= settings.max_candidates_per_operator:
            break
    return proposals


def problem_probe(context: ExpansionInput, settings: OperatorSettings) -> list[Proposal]:
    """问题探针：给主题套固定疑问模板，主动探需求表达（必须有来源片段）。"""
    if not context.evidence_ids:
        return []
    anchor = context.normalized.normalized
    proposals: list[Proposal] = []
    for template in settings.probe_templates:
        candidate = normalize_text(template.format(topic=anchor))
        if candidate in context.seen_normalized:
            continue
        proposals.append(
            Proposal(
                operator="problem_probe",
                text=candidate,
                origin=CandidateOrigin.generated,
                rationale=f"问题探针：模板「{template}」应用于已观察到内容的主题",
                source_evidence_ids=context.evidence_ids,
            )
        )
        if len(proposals) >= settings.max_candidates_per_operator:
            break
    return proposals


OPERATORS = {
    "suggest_vertical": suggest_vertical,
    "phrase_fallback": phrase_fallback,
    "content_lateral": content_lateral,
    "problem_probe": problem_probe,
}


def expand(
    context: ExpansionInput,
    settings: OperatorSettings,
    *,
    enabled: tuple[str, ...] = tuple(OPERATORS),
    rejected: list[str] | None = None,
) -> list[KeywordCandidate]:
    """按固定算子顺序扩展；同一批内按规范化文本去重，不同算子各自保留来源。

    `rejected` 是可选的拒绝原因收集器：被深度上限挡掉的纵向扩展与被判退化的候选必须留痕，
    否则"这一支为什么不再长"只能靠猜，与"没采到"混成一回事。
    """
    unknown = [name for name in enabled if name not in OPERATORS]
    if unknown:
        raise ValueError(f"未实现的扩展算子：{unknown}")
    if (
        rejected is not None
        and "suggest_vertical" in enabled
        and context.depth >= settings.max_depth
    ):
        rejected.append(
            f"{context.normalized.normalized}：联想纵向已到深度上限 {settings.max_depth}，不再向下追联想"
        )
    seen: set[str] = set(context.seen_normalized)
    proposals: list[Proposal] = []
    for name in enabled:
        for proposal in OPERATORS[name](context, settings):
            normalized = normalize_text(proposal.text)
            if normalized in seen or normalized == context.normalized.normalized:
                continue
            if is_degenerate_query(normalized, max_repeats=settings.max_segment_repeats):
                if rejected is not None:
                    rejected.append(
                        f"{normalized}：同一片段重复超过 {settings.max_segment_repeats} 次，"
                        "判为退化查询，不入队"
                    )
                continue
            seen.add(normalized)
            proposals.append(proposal)
    return [proposal.as_candidate(context) for proposal in proposals]


def register_candidates(
    repo: Repository,
    run_id: str,
    candidates: list[KeywordCandidate],
    *,
    parent_keyword_id: int,
    branch_key: str,
    seen_at,
) -> list[tuple[int, KeywordCandidate]]:
    """把候选登记进 run_keywords（origin 按候选自身），generated 不冒充 observed 边。"""
    registered: list[tuple[int, KeywordCandidate]] = []
    for candidate in candidates:
        keyword_id = repo.ensure_keyword(
            candidate.keyword.normalized,
            candidate.keyword.raw,
            seen_at=seen_at,
        )
        existing = repo.run_keyword(run_id, keyword_id)
        if existing is None:
            repo.upsert_run_keyword(
                run_id,
                keyword_id,
                parent_keyword_id=parent_keyword_id,
                depth=candidate.depth,
                branch_id=repo.branch_id(run_id, branch_key),
                origin=candidate.origin.value,
                status="queued",
                seen_at=seen_at,
            )
        if candidate.origin is CandidateOrigin.generated and keyword_id != parent_keyword_id:
            repo.record_discovery_edge(
                run_id,
                parent_keyword_id,
                keyword_id,
                origin=CandidateOrigin.generated,
                session_id=None,
                observed_at=seen_at,
            )
        registered.append((keyword_id, candidate))
    return registered


__all__ = [
    "OPERATORS",
    "ExpansionInput",
    "OperatorSettings",
    "Proposal",
    "content_lateral",
    "expand",
    "is_degenerate_query",
    "phrase_fallback",
    "problem_probe",
    "register_candidates",
    "suggest_vertical",
]
