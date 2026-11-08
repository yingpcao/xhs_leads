"""操作成本与相关性判定（P0-18/P0-19 的两个小依赖）。

`normalized_cost` 是奖励的分母，口径必须固定：
- 分母单位是"该类操作的一次计划内尝试"，所以一次成功的联想和一次成功的搜索都是 1.0，
  跨操作类型比较不会奖励某一类天生贵的动作；
- 重试计入成本（2 次尝试 = 2.0），所以"反复重试换一条线索"要真的产出才划算；
- 入库帖子数按参考权重累加：一次采 15 篇比采 3 篇贵，权重待 P4 实验校准，
  这里只是将"成本归操作"落成可比数字，不假装已经校准过。

无关判定保守：只有明显偏题才剔除并保存原因，边界词留在有限探索份额里。
"""

from __future__ import annotations

from dataclasses import dataclass

from xhs_discovery.config import Settings
from xhs_discovery.models.enums import OperationType
from xhs_discovery.text.normalize import KeywordNormalizer, normalize_text

# 一次"计划内"操作的参考尝试数：不同类型各自为 1，成本比较因此是同类内的相对值
REFERENCE_ATTEMPTS = {
    OperationType.suggest: 1.0,
    OperationType.search: 1.0,
    OperationType.body: 1.0,
    OperationType.author_posts: 1.0,
}
# 每入库一篇内容的附加成本权重（解析与存储成本），待实验校准，不是精确排名依据
POST_COST_WEIGHT = 0.05
MIN_NORMALIZED_COST = 0.25


@dataclass(frozen=True)
class OperationCost:
    operation_type: OperationType
    attempts: int
    posts_stored: int = 0

    @property
    def raw_units(self) -> float:
        return self.attempts + self.posts_stored * POST_COST_WEIGHT

    @property
    def normalized(self) -> float:
        reference = REFERENCE_ATTEMPTS[self.operation_type]
        return round(max(MIN_NORMALIZED_COST, self.raw_units / reference), 6)

    def as_dict(self) -> dict:
        return {
            "operation_type": self.operation_type.value,
            "attempts": self.attempts,
            "posts_stored": self.posts_stored,
            "raw_units": round(self.raw_units, 6),
            "normalized_cost": self.normalized,
            "post_cost_weight": POST_COST_WEIGHT,
        }


def score_tier(score: float | None, settings: Settings) -> tuple[str, int]:
    """采样优先级分档：分数缺失就是 low（未评分不得冒充高价值）。"""
    weights = settings.sampling
    if score is None:
        return "low", weights.low_value_posts
    if score >= weights.priority_high:
        return "high", weights.high_value_posts
    if score >= weights.priority_medium:
        return "medium", weights.medium_value_posts
    return "low", weights.low_value_posts


@dataclass(frozen=True)
class RelevanceJudgment:
    related: bool
    reason: str
    overlap: float

    @property
    def status(self) -> str:
        return "queued" if self.related else "irrelevant"


def judge_relevance(
    text: str,
    *,
    anchors: tuple[str, ...],
    domain_terms: tuple[str, ...],
    normalizer: KeywordNormalizer,
    min_overlap: int = 2,
) -> RelevanceJudgment:
    """保守判相关：与锚词有字符重叠、或命中领域词典，就保留为相关。

    只按"明显偏题"剔除：判据是重叠与词典都不命中。误剔一个词会把线索永久丢掉，
    而误留一个词只多花一次尝试预算，两个错误的代价不对称。
    """
    normalized = normalize_text(text)
    best_overlap = 0
    for anchor in anchors:
        best_overlap = max(best_overlap, _longest_common_run(normalized, normalize_text(anchor)))
    term_hit = next((term for term in domain_terms if normalize_text(term) in normalized), None)
    if best_overlap >= min_overlap:
        return RelevanceJudgment(True, f"与锚词有 {best_overlap} 字连续重叠", float(best_overlap))
    if term_hit is not None:
        return RelevanceJudgment(True, f"命中领域词典「{term_hit}」", 0.0)
    del normalizer  # 判定只用重叠与词典命中；normalizer 保留在签名里供后续加规则时不必改调用点
    return RelevanceJudgment(
        False, "与锚词无连续重叠且未命中领域词典（判为明显偏题）", float(best_overlap)
    )


def _longest_common_run(left: str, right: str) -> int:
    """最长公共连续片段长度（不依赖模型；两个短中文词有 2 字重叠通常同族）。"""
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


__all__ = [
    "MIN_NORMALIZED_COST",
    "POST_COST_WEIGHT",
    "REFERENCE_ATTEMPTS",
    "OperationCost",
    "RelevanceJudgment",
    "judge_relevance",
    "score_tier",
]
