"""版本与协议常量。

这些值进入 run 的 config_json、观察记录与 report.json，用于可复现与可解释的历史实验。
改变评分、分支、阈值或处理逻辑时必须提升对应的版本常量，不得覆盖历史结果。
"""

from __future__ import annotations

PROTOCOL_VERSION = "1"
"""引擎内部契约（模型字段与状态语义）版本。"""

CODE_VERSION = "0.0.1"
"""与 pyproject 的 project.version 一致；发布锁定后不再就地修改。"""

ANALYSIS_VERSION = "p0-rule-baseline.3"
"""派生分析（分词/短语 + 抽取 + 归并 + 评分 + 导出）整体处理版本。

`.3` 登记 C-11～C-15 累积改变的离线归并/日期锚点/评分聚合/回放口径（首次在此提升整体版本）：
C-15 r3 把观察标题版本的历史 NULL 引用从"取最新"改为"唯一候选才推断、多候选 unknown"，
正文来源聚合补齐 run/operation/attempt 自洽——回放、语料与 S 信号的读法随之改变，旧分析行/
导出不重写，新版本各写新行。`.2` 是 P0-D 规则分析层初版：写
`phrases/content_families/problems/needs/evidence/cluster_assignments` 的新行。P0-C 的在线
初步归并另用自己那一份常量（`discovery/merge.py::PRELIMINARY_ANALYSIS_VERSION =
"p0-preliminary.1"`），历史行与历史奖励都不动——同一 `analysis_version` 内幂等、跨版本各写一行，
正是"精修不篡改历史"的落库形态。
"""

EXTRACTOR_VERSION = "p0-rule-extract.2"
"""规则抽取器版本（P0-D 补齐对象/来源类型与证据定位；P0-C 的在线抽取仍是 p0-preliminary-extract.1）。"""

__all__ = [
    "PROTOCOL_VERSION",
    "CODE_VERSION",
    "ANALYSIS_VERSION",
    "EXTRACTOR_VERSION",
]
