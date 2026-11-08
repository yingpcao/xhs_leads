"""关键词标准化（P0-13）。

只做两件事：全半角折叠（NFKC）与空白归一。地域、年份、年级、数字、否定词、比较对象
一律原样保留——它们是归并兼容判定的结构属性，不是噪声。

原始写法永远保留：`NormalizedKeyword.original` 与 `raw` 进 `KeywordText`，
歧义种子只标记、不改写（改写会同时毁掉用户输入和可复现性）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from xhs_discovery.models.source import KeywordText

# 年份、年级与"数值"作为结构属性提取，但绝不从文本里删除。
YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)|(?<!\d)\d{2}年")
GRADE_PATTERN = re.compile(r"[一二七八九]年级|初一|初二|初三|高一|高二|高三")
NEGATION_TERMS = ("不要", "别", "没", "无", "不", "无法", "难")
DIGITS_PATTERN = re.compile(r"\d+")


def fold(text: str) -> str:
    """全半角与字形兼容折叠；NFKC 会把全角数字/字母/标点转成半角。"""
    return unicodedata.normalize("NFKC", text)


def normalize_text(text: str) -> str:
    """规范化：只做 NFKC + 空白归一，其余一概不动。"""
    return " ".join(fold(text).split())


@dataclass(frozen=True)
class NormalizedKeyword:
    """规范化结果 + 结构属性 + 歧义标记，三者都不改写原文。"""

    original: str
    raw: str
    normalized: str
    is_ambiguous_seed: bool = False
    locations: tuple[str, ...] = ()
    years: tuple[str, ...] = ()
    grades: tuple[str, ...] = ()
    has_negation: bool = False
    digits: tuple[str, ...] = ()

    @property
    def structural(self) -> dict[str, object]:
        """参与归并兼容判定的结构属性；缺失就是缺失，不当作"任意兼容"。"""
        return {
            "locations": list(self.locations),
            "years": list(self.years),
            "grades": list(self.grades),
            "negation": self.has_negation,
            "digits": list(self.digits),
        }

    def as_keyword_text(self) -> KeywordText:
        return KeywordText(raw=self.raw, normalized=self.normalized)


class KeywordNormalizer:
    """把数据集/配置声明的歧义种子与地域词表接进标准化步骤。"""

    def __init__(self, *, ambiguous_seeds: tuple[str, ...] = (), locations: tuple[str, ...] = ()) -> None:
        self.ambiguous_seeds = tuple(sorted({normalize_text(seed) for seed in ambiguous_seeds}))
        self.locations = tuple(sorted({normalize_text(item) for item in locations}, key=len, reverse=True))

    def normalize(self, text: str) -> NormalizedKeyword:
        if not text or not text.strip():
            raise ValueError("关键词不得为空；空白关键词会让不同词共享同一身份")
        normalized = normalize_text(text)
        return NormalizedKeyword(
            original=text,
            raw=text.strip(),
            normalized=normalized,
            is_ambiguous_seed=self.is_ambiguous_seed(normalized),
            locations=self._locations(normalized),
            years=self._years(normalized),
            grades=tuple(sorted(set(GRADE_PATTERN.findall(normalized)))),
            has_negation=any(term in normalized for term in NEGATION_TERMS),
            digits=tuple(DIGITS_PATTERN.findall(normalized)),
        )

    def is_ambiguous_seed(self, normalized: str) -> bool:
        """只判定"是否属于声明过的歧义种子"，不据此改写用户输入。"""
        return normalized in self.ambiguous_seeds

    @staticmethod
    def _years(normalized: str) -> tuple[str, ...]:
        return tuple(sorted({match.group(0) for match in YEAR_PATTERN.finditer(normalized)}))

    def _locations(self, normalized: str) -> tuple[str, ...]:
        return tuple(sorted({item for item in self.locations if item in normalized}))


__all__ = ["NormalizedKeyword", "KeywordNormalizer", "fold", "normalize_text"]
