"""规则意图匹配（P0-16 的 I 特征；P0-23 的抽取器复用同一套规则）。

放在这里而不是 discovery 里，是因为"意图分数"同时被选词评分和问题抽取使用：
两处各写一份模式匹配，迟早会算出不同的 I，评分轨迹就不可复现了。

来源类型判定顺序是刻意的：营销/机构标题里也常出现"怎么"，
若先判疑问标记就会把营销标题当成用户求助，直接污染有效问题簇计数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from xhs_discovery.config import IntentRules
from xhs_discovery.models.enums import IntentCode, PostSourceType
from xhs_discovery.text.normalize import normalize_text

INTENT_MATCHER_VERSION = "rule-intent.1"

# 求助/疑问线索：有效问题簇必须至少命中一条，否则只是经验分享或资源标题
HELP_MARKERS = ("怎么", "如何", "怎样", "吗", "呢", "？", "?", "求助", "求问", "有没有", "求", "该不该", "要不要")
MARKETING_MARKERS = ("招生", "报名", "咨询", "课程", "一对一", "集训", "优惠", "限时", "加V", "加v", "机构", "辅导", "培训")
INSTITUTION_MARKERS = ("官方", "考试院", "教委", "教育局", "学校通知", "公告", "简章", "政策发布")
SHARE_MARKERS = ("经验", "上岸", "复盘", "记录", "分享", "笔记", "总结", "走过")


@dataclass(frozen=True)
class IntentMatch:
    intent: IntentCode
    score: float
    matched: tuple[str, ...]


@dataclass(frozen=True)
class StructureMarks:
    """归并兼容判定要用的结构线索；命中与否都不改写原文。"""

    negations: tuple[str, ...] = ()
    comparisons: tuple[str, ...] = ()


class IntentMatcher:
    def __init__(self, rules: IntentRules) -> None:
        self.rules = rules
        self.version = f"{INTENT_MATCHER_VERSION}+rules.v{rules.version}"
        self._by_intent: dict[IntentCode, tuple[str, ...]] = {
            rule.intent: tuple(normalize_text(p) for p in rule.patterns) for rule in rules.rules
        }
        self._weights = {rule.intent: rule.weight for rule in rules.rules}
        self._negations = tuple(normalize_text(p) for p in rules.negation_patterns)
        self._comparisons = tuple(normalize_text(p) for p in rules.comparison_patterns)

    def match(self, text: str) -> tuple[IntentMatch, ...]:
        normalized = normalize_text(text)
        found: list[IntentMatch] = []
        for intent, patterns in self._by_intent.items():
            hits = tuple(p for p in patterns if p and p in normalized)
            if not hits:
                continue
            # 命中越多模式，意图越强；权重只影响同类内的相对强度，不引入未验证的经验系数
            score = min(1.0, len(hits) / max(1, min(3, len(patterns)))) * self._weights[intent]
            found.append(IntentMatch(intent=intent, score=round(score, 6), matched=hits))
        return tuple(sorted(found, key=lambda m: (-m.score, m.intent.value)))

    def primary(self, text: str) -> IntentMatch | None:
        matches = self.match(text)
        return matches[0] if matches else None

    def score(self, text: str) -> float:
        """I 特征：意图规则命中强度，没有任何命中就是 0（不是"未知"）。"""
        matches = self.match(text)
        if not matches:
            return 0.0
        top = matches[0].score
        extra = sum(m.score for m in matches[1:]) * 0.25
        return round(min(1.0, top + extra), 6)

    def marks(self, text: str) -> StructureMarks:
        normalized = normalize_text(text)
        return StructureMarks(
            negations=tuple(sorted({p for p in self._negations if p in normalized})),
            comparisons=tuple(sorted({p for p in self._comparisons if p in normalized})),
        )

    @staticmethod
    def has_help_marker(text: str) -> bool:
        normalized = normalize_text(text)
        return any(marker in normalized for marker in HELP_MARKERS) or bool(
            re.search(r"[？?]", normalized)
        )

    def source_type(self, text: str) -> PostSourceType:
        """营销/机构优先排除；经验分享与纯资源标题不自动算用户求助。"""
        normalized = normalize_text(text)
        if any(marker in normalized for marker in MARKETING_MARKERS):
            return PostSourceType.marketing
        if any(marker in normalized for marker in INSTITUTION_MARKERS):
            return PostSourceType.institution
        if self.has_help_marker(normalized):
            return PostSourceType.user_help
        if any(marker in normalized for marker in SHARE_MARKERS):
            return PostSourceType.experience_share
        return PostSourceType.unknown


__all__ = ["HELP_MARKERS", "INTENT_MATCHER_VERSION", "IntentMatch", "IntentMatcher", "StructureMarks"]
