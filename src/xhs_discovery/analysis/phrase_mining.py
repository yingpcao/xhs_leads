"""短语发现（P0-22）：领域词典切词 + 字符 n-gram + TF-IDF + PMI，低频一律降级不硬造。

三个量各自负责一件事，缺一个都会留下"看起来能用其实会造假"的口子：

- **文档频次 `doc_freq`**：这个词/串出现在几条语料里。**不是**出现总次数——
  同一篇里复读十遍不等于十个独立来源，Q 分量与去重都按文档频次算。
- **TF-IDF**：`text.similarity.CharTfidf` 的 idf 权重，用来排"这批语料里谁稀奇"。
  向量器版本随批次进入结果行，同一批语料 + 同一版本 → 同一权重。
- **PMI（互信息，取所有切分点上的最小值，即 solidity）**：衡量"这几串在一起是真短语还是恰好
  路过"。只有频次没有 PMI，"的上海""海中考"这类跨词边也会被当成短语；只有 PMI 没有频次，
  一次性共现会因为样本太少而虚高。所以频次与 PMI 都要过阈值，各自产出 `kind` 不同的行：
  `ngram` 的 score 是 idf（稀有度），`pmi_phrase` 的 score 是 log2 PMI（粘合度）。同一串两种
  问法都成立，但绝不混在同一个 `score` 里——分数口径由 `score_basis` 随行说明。

**低频降级**是这一层的验收点，不是失败：文档频次低于 `nlp.min_phrase_freq`、PMI 低于
`nlp.min_pmi`、被领域词典整体覆盖的片段、以及不足以统计 PMI 的语料（<2 条），
都原样报告成 `SkippedPhrase`，由调用方写 `phrase_skipped` 事件——
"确实没发现短语"和"没跑发现"必须在库里分得开（DATA_MODEL 第 4 节 phrases）。

纯统计、不碰数据库：本模块只吃字符串、只出 `PhraseSet`，落库与事件在 `analysis/store.py`，
这样同一批语料的发现结果可以脱离 SQLite 复核。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Sequence

from xhs_discovery._version import EXTRACTOR_VERSION
from xhs_discovery.analysis.lexicon import DomainLexicon, Segmentation
from xhs_discovery.config import Settings
from xhs_discovery.text.similarity import CharTfidf

PhraseKind = Literal["word", "ngram", "pmi_phrase"]

SKIP_BELOW_FREQ = "below_min_phrase_freq"
SKIP_PMI_BELOW = "pmi_below_threshold"
SKIP_CORPUS_SMALL = "corpus_too_small_for_pmi"
SKIP_IN_DOMAIN_TERM = "covered_by_domain_term"

SCORE_BASIS_DOC_RATIO = "doc_freq_over_docs"
SCORE_BASIS_NGRAM_IDF = "char_ngram_idf"
SCORE_BASIS_PMI = "pmi_min_over_splits_log2"


@dataclass(frozen=True)
class PhraseCandidate:
    text: str
    kind: PhraseKind
    doc_freq: int
    score: float | None
    #: 该分数的口径说明，进导出与事件明细，便于复核"这个数是什么"
    score_basis: str
    pmi: float | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "text": self.text,
            "kind": self.kind,
            "freq": self.doc_freq,
            "score": self.score,
            "score_basis": self.score_basis,
            "pmi": self.pmi,
        }


@dataclass(frozen=True)
class SkippedPhrase:
    text: str
    kind: PhraseKind
    doc_freq: int
    reason: str
    detail: str

    def as_event(self) -> dict[str, object]:
        return {
            "text": self.text,
            "kind": self.kind,
            "doc_freq": self.doc_freq,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PhraseSet:
    """一次批次发现的结果：命中的短语、被降级的候选、以及这批统计自身的可解释边界。"""

    phrases: tuple[PhraseCandidate, ...]
    skipped: tuple[SkippedPhrase, ...]
    doc_count: int
    vectorizer_version: str
    extractor_version: str
    corpus_below_min_docs: bool = False

    def by_kind(self, kind: PhraseKind) -> tuple[PhraseCandidate, ...]:
        return tuple(phrase for phrase in self.phrases if phrase.kind == kind)

    @property
    def skipped_details(self) -> list[dict[str, object]]:
        return [item.as_event() for item in self.skipped]

    def summary(self) -> dict[str, object]:
        """写进 `phrase_mining` 事件的批次口径：分数从哪来、边界在哪。"""
        return {
            "doc_count": self.doc_count,
            "vectorizer_version": self.vectorizer_version,
            "extractor_version": self.extractor_version,
            "corpus_below_min_docs": self.corpus_below_min_docs,
            "counts": {
                kind: len(self.by_kind(kind)) for kind in ("word", "ngram", "pmi_phrase")
            },
            "skipped": len(self.skipped),
            "score_basis": {
                "word": SCORE_BASIS_DOC_RATIO,
                "ngram": SCORE_BASIS_NGRAM_IDF,
                "pmi_phrase": SCORE_BASIS_PMI,
            },
        }


def _occurrences(haystacks: Sequence[str], max_size: int) -> Counter[str]:
    """所有长度 1..max_size 子串的出现次数（允许重叠）：PMI 的分子分母都从这里来。"""
    counts: Counter[str] = Counter()
    for text in haystacks:
        for size in range(1, max_size + 1):
            if size > len(text):
                continue
            for start in range(len(text) - size + 1):
                counts[text[start : start + size]] += 1
    return counts


def _grams(texts: Sequence[str], size: int) -> set[str]:
    return {
        text[start : start + size]
        for text in texts
        for start in range(len(text) - size + 1)
    }


def _domain_spans(segment: Segmentation) -> tuple[tuple[int, int], ...]:
    """词典命中的字符区间（折叠串坐标）。

    只认**单个词内部**：`上海中考志愿填报` 里 `考志` 的两个字各自落在不同的词里，
    它是跨词边而非已知词的碎片，必须留给 PMI 判定。用逐字符 mask 会把这种跨词边
    误判成"词典已覆盖"，正好丢掉最该检查的那类假短语。
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for token in segment.tokens:
        if token.is_domain:
            spans.append((cursor, cursor + len(token.text)))
        cursor += len(token.text)
    return tuple(spans)


