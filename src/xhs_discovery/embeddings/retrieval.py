"""确定性离线余弦检索（纯标准库）。

规则：维度不一致或非有限值直接拒绝；并列分数按 key 字典序稳定输出，
保证同一输入永远得到同一排序，可复现。
"""

from __future__ import annotations

import math


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"维度不一致：{len(a)} vs {len(b)}")
    for value in list(a) + list(b):
        if not math.isfinite(value):
            raise ValueError("含 NaN/Inf，拒绝计算相似度")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("零向量无方向，余弦未定义")
    return dot / (norm_a * norm_b)


def cosine_top_k(
    query: list[float], corpus: dict[str, list[float]], k: int
) -> list[tuple[str, float]]:
    """返回前 k 个 (key, score)；并列按 key 升序，确定性可复现。"""
    if k < 1:
        raise ValueError("k 必须 >= 1")
    scored = [(-cosine(query, vec), key) for key, vec in corpus.items()]
    scored.sort()
    return [(key, -neg) for neg, key in scored[:k]]


__all__ = ["cosine", "cosine_top_k"]
