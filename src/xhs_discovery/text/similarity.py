"""字符 n-gram 与 TF-IDF 相似度（P0-16 的 R/N 特征输入）。

只用字符 n-gram：中文不强制分词，jieba/sklearn 都不在核心依赖里（P0-01 的"无模型可导入"
约束）。实现是纯 Python 且确定性的：同一批语料 + 同一版本常量 → 同一向量与同一分数。
"""

from __future__ import annotations

import math
from collections import Counter

from xhs_discovery._version import ANALYSIS_VERSION
from xhs_discovery.text.normalize import normalize_text

VECTORIZER_VERSION = f"char-ngram-{ANALYSIS_VERSION}"


def ngrams(text: str, *, min_n: int = 2, max_n: int = 4) -> list[str]:
    body = normalize_text(text).replace(" ", "")
    if not body:
        return []
    sizes = range(min_n, max(min_n, max_n) + 1)
    grams: list[str] = []
    for size in sizes:
        if size > len(body):
            continue
        grams.extend(body[i : i + size] for i in range(len(body) - size + 1))
    return grams


def jaccard(a: str, b: str, *, min_n: int = 2, max_n: int = 4) -> float:
    """新颖度 N 用的重叠度量：对称、有界，且对短词不会因为长度差而虚高。"""
    set_a, set_b = set(ngrams(a, min_n=min_n, max_n=max_n)), set(ngrams(b, min_n=min_n, max_n=max_n))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


class CharTfidf:
    """固定批次刷新的字符 n-gram TF-IDF 向量器；`version` 进入评分模型版本。"""

    def __init__(self, corpus: list[str], *, min_n: int = 2, max_n: int = 4) -> None:
        self.min_n = min_n
        self.max_n = max_n
        self.version = VECTORIZER_VERSION
        self.doc_count = len(corpus)
        counts: Counter[str] = Counter()
        for doc in corpus:
            counts.update(set(ngrams(doc, min_n=min_n, max_n=max_n)))
        self.idf = {
            term: math.log((self.doc_count + 1) / (freq + 1)) + 1.0 for term, freq in counts.items()
        }
        self._corpus_vectors = [self._vector(doc) for doc in corpus]

    def _vector(self, text: str) -> dict[str, float]:
        grams = Counter(ngrams(text, min_n=self.min_n, max_n=self.max_n))
        total = sum(grams.values())
        if not total:
            return {}
        vector = {
            term: (freq / total) * self.idf.get(term, math.log(self.doc_count + 1) + 1.0)
            for term, freq in grams.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm == 0:
            return {}
        return {term: value / norm for term, value in vector.items()}

    def cosine(self, text: str, index: int) -> float:
        """候选与语料第 index 篇的余弦相似度（已归一化，点积即余弦；负值按 0 处理）。"""
        left, right = self._vector(text), self._corpus_vectors[index]
        if not left or not right:
            return 0.0
        return max(0.0, sum(value * right[term] for term, value in left.items() if term in right))

    def max_similarity(self, text: str) -> float:
        if not self._corpus_vectors:
            return 0.0
        return max((self.cosine(text, index) for index in range(len(self._corpus_vectors))), default=0.0)


def max_similarity(text: str, others: list[str], *, min_n: int = 2, max_n: int = 4) -> float:
    """新颖度 N 用：与已探索词的最大重叠（Jaccard），不依赖批次向量器。"""
    if not others:
        return 0.0
    grams = set(ngrams(text, min_n=min_n, max_n=max_n))
    if not grams:
        return 0.0
    best = 0.0
    for other in others:
        other_grams = set(ngrams(other, min_n=min_n, max_n=max_n))
        if not other_grams:
            continue
        union = grams | other_grams
        best = max(best, len(grams & other_grams) / len(union))
    return best


__all__ = ["VECTORIZER_VERSION", "CharTfidf", "jaccard", "max_similarity", "ngrams"]
