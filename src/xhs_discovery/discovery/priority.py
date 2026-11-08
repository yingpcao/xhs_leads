"""候选优先级评分 P = .25R + .20N + .20I + .15E + .10C + .10U（P0-16）。

每条特征都同时记录原值与归一化值，并保存完整分解（`WeightedScore.features`）；
分解写进 `frontier.score_breakdown_json`，"为什么选这个词"必须能被复核。

三条不可省略的规则：
- 未做内容采样的候选，E 用配置里的中性先验并标 `is_prior=True`，不能冒充实际证据；
- generated 候选没有平台共识，C 记为"不可用"并触发权重重归一化（`effective_weight_total` < 1），
  而不是记 0 分——记 0 会把"还没观察"偷换成"观察过但不被认可"；
- 向量器按固定批次一次建好（同一份 IDF），同批候选分数可比，跨批不假装可比，
  因此模型版本里带批次语料哈希。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace

from xhs_discovery.config import PriorityWeights
from xhs_discovery.models.enums import CandidateOrigin
from xhs_discovery.models.scoring import (
    FeatureValue,
    WeightedScore,
    feature,
    missing_feature,
    prior_feature,
)
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.similarity import CharTfidf, VECTORIZER_VERSION

EVIDENCE_FULL_COUNT = 3
PARENT_CONSENSUS_CAP = 4


@dataclass(frozen=True)
class PriorityInput:
    """一个候选在本次批次里的全部已知事实。"""

    text: str
    origin: CandidateOrigin
    branch_key: str | None = None
    observed_parents: int = 0
    best_suggestion_rank: int | None = None
    sampled: bool = False
    explicit_evidence_count: int = 0
    branch_ucb: float | None = None
    branch_ucb_note: str | None = None

    @property
    def platform_signal(self) -> float:
        """平台推荐强度：第 1 位最强，排名越后越弱；没有排名就没有信号。"""
        if self.best_suggestion_rank is None:
            return 0.0
        return round(1.0 / self.best_suggestion_rank, 6)


@dataclass(frozen=True)
class PriorityBatch:
    """固定批次：锚词 + 已探索词 + 本批候选，共用一个字符 n-gram TF-IDF 向量器。"""

    anchors: tuple[str, ...]
    explored: tuple[str, ...]
    weights: PriorityWeights
    matcher: IntentMatcher
    unknown_evidence_prior: float = 0.5
    char_ngram_min: int = 2
    char_ngram_max: int = 4

    def corpus(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        parts: list[str] = []
        for text in (*self.anchors, *self.explored, *candidates):
            if text and text not in parts:
                parts.append(text)
        return tuple(parts)

    def known_bounds(self) -> tuple[int, int]:
        """锚词段与"锚词+已探索"段在去重语料里的边界。

        语料按 `anchors → explored → candidates` 顺序去重，候选只会追加在这两段之后，
        所以边界只由锚词与已探索词决定。用 `len(anchors)`/`len(explored)` 直接相加是错的：
        同名词被去重后语料变短，越界就是 IndexError，不越界也会把候选自己当比较基准。
        """
        parts: list[str] = []
        for text in self.anchors:
            if text and text not in parts:
                parts.append(text)
        anchor_end = len(parts)
        for text in self.explored:
            if text and text not in parts:
                parts.append(text)
        return anchor_end, len(parts)

    def model_version(self, candidates: tuple[str, ...]) -> str:
        digest = hashlib.sha256("|".join(self.corpus(candidates)).encode("utf-8")).hexdigest()[:12]
        return f"priority-heuristic.{VECTORIZER_VERSION}+batch-{digest}"


def _scaled_parents(count: int) -> float:
    """独立父路径计数取对数缩放：同一会话重复返回不会把共识推高。"""
    if count <= 0:
        return 0.0
    return round(min(1.0, math.log1p(count) / math.log1p(PARENT_CONSENSUS_CAP)), 6)


def _cosine_to(vectorizer: CharTfidf, text: str, indexes: range) -> float | None:
    values = [vectorizer.cosine(text, index) for index in indexes]
    if not values:
        return None
    return max(values)


def build_features(
    batch: PriorityBatch,
    vectorizer: CharTfidf,
    entry: PriorityInput,
) -> dict[str, FeatureValue]:
    weights = batch.weights.as_dict()
    anchor_end, known_end = batch.known_bounds()
    relevance = _cosine_to(vectorizer, entry.text, range(anchor_end))
    novelty = _cosine_to(vectorizer, entry.text, range(anchor_end, known_end))
    intent_score = batch.matcher.score(entry.text)

    features: dict[str, FeatureValue] = {}
    if relevance is None:
        features["R"] = missing_feature(weights["R"], "批次内没有可比锚词")
    else:
        features["R"] = feature(relevance, round(min(1.0, relevance), 6), weights["R"])
    if novelty is None:
        # 一个已探索词都没有：新颖度是"未定义"，不是"满分新颖"
        features["N"] = missing_feature(weights["N"], "尚无已探索词可比较新颖度")
    else:
        features["N"] = feature(novelty, round(max(0.0, 1.0 - novelty), 6), weights["N"])
    features["I"] = feature(intent_score, intent_score, weights["I"])
    if not entry.sampled:
        features["E"] = prior_feature(
            None, batch.unknown_evidence_prior, weights["E"], "未做内容采样，E 使用中性先验而不是实际证据"
        )
    else:
        ratio = min(1.0, entry.explicit_evidence_count / EVIDENCE_FULL_COUNT)
        features["E"] = feature(float(entry.explicit_evidence_count), round(ratio, 6), weights["E"])
    if entry.observed_parents <= 0:
        features["C"] = missing_feature(
            weights["C"],
            "generated 候选没有平台共识证据"
            if entry.origin is CandidateOrigin.generated
            else "尚未被平台联想返回",
        )
    else:
        features["C"] = feature(
            float(entry.observed_parents), _scaled_parents(entry.observed_parents), weights["C"]
        )
    if entry.branch_ucb is None:
        features["U"] = missing_feature(
            weights["U"], entry.branch_ucb_note or "分支统计不可用，U 分量不参与计算"
        )
    else:
        features["U"] = feature(entry.branch_ucb, round(min(1.0, max(0.0, entry.branch_ucb)), 6), weights["U"])
    return features


def evaluate_batch(
    batch: PriorityBatch, entries: tuple[PriorityInput, ...]
) -> list[tuple[PriorityInput, WeightedScore]]:
    """整批评分：向量器一次建好，随后每个候选都用同一份 IDF。"""
    texts = tuple(entry.text for entry in entries)
    corpus = batch.corpus(texts)
    vectorizer = CharTfidf(list(corpus), min_n=batch.char_ngram_min, max_n=batch.char_ngram_max)
    version = batch.model_version(texts)
    results: list[tuple[PriorityInput, WeightedScore]] = []
    for entry in entries:
        features = build_features(batch, vectorizer, entry)
        results.append(
            (
                entry,
                WeightedScore.compute(
                    features,
                    model_version=version,
                    provisional=len(entries) < 2,
                    provisional_reason=(
                        None if len(entries) >= 2 else "批次候选不足 2 个，分数仅供冷启动使用"
                    ),
                ),
            )
        )
    return results


def with_branch_ucb(entry: PriorityInput, ucb: float | None, *, note: str | None = None) -> PriorityInput:
    """调度器把分支 UCB 值挂到候选上（U 分量唯一的真实来源）。"""
    return replace(entry, branch_ucb=ucb, branch_ucb_note=note)


__all__ = [
    "EVIDENCE_FULL_COUNT",
    "PARENT_CONSENSUS_CAP",
    "PriorityBatch",
    "PriorityInput",
    "build_features",
    "evaluate_batch",
    "with_branch_ucb",
]
