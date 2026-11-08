"""离线规则文本层：标准化、结构属性与相似度。"""

from xhs_discovery.text.normalize import (
    KeywordNormalizer,
    NormalizedKeyword,
    fold,
    normalize_text,
)

__all__ = ["KeywordNormalizer", "NormalizedKeyword", "fold", "normalize_text"]
