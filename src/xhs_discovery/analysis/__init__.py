"""P0-D 规则分析层（离线、无模型、可复现）。

与 `discovery/`（在线主循环）的分工：`discovery` 在跑动中做**初步**归并并即时更新奖励，
本包在 run 结束后从库里的观察事实重算**规则基线**分析，写新的 `analysis_version` 行。
历史行与历史奖励不改写（DEVELOPMENT_PLAN 第 5.2 节末尾）。

模块划分：`lexicon`（领域词典分词）→ `phrase_mining`（n-gram/TF-IDF/PMI）→
`extraction`（八类意图与问题/需求候选抽取）→ `clustering`（保守归并与稳定需求 ID）→
`scoring`（S/Q/A/F 与内容家族去重）→ `graph`（主题→问题→需求→证据图）→
`export`（五类产物与反读校验）→ `store`（分析结果入库，唯一碰写库的一层）→
`pipeline`（编排）。

本包不导入 jieba/sklearn/streamlit/torch 等重型依赖：闭环保离线（tests/test_environment.py
钉住这条约束），且同一批输入必须给出同一批结果。
"""

from __future__ import annotations

from xhs_discovery.analysis.lexicon import DomainLexicon, Token
from xhs_discovery.analysis.phrase_mining import PhraseCandidate, PhraseMiner, PhraseSet

__all__ = [
    "DomainLexicon",
    "PhraseCandidate",
    "PhraseMiner",
    "PhraseSet",
    "Token",
]
