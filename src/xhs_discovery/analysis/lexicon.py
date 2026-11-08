"""领域词典分词（P0-22）。

为什么自己做而不用通用分词器：核心闭包不允许依赖 jieba/sklearn（P0-01 钉住的"无模型也能跑"
约束，`tests/test_environment.py` 会在子进程里断言这些模块没被导入）。本项目的语料极窄
（中考相关的关键词与标题），**领域词典 + 最长匹配**已经够用，而且完全确定性：同一份词典
加同一句输入永远得到同一串切分，能被复现，也能被 review。

切分只读不改写：`normalize_text` 之后按位置切，地域/年份/否定词都留在原串里
（P0-13 的口径）。分词结果只用于统计与短语发现，**不反过来改写关键词身份**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from xhs_discovery.config import Settings, load_domain_terms
from xhs_discovery.text.normalize import normalize_text

TokenKind = Literal["domain", "digit", "latin", "char"]

DIGIT_RUN = re.compile(r"\d+(?:\.\d+)?")
LATIN_RUN = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Token:
    text: str
    kind: TokenKind
    start: int
    end: int

    @property
    def is_domain(self) -> bool:
        return self.kind == "domain"


@dataclass(frozen=True)
class Segmentation:
    """一次切分的结果：tokens 覆盖整段文本（除空白），并给出词典覆盖率。"""

    text: str
    tokens: tuple[Token, ...]

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(token.text for token in self.tokens if token.is_domain)

    @property
    def characters(self) -> str:
        """去掉空白后的字符序列：n-gram 与 PMI 的计数基座。"""
        return "".join(token.text for token in self.tokens)

    @property
    def domain_coverage(self) -> float:
        """词典命中的字符占比。低覆盖率意味着语料超出词典范围——只报告，不硬凑词。"""
        total = sum(len(token.text) for token in self.tokens)
        if total == 0:
            return 0.0
        matched = sum(len(token.text) for token in self.tokens if token.is_domain)
        return matched / total


class DomainLexicon:
    """按"最长优先"做贪心匹配的领域词典；词典外文本逐字符成 token，不猜词。"""

    def __init__(self, terms: Iterable[str]) -> None:
        normalized = {normalize_text(term) for term in terms if normalize_text(term)}
        if not normalized:
            raise ValueError("领域词典为空：分词会退化成逐字符，短语统计失去意义")
        self.terms = tuple(
            sorted(normalized, key=lambda term: (-len(term), term))
        )
        self._by_length: dict[int, tuple[str, ...]] = {}
        for term in self.terms:
            self._by_length.setdefault(len(term), ())
            self._by_length[len(term)] = (*self._by_length[len(term)], term)
        self._lengths = tuple(sorted(self._by_length, reverse=True))
        self._lookup = normalized

    def is_term(self, text: str) -> bool:
        return normalize_text(text) in self._lookup

    def segment(self, text: str) -> Segmentation:
        body = normalize_text(text)
        tokens: list[Token] = []
        index = 0
        while index < len(body):
            char = body[index]
            if char.isspace():
                index += 1
                continue
            matched = self._match_at(body, index)
            if matched is not None:
                tokens.append(Token(text=matched, kind="domain", start=index, end=index + len(matched)))
                index += len(matched)
                continue
            run = DIGIT_RUN.match(body, index) or LATIN_RUN.match(body, index)
            if run is not None:
                kind: TokenKind = "digit" if run.group(0)[0].isdigit() else "latin"
                tokens.append(
                    Token(text=run.group(0), kind=kind, start=index, end=run.end())  # type: ignore[arg-type]
                )
                index = run.end()
                continue
            tokens.append(Token(text=char, kind="char", start=index, end=index + 1))
            index += 1
        return Segmentation(text=body, tokens=tuple(tokens))

    def _match_at(self, body: str, index: int) -> str | None:
        for length in self._lengths:
            if index + length > len(body):
                continue
            candidate = body[index : index + length]
            if candidate in self._lookup:
                return candidate
        return None


def load_lexicon(settings: Settings) -> DomainLexicon:
    """从配置声明的词典文件构建分词器：文件缺失或重复词条直接报错。

    不静默退化成逐字符——那样短语发现照样"跑通"，只是每次切分都不一样，
    而没人能从结果里看出来。
    """
    return DomainLexicon(load_domain_terms(Path(settings.nlp.domain_terms_path)))


__all__ = ["DomainLexicon", "Segmentation", "Token", "TokenKind", "load_lexicon"]