class PhraseMiner:
    """确定性短语发现器：阈值全部来自配置，不内置任何"看着合理"的魔法数。"""

    def __init__(
        self,
        lexicon: DomainLexicon,
        *,
        char_ngram_min: int = 2,
        char_ngram_max: int = 4,
        min_phrase_freq: int = 3,
        min_pmi: float = 1.0,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        if char_ngram_max < char_ngram_min:
            raise ValueError("char_ngram_max 必须 >= char_ngram_min")
        if char_ngram_min < 1:
            raise ValueError("char_ngram_min 至少为 1")
        if min_phrase_freq < 1:
            raise ValueError("min_phrase_freq 至少为 1，否则任何词都能'被发现'")
        self.lexicon = lexicon
        self.min_n = char_ngram_min
        self.max_n = char_ngram_max
        self.min_phrase_freq = min_phrase_freq
        self.min_pmi = min_pmi
        self.extractor_version = extractor_version

    @classmethod
    def from_settings(cls, settings: Settings, lexicon: DomainLexicon) -> "PhraseMiner":
        nlp = settings.nlp
        return cls(
            lexicon,
            char_ngram_min=nlp.char_ngram_min,
            char_ngram_max=nlp.char_ngram_max,
            min_phrase_freq=nlp.min_phrase_freq,
            min_pmi=nlp.min_pmi,
        )

    # ------------------------------------------------------------------ 主入口

    def mine(self, docs: Sequence[str]) -> PhraseSet:
        segments = [self.lexicon.segment(doc) for doc in docs if doc.strip()]
        texts = [segment.characters for segment in segments if segment.characters]
        if not texts:
            return PhraseSet(
                phrases=(),
                skipped=(
                    SkippedPhrase(
                        text="",
                        kind="ngram",
                        doc_freq=0,
                        reason=SKIP_CORPUS_SMALL,
                        detail="语料为空：没有可统计的文档频次，报缺失而不是零个短语",
                    ),
                ),
                doc_count=0,
                vectorizer_version=CharTfidf([]).version,
                extractor_version=self.extractor_version,
                corpus_below_min_docs=True,
            )

        vectorizer = CharTfidf(texts, min_n=self.min_n, max_n=self.max_n)
        occurrences = _occurrences(texts, self.max_n)
        covered = self._domain_covered_counts(segments, texts)
        doc_count = len(texts)

        phrases: list[PhraseCandidate] = []
        skipped: list[SkippedPhrase] = []
        phrases.extend(self._domain_words(texts, skipped))

        freq_by_gram = self._document_frequencies(texts)
        for size in range(self.min_n, self.max_n + 1):
            for gram in sorted(_grams(texts, size)):
                freq = freq_by_gram.get(gram, 0)
                if freq < self.min_phrase_freq:
                    skipped.append(
                        SkippedPhrase(
                            gram, "ngram", freq, SKIP_BELOW_FREQ,
                            f"文档频次 {freq} < nlp.min_phrase_freq {self.min_phrase_freq}",
                        )
                    )
                    continue
                if self._covered_by_domain(gram, covered, occurrences):
                    skipped.append(
                        SkippedPhrase(
                            gram, "ngram", freq, SKIP_IN_DOMAIN_TERM,
                            "只在词典词内部出现：重复计一遍不算新短语",
                        )
                    )
                    continue
                phrases.append(
                    PhraseCandidate(
                        text=gram,
                        kind="ngram",
                        doc_freq=freq,
                        score=round(vectorizer.idf[gram], 6),
                        score_basis=SCORE_BASIS_NGRAM_IDF,
                    )
                )

        phrases.extend(
            self._pmi_phrases(
                texts=texts,
                freq_by_gram=freq_by_gram,
                occurrences=occurrences,
                covered=covered,
                total_chars=sum(len(text) for text in texts),
                skipped=skipped,
            )
        )
        phrases.sort(key=lambda item: (item.kind, -(item.score or 0.0), item.text))
        skipped.sort(key=lambda item: (item.kind, item.doc_freq, item.text))
        return PhraseSet(
            phrases=tuple(phrases),
            skipped=tuple(skipped),
            doc_count=doc_count,
            vectorizer_version=vectorizer.version,
            extractor_version=self.extractor_version,
            corpus_below_min_docs=doc_count < 2,
        )

    # ------------------------------------------------------------------ 各类候选

    def _domain_words(self, texts: Sequence[str], skipped: list[SkippedPhrase]) -> list[PhraseCandidate]:
        """词典词按文档频次入选：`word` 的 score 是"出现在多少比例的语料里"，不是权重。"""
        out: list[PhraseCandidate] = []
        total = len(texts)
        for term in self.lexicon.terms:
            freq = sum(1 for text in texts if term in text)
            if freq < self.min_phrase_freq:
                skipped.append(
                    SkippedPhrase(
                        term, "word", freq, SKIP_BELOW_FREQ,
                        f"文档频次 {freq} < nlp.min_phrase_freq {self.min_phrase_freq}"
                        f"（本批 {total} 条语料）",
                    )
                )
                continue
            out.append(
                PhraseCandidate(
                    text=term,
                    kind="word",
                    doc_freq=freq,
                    score=round(freq / total, 6),
                    score_basis=SCORE_BASIS_DOC_RATIO,
                )
            )
        return out

    def _pmi_phrases(
        self,
        *,
        texts: Sequence[str],
        freq_by_gram: dict[str, int],
        occurrences: Counter[str],
        covered: Counter[str],
        total_chars: int,
        skipped: list[SkippedPhrase],
    ) -> list[PhraseCandidate]:
        """PMI 取所有切分点上的最小值：任何一处"像拼凑"就整体不算短语。"""
        if total_chars <= 0:
            return []
        if len(texts) < 2:
            skipped.append(
                SkippedPhrase(
                    text="*",
                    kind="pmi_phrase",
                    doc_freq=len(texts),
                    reason=SKIP_CORPUS_SMALL,
                    detail="少于 2 条语料：共现统计只会自我确认，PMI 降级为不产出",
                )
            )
            return []
        out: list[PhraseCandidate] = []
        # PMI 与 ngram 用同一个候选窗口：窗口外的串既没有频次结论也没有粘合度结论。
        for size in range(max(2, self.min_n), self.max_n + 1):
            for gram in sorted(_grams(texts, size)):
                freq = freq_by_gram.get(gram, 0)
                if freq < self.min_phrase_freq or self._covered_by_domain(gram, covered, occurrences):
                    continue  # 频次与词典覆盖已由 ngram 分支报告，这里不重复占用事件明细
                value = self._solidity(gram, occurrences, total_chars)
                if value < self.min_pmi:
                    skipped.append(
                        SkippedPhrase(
                            gram, "pmi_phrase", freq, SKIP_PMI_BELOW,
                            f"PMI {value:.4f} < nlp.min_pmi {self.min_pmi}",
                        )
                    )
                    continue
                out.append(
                    PhraseCandidate(
                        text=gram,
                        kind="pmi_phrase",
                        doc_freq=freq,
                        score=round(value, 6),
                        score_basis=SCORE_BASIS_PMI,
                        pmi=round(value, 6),
                    )
                )
        return out

    def _solidity(self, gram: str, occurrences: Counter[str], total_chars: int) -> float:
        whole = occurrences[gram]
        if whole <= 0 or len(gram) < 2:
            return float("-inf")
        worst = math.inf
        for split in range(1, len(gram)):
            left, right = occurrences[gram[:split]], occurrences[gram[split:]]
            if left <= 0 or right <= 0:
                return float("-inf")
            # log2( P(整) / (P(左)·P(右)) )，P 用"出现次数 / 语料字符总数"近似
            worst = min(worst, math.log2((whole * total_chars) / (left * right)))
        return worst if worst != math.inf else float("-inf")

    # ------------------------------------------------------------------ 计数辅助

    def _document_frequencies(self, texts: Sequence[str]) -> dict[str, int]:
        """文档频次：同一篇里出现多次只算一条。候选集是长度 1..max_n 的所有子串。"""
        frequencies: Counter[str] = Counter()
        for text in texts:
            for size in range(1, self.max_n + 1):
                frequencies.update(_grams([text], size))
        return dict(frequencies)

    def _domain_covered_counts(self, segments: Sequence[Segmentation], texts: Sequence[str]) -> Counter[str]:
        """统计"整段落在某一个词典命中区间内部"的子串出现次数。"""
        counts: Counter[str] = Counter()
        for segment, text in zip(segments, texts):
            if segment.characters != text:
                continue  # 折叠后为空的语料已在上层剔除，这里只对齐同一条记录
            for start, end in _domain_spans(segment):
                for size in range(1, self.max_n + 1):
                    counts.update(_grams([text[start:end]], size))
        return counts

    @staticmethod
    def _covered_by_domain(gram: str, covered: Counter[str], occurrences: Counter[str]) -> bool:
        """该串的出现**全部**落在词典词内部 → 它只是已知词的碎片，不是新短语。"""
        inside = covered[gram]
        return inside > 0 and inside >= occurrences.get(gram, 0)


__all__ = [
    "SCORE_BASIS_DOC_RATIO",
    "SCORE_BASIS_NGRAM_IDF",
    "SCORE_BASIS_PMI",
    "SKIP_BELOW_FREQ",
    "SKIP_CORPUS_SMALL",
    "SKIP_IN_DOMAIN_TERM",
    "SKIP_PMI_BELOW",
    "PhraseCandidate",
    "PhraseMiner",
    "PhraseSet",
    "SkippedPhrase",
]
