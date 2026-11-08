"""规则问题抽取（P0-23）与可定位证据（P0-24）。

与 P0-C 的在线初步抽取（`discovery/extract.py`）相比，这一层只做三件本来就该由离线分析
做的事，其余口径一字不动，好让两版结果能对比：

1. **正文也进语料**：在线阶段只有标题（数据源能力是 title_only），离线分析拿到带来源的
   正文观察后按句切，问句在哪段就引哪段。
2. **一篇多问**：在线每条标题最多一个候选；这里按分句扫，一句一个候选（有上限并留审计），
   因为正文里"怎么排"和"要不要复读"确实是两个问题。
3. **补齐 `object`**：在线一直没填这一列。这里用领域词典取问题对象，缺失就留 NULL——
   计划规定"缺失属性不视为任意兼容"，用主题词硬填空位会造出一堆假兼容。

有效问题簇的判定沿用 `text/intent.py` 文档里写死却未在在线层生效的规则：
必须同时满足「用户求助来源 + 命中求助类意图 + 句中含求助线索」。在线层当时只要求
"能切出子句"，所以 P0-D 的簇可能比在线少——这是**新 `analysis_version` 下的新判定**，
在线行与历史奖励一律不改写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.corpus import TextUnit
from xhs_discovery.analysis.lexicon import DomainLexicon
from xhs_discovery.discovery.extract import HELP_INTENTS, TRAILING_PUNCT, core_text
from xhs_discovery.models.enums import PostSourceType, SourceField, StatementKind
from xhs_discovery.models.evidence import (
    Evidence,
    ProblemAttributes,
    ProblemCandidate,
)
from xhs_discovery.text.intent import SHARE_MARKERS, IntentMatcher
from xhs_discovery.text.normalize import KeywordNormalizer, normalize_text

EXTRACTION_METHOD = "rule_intent_segment_extract"

# 分句在**原文**上做：normalize_text 走 NFKC，会把全角 `，？` 折成半角，
# 拿规范化后的子句当 quote 就不再是原文子串，证据定位会直接失败。
# 句读断句，但疑问号跟着它所在的句子走：`……哪个好？` 里唯一的求助线索就是这个 `？`，
# 把它切掉，"句中含求助线索"就变成"这句碰巧长得像提问"，有效簇会漏掉一整类真问题。
SEGMENT_PUNCT = "，,。；;！!、\n\r\t "
QUESTION_PUNCT = "？?"

# 一篇正文最多产出几个问题候选。上限存在的理由不是性能而是记账：长正文里
# "怎么办"可能出现十几次，全部入库会把簇计数变成该文的情绪强度指标。
# 截断必须可见——它进 outcome 的 summary，不静默丢。
MAX_PROBLEMS_PER_UNIT = 3

REASON_NOT_QUESTION = "no_question_segment"
REASON_SUGGESTION = "suggestion_text_is_not_a_post_fact"

# 真正的"求助"要有疑问形式或求字句，光出现"怎么"不够：
# 「上岸经验分享，志愿怎么填的我不再说」里的"怎么"是叙述，不是提问。
EXPLICIT_HELP = re.compile(r"[？?]|求助|求问|有没有|该不该|要不要")


@dataclass(frozen=True)
class ExtractedProblem:
    """一条问题候选 + 它的来源单元 + 是否计入有效簇。"""

    candidate: ProblemCandidate
    unit: TextUnit
    statement_kind: StatementKind
    counts_as_valid: bool
    segment_index: int


@dataclass(frozen=True)
class ExtractionOutcome:
    problems: tuple[ExtractedProblem, ...]
    documents: int
    no_problem_documents: tuple[dict, ...]
    truncated_documents: int
    analysis_version: str
    extractor_version: str

    @property
    def valid_problems(self) -> tuple[ExtractedProblem, ...]:
        return tuple(problem for problem in self.problems if problem.counts_as_valid)

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(problem.candidate.evidence for problem in self.problems)

    def by_source_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for problem in self.problems:
            key = problem.candidate.source_type.value
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def by_statement_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for problem in self.problems:
            key = problem.statement_kind.value
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def summary(self) -> dict[str, object]:
        return {
            "analysis_version": self.analysis_version,
            "extractor_version": self.extractor_version,
            "documents": self.documents,
            "problems": len(self.problems),
            "valid_problems": len(self.valid_problems),
            "no_problem_documents": len(self.no_problem_documents),
            "truncated_documents": self.truncated_documents,
            "by_source_type": self.by_source_type(),
            "by_statement_kind": self.by_statement_kind(),
        }


def segments(text: str) -> list[str]:
    """原文断句：句读与疑问号都算断点，疑问号留在句子内，只剩标点的片段丢弃。"""
    parts: list[str] = []
    current: list[str] = []
    for char in text:
        if char in QUESTION_PUNCT:
            current.append(char)
            parts.append("".join(current))
            current = []
            continue
        if char in SEGMENT_PUNCT:
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [part for part in parts if core_text(part)]


def _object_for(segment: str, *, topic: str, lexicon: DomainLexicon) -> str | None:
    """问题对象取该句里最长且**不等于主题**的词典词；同长取更靠近疑问词的。

    只在单个分句内找：整篇正文的词典词会跨话题，"上海中考"和"民办费用"在同一段流水账里
    各说各话，取错对象比留 NULL 更糟——NULL 只是缺信息，错对象会造出假兼容。
    排除与主题同词的候选是因为 `topic` 列已经记了它，"上海中考怎么报名"的对象不是"上海中考"。
    """
    terms = [term for term in lexicon.segment(segment).terms if term != topic]
    if not terms:
        return None
    best = max(range(len(terms)), key=lambda i: (len(terms[i]), i))
    return terms[best]


class ProblemExtractor:
    """把语料切成"问题 + 可定位引用"，并判定它是否算用户求助。"""

    def __init__(
        self,
        *,
        matcher: IntentMatcher,
        normalizer: KeywordNormalizer,
        lexicon: DomainLexicon,
        analysis_version: str = ANALYSIS_VERSION,
        extractor_version: str = EXTRACTOR_VERSION,
        max_problems_per_unit: int = MAX_PROBLEMS_PER_UNIT,
    ) -> None:
        if max_problems_per_unit < 1:
            raise ValueError("max_problems_per_unit 至少为 1，否则抽取永远不会产出候选")
        self.matcher = matcher
        self.normalizer = normalizer
        self.lexicon = lexicon
        self.analysis_version = analysis_version
        self.extractor_version = extractor_version
        self.max_problems_per_unit = max_problems_per_unit

    def attributes_for(self, segment: str, unit: TextUnit) -> tuple[ProblemAttributes, str | None]:
        norm = self.normalizer.normalize(segment)
        marks = self.matcher.marks(norm.normalized)
        primary = self.matcher.primary(segment)
        intent = primary.intent.value if primary else None
        pain = primary.matched[0] if primary and primary.matched else None
        attributes = ProblemAttributes(
            topic=unit.keyword.normalized,
            object=_object_for(segment, topic=unit.keyword.normalized, lexicon=self.lexicon),
            intent=intent,
            pain_point=pain,
            location=norm.locations[0] if norm.locations else None,
            year=norm.years[0] if norm.years else None,
            polarity="negative" if (marks.negations or norm.has_negation) else None,
        )
        return attributes, intent

    def _source_type(self, text: str) -> PostSourceType:
        """来源判定：营销/机构优先排除，其次疑问形式，经验分享不因带"怎么"就升级成求助。

        `IntentMatcher.source_type` 的顺序是为在线层写的——那里宁可错认求助也不能漏掉用户
        提问。计划 §5.2 另外写明"经验分享不自动算用户求助"，而在线层不能改口径
        （改了等于换一套 P0-C 的簇计数与历史奖励），所以这条修正只在本分析层生效。
        """
        kind = self.matcher.source_type(text)
        if kind is not PostSourceType.user_help:
            return kind
        normalized = normalize_text(text)
        if not any(marker in normalized for marker in SHARE_MARKERS):
            return kind
        if EXPLICIT_HELP.search(normalized):
            return PostSourceType.user_help
        return PostSourceType.experience_share

    def _counts_as_valid(self, segment: str, source_type: PostSourceType, intent: str | None) -> bool:
        """用户求助 + 求助类意图 + 句内确有求助线索，三者缺一就不进簇计数。"""
        return (
            source_type is PostSourceType.user_help
            and intent in HELP_INTENTS
            and self.matcher.has_help_marker(segment)
        )

    @staticmethod
    def _is_question(segment: str, matcher: IntentMatcher, intent: str | None) -> bool:
        """够不够格当"问题"：有疑问线索，或在问一件求助类的事。

        只命中"资料/真题"这类资源词的标题不是问题（计划把纯资源标题单列），它们进审计
        而不是进 `problems`——把资源标题存成问题，需求图里就会出现一堆没有疑问的"需求"。
        """
        return matcher.has_help_marker(segment) or (intent in HELP_INTENTS and intent is not None)

    def extract_unit(self, unit: TextUnit) -> tuple[list[ExtractedProblem], dict | None]:
        """一个文本单元 → 若干问题候选；没有候选时返回一条"为什么没有"的审计记录。"""
        if unit.source_field is SourceField.suggestion:
            return [], {
                "source_field": unit.source_field.value,
                "reason": REASON_SUGGESTION,
                **unit.source_ref(),
            }
        source_type = self._source_type(unit.text)
        found: list[ExtractedProblem] = []
        for index, segment in enumerate(segments(unit.text)):
            primary = self.matcher.primary(segment)
            intent = primary.intent.value if primary else None
            if not self._is_question(segment, self.matcher, intent):
                continue
            evidence = Evidence.locate(
                container_text=unit.text,
                quote=segment,
                source_field=unit.source_field,
                provenance=unit.provenance(unit.run_id),
                extractor_version=self.extractor_version,
                analysis_version=self.analysis_version,
                synthetic=unit.synthetic,
                content_version_id=unit.content_version_id,
                suggestion_version_id=unit.suggestion_version_id,
            )
            attributes, _ = self.attributes_for(segment, unit)
            found.append(
                ExtractedProblem(
                    candidate=ProblemCandidate(
                        canonical_text=core_text(segment),
                        source_type=source_type,
                        attributes=attributes,
                        evidence=evidence,
                    ),
                    unit=unit,
                    statement_kind=StatementKind.explicit_problem,
                    counts_as_valid=self._counts_as_valid(segment, source_type, intent),
                    segment_index=index,
                )
            )
        if not found:
            return [], {
                "source_field": unit.source_field.value,
                "reason": REASON_NOT_QUESTION,
                "source_type": source_type.value,
                "question_segments": 0,
                **unit.source_ref(),
            }
        return found, None

    def extract(self, units) -> ExtractionOutcome:
        """语料 → 问题候选。联想文本不参与：它不是帖子事实（P0-26 另走意图候选）。"""
        problems: list[ExtractedProblem] = []
        no_problem: list[dict] = []
        truncated = 0
        documents = 0
        for unit in units:
            if unit.source_field is SourceField.suggestion:
                continue
            documents += 1
            found, miss = self.extract_unit(unit)
            if miss is not None:
                no_problem.append(miss)
                continue
            # 同一篇里重复出现的同一问句只留第一次：簇计数要数"几个问题"，不是"问了几遍"
            seen_texts: set[str] = set()
            kept: list[ExtractedProblem] = []
            for problem in found:
                text = problem.candidate.canonical_text
                if text in seen_texts:
                    continue
                seen_texts.add(text)
                kept.append(problem)
            if len(kept) > self.max_problems_per_unit:
                truncated += 1
            problems.extend(kept[: self.max_problems_per_unit])
        return ExtractionOutcome(
            problems=tuple(problems),
            documents=documents,
            no_problem_documents=tuple(no_problem),
            truncated_documents=truncated,
            analysis_version=self.analysis_version,
            extractor_version=self.extractor_version,
        )


__all__ = [
    "EXTRACTION_METHOD",
    "MAX_PROBLEMS_PER_UNIT",
    "REASON_NOT_QUESTION",
    "REASON_SUGGESTION",
    "ExtractedProblem",
    "ExtractionOutcome",
    "ProblemExtractor",
    "segments",
]
