"""五类产物导出与反读校验（P0-29）。

`DEVELOPMENT_PLAN` 第 6 节的 P0 退出门槛要求交出五个文件：keywords.csv、needs.csv、
evidence.csv、graph.graphml、report.json。本模块只从**库里的行**重建它们——不接受调用方
传入的内存对象。否则导出内容与数据库状态分成两套真相，反读校验就无从对照，看板显示的
"需求"也可能只是某次运行时恰好活在那儿的那个列表。

四条不能糊过去的规则：

1. **展示口径**：`needs.csv` 只收 `evidence_status IN (verified, inferred)`、`score IS NOT NULL`
   且在需求图自检里没有失效引用的行。0 分是真实但过时的需求，NULL 分是根本没有可定位证据，
   两者在 `report.json` 的统计里分列——排行榜少一行必须能说清为什么少。
2. **文本安全**：CSV 单元格以 ``=``、``+``、``-``、``@``、制表符或回车开头时前置一个撇号，
   并把 XML/CSV 都装不下的 NUL 换成替换符；GraphML 属性值去掉 XML 1.0 非法字符。
   转义规则同时进反读校验：写进去什么，读回来就必须是什么。
3. **GraphML 类型**：每种属性只声明一种 `attr.type`，复合值（列表、字典）与类型不一致的同名
   属性统一 JSON 序列化成 `string`，并在 `report.json` 里点名被降级成字符串的属性；
   节点 id 用 `<类型前缀>_<序号>`，精确身份另存 `ref` 属性——`topic:` 后面的中文标签不是
   合法的 XML `ID`，硬塞进 id 会让文件在别家的解析器里直接报错。
4. **不改写历史**：产物落在 `runs/<run>/exports/<analysis_version>/`，内容寻址。同一版本重跑
   而内容变了，沿用 `write_artifact` 的冲突保护报错，由调用方显式给 `snapshot` 换目录——
   悄悄覆盖上一份导出会让已经发出去的引用不可复现。

`artifacts` 表登记五类产物的路径与哈希，是 UI/CLI 唯一的取用入口；文件本身仍由
`storage/artifacts.py` 落盘，因此导出与原始响应共用同一套原子写、内容哈希与重启补导入纪律。
本模块除 `artifacts` 与事件外不写业务表，尤其不碰 `branch_stats`。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

from pydantic import ValidationError

from xhs_discovery._version import ANALYSIS_VERSION, CODE_VERSION, EXTRACTOR_VERSION, PROTOCOL_VERSION
from xhs_discovery.analysis.clustering import MERGE_METHOD
from xhs_discovery.analysis.graph import (
    DISPLAYED_STATUSES,
    NODE_PREFIX,
    DemandGraphBuilder,
    Graph,
    GraphEdge,
    GraphIntegrity,
    GraphNode,
    build_discovery_graph,
)
from xhs_discovery.analysis.scoring import FAMILY_METHOD, SCORING_METHOD, SCORING_MODEL_VERSION
from xhs_discovery.config import Settings
from xhs_discovery.discovery.extract import core_text
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import OperationState, SourceField
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import migrations
from xhs_discovery.storage.artifacts import ArtifactConflict, write_artifact
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository

EXPORT_DIRNAME = "exports"
"""导出目录：`runs/<run>/exports/<analysis_version>/`，与采集产物分开放，补导入时一眼能认出归属。"""

EXPORT_FILES = (
    ("keywords.csv", "keywords_csv"),
    ("needs.csv", "needs_csv"),
    ("evidence.csv", "evidence_csv"),
    ("graph.graphml", "graphml"),
    ("report.json", "report_json"),
)
"""文件名与 `artifacts.kind` 的唯一对应表；顺序就是落盘顺序（report.json 要嵌前四个的哈希）。"""

KEYWORD_COLUMNS = (
    "keyword_id", "normalized", "aliases", "origin", "depth", "status", "branch_key", "is_seed",
    "parents", "parent_keyword_id", "parent_normalized", "independent_parents",
    "suggestion_observations", "suggestions_returned", "best_suggestion_rank", "distinct_sessions",
    "revisit_count", "posts_observed", "distinct_posts",
    "title_evidence", "body_evidence", "suggestion_evidence",
    "problems_extracted", "displayable_needs",
    "first_seen_at", "last_seen_at", "analysis_version", "extractor_version",
)

NEED_COLUMNS = (
    "need_id", "stable_key", "canonical_text", "statement_kind", "category", "evidence_status",
    "displayable", "score", "scoring_method", "score_model_version",
    "S_raw", "S_normalized", "S_weight", "S_missing_reason",
    "Q_raw", "Q_normalized", "Q_weight", "Q_missing_reason",
    "A_raw", "A_normalized", "A_weight", "A_missing_reason",
    "F_raw", "F_normalized", "F_weight", "F_missing_reason",
    "effective_weight_total", "provisional", "provisional_reason", "unknown_components",
    "sessions", "parent_paths", "evidence_row_sessions", "evidence_row_parent_paths",
    "best_rank", "consensus_signal", "position_signal",
    "evidence_rows", "distinct_posts", "content_families",
    "q_counted_posts", "q_family_suppressed_posts", "q_post_absorbed_families",
    "q_suppression_reasons", "duplicate_versions_suppressed",
    "distinct_known_authors", "posts_without_author_ref",
    "published_dates_parsed", "published_dates_unparseable", "published_dates_without_anchor",
    "latest_published_at", "window_reference_at", "window_reference_source",
    "topics", "problems", "problem_ids", "merge_methods",
    "evidence_ids", "note_ids", "source_keywords",
    "source_fields", "generated_expression", "derived_from_stable_key",
    "created_at", "updated_at", "analysis_version", "extractor_version", "score_json",
)

EVIDENCE_COLUMNS = (
    "evidence_id", "quote", "source_field", "container_field", "char_start", "char_end",
    "locatable", "container_kind", "content_version_id", "suggestion_version_id",
    "container_sha256", "container_text_sha256", "container_captured_at", "container_is_missing",
    "post_id", "note_id", "access_url", "keyword_id", "keyword_normalized", "session_id",
    "operation_id", "operation_type", "attempt_no", "result_rank", "published_at_text",
    "post_observation_id", "published_at_observed_at",
    "suggestion_rank", "problem_id", "counts_as_valid_problem",
    "need_ids", "need_stable_keys", "on_displayed_need",
    "extractor_version", "analysis_version", "synthetic", "created_at",
)

COLUMNS_BY_FILENAME = {
    "keywords.csv": KEYWORD_COLUMNS,
    "needs.csv": NEED_COLUMNS,
    "evidence.csv": EVIDENCE_COLUMNS,
}

FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")
"""电子表格会把这些开头的单元格当公式执行；导出的是文本，不是可执行内容。"""

GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
CORE_NODE_ATTRS = ("ref", "kind", "label")
CORE_EDGE_ATTRS = ("relation", "source_ref", "target_ref")
_XML_ILLEGAL = frozenset(
    [chr(code) for code in range(0x00, 0x09)] + [chr(0x0B), chr(0x0C)]
    + [chr(code) for code in range(0x0E, 0x20)]
)
NOTABLE_EVENTS = (
    "artifact_orphaned", "imported_after_restart", "branch_stalled", "branch_suspended",
    "candidates_dropped", "cluster_revoked", "cluster_skipped", "demand_score_undisplayable",
    "demand_score_unknown_component", "failed", "forced_takeover", "graph_path_truncated",
    "attempt_charged_after_failure", "paused", "phrase_skipped", "stale_lock_removed",
    "uncertain_operations",
)
NOTABLE_EVENT_PREFIXES = ("stop_",)
"""异常清单按事件名挑选，而不是把全部事件塞进报告：调度类事件是运行流水，不是异常。"""

EXPORT_SELF_EVENTS = ("analysis_export", "export_verification_failed")
"""导出自己写的事件：统计口径里必须排除，否则 report.json 永远追不上自己那一条。"""

DISPLAY_BLOCKING_CODES = (
    "need_without_evidence",
    "evidence_row_missing",
    "evidence_quote_broken",
    "suggestion_as_post_fact",
    "candidate_uses_post_evidence",
)
"""让需求掉出排行榜的自检码：证据点不开或来源类型造假，就不配占一个名次。"""


class ExportConflict(RuntimeError):
    """同一 run 与分析版本下产物内容变了：显式换快照目录，不覆盖已登记的历史导出。"""


class ExportVerificationError(RuntimeError):
    """反读校验没过：文件或数据库登记与重建结果不一致，宁可不出货。"""


# ------------------------------------------------------------------ 文本安全


def csv_cell(value: object) -> str:
    """一个单元格：None 是空串，标量按原样，列表与字典按 JSON，公式字符前置撇号。"""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float, str)):
        text = str(value) if isinstance(value, str) else json.dumps(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = text.replace("\x00", "\ufffd")
    if text[:1] in FORMULA_CHARS:
        return "'" + text
    return text


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """按 RFC 4180 读回：表头与行分隔都交给 csv 模块，不自己 split。

    返回**原始单元格列表**而不是字典：短行被字典化会静默补 KeyError，
    反读校验要能看出"某一行少了一列"这种破损。
    """
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def xml_text(value: str) -> str:
    """去掉 XML 1.0 不允许出现的控制字符：这些字符写进属性会让整个文件解析失败。"""
    if not value:
        return value
    return "".join("\ufffd" if ch in _XML_ILLEGAL else ch for ch in value)


def render_csv(columns: Iterable[str], rows: Iterable[dict]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\n", restval="", extrasaction="raise"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({name: csv_cell(row.get(name)) for name in columns})
    return buffer.getvalue()


def render_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ------------------------------------------------------------------ GraphML


def _graphml_type(values: Iterable[object]) -> tuple[str, bool]:
    """返回 (attr.type, 是否需要整体 JSON 化)。

    int 与 float 混在一起升级到 double；出现布尔与数字混用、字符串与数字混用这类
    无法靠加宽解决的组合，就把该属性的值全部序列化成字符串——GraphML 的一个 `key`
    只能声明一种类型，宁可类型信息变粗，也不让别家的解析器在半个值上崩掉。
    """
    seen: set[str] = set()
    complex_value = False
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            seen.add("boolean")
        elif isinstance(value, int):
            seen.add("long")
        elif isinstance(value, float):
            seen.add("double")
        elif isinstance(value, str):
            seen.add("string")
        else:
            complex_value = True
            seen.add("string")
    if not seen:
        return ("string", False) if not complex_value else ("string", True)
    if complex_value:
        return ("string", True)
    if seen <= {"long", "double"}:
        return ("double", False) if "double" in seen else ("long", False)
    if len(seen) == 1:
        return (next(iter(seen)), False)
    return ("string", True)


def _graphml_value(declared: str, stringify: bool, value: object) -> str | None:
    if value is None:
        return None
    if stringify:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if declared == "boolean":
        return "true" if value else "false"
    if declared in ("long", "double"):
        return json.dumps(value)
    return xml_text(str(value))


def merge_graphs(*graphs: Graph) -> Graph:
    """把发现图与需求图并进同一份 GraphML：节点按 id 合并，属性以先出现的口径为准。

    同一个 `kw:` 节点在两张图里都出现——发现图带调度事实（深度、分支、重探次数），
    需求图只带"这条证据来自哪个词"。合并顺序决定谁赢，所以调用方必须先给发现图。
    """
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for graph in graphs:
        for node in graph.nodes:
            existing = nodes.get(node.node_id)
            if existing is None:
                nodes[node.node_id] = node
                continue
            nodes[node.node_id] = GraphNode(
                kind=existing.kind,
                node_id=existing.node_id,
                label=existing.label or node.label,
                attributes={**node.attributes, **existing.attributes},
            )
        edges.extend(graph.edges)
    if not graphs:
        raise ValueError("合并空图列表没有意义")
    return Graph(
        run_id=graphs[0].run_id,
        analysis_version=graphs[0].analysis_version,
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(edges),
    )


@dataclass(frozen=True)
class GraphMLPlan:
    document: str
    nodes: int
    edges: int
    declared_types: dict[str, str]
    stringified_attrs: tuple[str, ...]


def _node_values(node: GraphNode) -> dict[str, object]:
    """节点的全部可见属性：核心三件（身份、类型、标签）与业务属性同层，但类型固定为 string。"""
    return {"ref": node.node_id, "kind": node.kind, "label": node.label, **node.attributes}


def _edge_values(edge: GraphEdge) -> dict[str, object]:
    return {
        "relation": edge.relation,
        "source_ref": edge.source,
        "target_ref": edge.target,
        **edge.attributes,
    }


def render_graphml(graph: Graph) -> GraphMLPlan:
    """一张图 → 一份合法 GraphML；节点 id 是 `<类型前缀>_<序号>`，精确身份在 `ref` 属性里。"""
    nodes = sorted(graph.nodes, key=lambda item: item.node_id)
    for node in nodes:
        if node.kind not in NODE_PREFIX:
            raise ValueError(f"未知的节点类型 {node.kind!r}")
        clash = set(node.attributes) & set(CORE_NODE_ATTRS)
        if clash:
            raise ValueError(f"节点属性 {sorted(clash)} 与 GraphML 核心属性同名，无法导出")
    edge_clash: set[str] = set()
    for edge in graph.edges:
        edge_clash |= set(edge.attributes) & set(CORE_EDGE_ATTRS)
    if edge_clash:
        raise ValueError(f"边属性 {sorted(edge_clash)} 与 GraphML 核心属性同名，无法导出")

    node_names = list(CORE_NODE_ATTRS)
    for node in nodes:
        for name in node.attributes:
            if name not in node_names:
                node_names.append(name)
    edge_names = list(CORE_EDGE_ATTRS)
    for edge in graph.edges:
        for name in edge.attributes:
            if name not in edge_names:
                edge_names.append(name)

    node_types: dict[str, tuple[str, bool]] = {}
    for name in node_names:
        if name in CORE_NODE_ATTRS:
            node_types[name] = ("string", False)
        else:
            node_types[name] = _graphml_type([_node_values(node).get(name) for node in nodes])
    edge_types: dict[str, tuple[str, bool]] = {}
    for name in edge_names:
        if name in CORE_EDGE_ATTRS:
            edge_types[name] = ("string", False)
        else:
            edge_types[name] = _graphml_type([_edge_values(edge).get(name) for edge in graph.edges])

    stringified = tuple(
        sorted(
            [f"node.{name}" for name, (_, dump) in node_types.items() if dump]
            + [f"edge.{name}" for name, (_, dump) in edge_types.items() if dump]
        )
    )

    ElementTree.register_namespace("", GRAPHML_NS)
    tag = lambda name: f"{{{GRAPHML_NS}}}{name}"  # noqa: E731 - 局部命名空间辅助
    root = ElementTree.Element(tag("graphml"))
    key_ids: dict[tuple[str, str], str] = {}
    counter = 0
    for domain, names, types, items, values_of in (
        ("node", node_names, node_types, nodes, _node_values),
        ("edge", edge_names, edge_types, list(graph.edges), _edge_values),
    ):
        for name in names:
            declared, dump = types[name]
            if all(values_of(item).get(name) is None for item in items):
                continue  # 全是空值的属性不声明 key：别给读的人留一个永远没有数据的类型
            counter += 1
            key_id = f"key_{counter}"
            key_ids[(domain, name)] = key_id
            ElementTree.SubElement(
                root,
                tag("key"),
                {"id": key_id, "for": domain, "attr.name": name, "attr.type": declared},
            )
    graphml_graph = ElementTree.SubElement(root, tag("graph"), {"id": "G", "edgedefault": "directed"})

    graphml_ids: dict[str, str] = {}
    per_kind: dict[str, int] = {}
    for node in nodes:
        prefix = NODE_PREFIX[node.kind]
        index = per_kind.get(prefix, 0)
        per_kind[prefix] = index + 1
        node_key = f"{prefix}_{index}"
        graphml_ids[node.node_id] = node_key
        element = ElementTree.SubElement(graphml_graph, tag("node"), {"id": node_key})
        for name in node_names:
            _put_data(element, tag, domain="node", name=name, types=node_types, key_ids=key_ids,
                      value=_node_values(node).get(name))

    for index, edge in enumerate(graph.edges):
        edge_element = ElementTree.SubElement(
            graphml_graph,
            tag("edge"),
            {
                "id": f"e_{index}",
                "source": graphml_ids[edge.source],
                "target": graphml_ids[edge.target],
                "directed": "1",
            },
        )
        for name in edge_names:
            _put_data(edge_element, tag, domain="edge", name=name, types=edge_types, key_ids=key_ids,
                      value=_edge_values(edge).get(name))

    ElementTree.indent(root, space="  ")
    body = ElementTree.tostring(root, encoding="unicode")
    declared = {
        **{name: node_types[name][0] for name in node_names if ("node", name) in key_ids},
        **{name: edge_types[name][0] for name in edge_names if ("edge", name) in key_ids},
    }
    return GraphMLPlan(
        document='<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n",
        nodes=len(nodes),
        edges=len(graph.edges),
        declared_types=declared,
        stringified_attrs=stringified,
    )


def _put_data(element, tag, *, domain: str, name: str, types: dict, key_ids: dict, value) -> None:
    key = key_ids.get((domain, name))
    if key is None:
        return
    declared, dump = types[name]
    rendered = _graphml_value(declared, dump, value)
    if rendered is None:
        return
    data = ElementTree.SubElement(element, tag("data"), {"key": key})
    data.text = rendered


# ------------------------------------------------------------------ 取数与建行


def _settings_of(run: dict) -> Settings | None:
    """run 的 config_json 是当时的配置快照；不是合法 Settings 就返回 None，不猜额度。"""
    try:
        return Settings.model_validate(json.loads(str(run["config_json"])))
    except (ValueError, TypeError, ValidationError):
        return None


def _payload(need: dict) -> dict:
    raw = need.get("evidence_complete_json")
    return json.loads(raw) if raw else {}


def _score_json(need: dict) -> dict:
    raw = need.get("score_json")
    return json.loads(raw) if raw else {}


def _join(values: Iterable[object]) -> str:
    return ",".join(str(value) for value in values if value is not None)


@dataclass(frozen=True)
class ExportDataset:
    run_id: str
    analysis_version: str
    extractor_version: str
    keywords: tuple[dict, ...]
    needs: tuple[dict, ...]
    evidence: tuple[dict, ...]
    graph: Graph
    integrity: GraphIntegrity
    withheld_edges: int
    statistics: dict[str, object]
    anomalies: dict[str, object]
    exclusions: dict[str, object]
    limitations: tuple[str, ...]
    graphml: GraphMLPlan

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "analysis_version": self.analysis_version,
            "keywords": len(self.keywords),
            "needs": len(self.needs),
            "evidence": len(self.evidence),
            "graph_nodes": len(self.graph.nodes),
            "graph_edges": len(self.graph.edges),
            "integrity_ok": self.integrity.ok,
        }


class ExportBuilder:
    """从库里重建五类产物的内容；同一个连接重复调用必须给出逐字相同的结果。"""

    def __init__(
        self,
        repo: Repository,
        run_id: str,
        *,
        analysis_version: str = ANALYSIS_VERSION,
        extractor_version: str = EXTRACTOR_VERSION,
        generated_at: datetime | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("导出必须按 run 进行：跨 run 的证据与预算不是同一次运行")
        self.repo = repo
        self.conn = repo.conn
        self.run_id = run_id
        self.analysis_version = analysis_version
        self.extractor_version = extractor_version
        self.run = repo.require_run(run_id)
        self.generated_at = generated_at
        self.builder = DemandGraphBuilder(
            repo, run_id, analysis_version=analysis_version, extractor_version=extractor_version
        )
        self.settings = _settings_of(self.run)
        # 需求行既要进 needs.csv，又要回算 keywords.csv 的 displayable_needs；
        # 两处各跑一遍会把"同一个口径两次实现"的漂移风险留在导出里，所以只算一次并缓存。
        self._need_cache: tuple[tuple[dict, ...], dict[str, object]] | None = None
        self._keyword_needs: dict[int, int] = {}
        self._integrity: GraphIntegrity | None = None
        self._demand: Graph | None = None

    # ------------------------------------------------------------------ 需求与证据

    def demand(self) -> Graph:
        if self._demand is None:
            self._demand = self.builder.build()
        return self._demand

    def check(self) -> GraphIntegrity:
        if self._integrity is None:
            self._integrity = self.builder.check(self.demand())
        return self._integrity

    def _broken_references(self) -> tuple[set[int], set[int]]:
        """自检里"这条需求不能再上排行榜"的两种落点。

        违规可能挂在需求节点上（没有引用、引用了别的批次的证据行），也可能挂在它引用的证据
        节点上（原句在版本行里定位不到、把联想当成帖子事实）。只认前一种的话，失效引用那条
        需求照样会带着分数出现在排行榜上，而它的原句已经点不开。
        """
        needs: set[int] = set()
        evidence: set[int] = set()
        for item in self.check().violations:
            if item.code not in DISPLAY_BLOCKING_CODES:
                continue
            for name in ("need_id", "need_node"):
                value = item.detail.get(name)
                if value:
                    needs.add(int(str(value).rpartition(":")[2]))
            kind, _, identity = str(item.node_id).partition(":")
            if kind == "need":
                needs.add(int(identity))
            elif kind == "evidence":
                evidence.add(int(identity))
        return needs, evidence

    def need_rows(self) -> tuple[tuple[dict, ...], dict[str, object]]:
        """展示需求行 + 排除口径统计：每一行为什么进得来、排出去的为什么进不来。"""
        if self._need_cache is not None:
            return self._need_cache
        needs = self.builder.needs()
        evidence = self.builder.evidence_rows()
        problems = self.builder.problems()
        assignments = self.builder.assignments()
        untrusted_needs, untrusted_evidence = self._broken_references()
        rows: list[dict] = []
        excluded: dict[str, object] = {
            "not_displayed_status": 0, "no_score": 0, "broken_evidence": 0, "broken_ids": []
        }
        self._keyword_needs = {}
        for need in needs:
            payload = _payload(need)
            score = _score_json(need)
            displayed = str(need["evidence_status"]) in DISPLAYED_STATUSES
            has_score = need["score"] is not None
            ids = [int(value) for value in payload.get("evidence_ids", [])]
            has_evidence = bool(ids)
            if not displayed:
                excluded["not_displayed_status"] += 1
                continue
            if not has_score:
                excluded["no_score"] += 1
                continue
            if (
                not has_evidence
                or int(need["id"]) in untrusted_needs
                or any(value in untrusted_evidence for value in ids)
            ):
                excluded["broken_evidence"] += 1
                excluded["broken_ids"].append(str(need["id"]))
                continue
            rows.append(self._need_row(need, payload, score, evidence, problems, assignments))
            # 一个需求在同一词下有三条证据也只算一次：displayable_needs 数的是需求，不是证据。
            for key in {
                int(evidence[int(value)]["keyword_id"])
                for value in payload.get("evidence_ids", [])
                if int(value) in evidence
            }:
                self._keyword_needs[key] = self._keyword_needs.get(key, 0) + 1
        rows.sort(key=lambda item: (-float(item["score"] or 0.0), int(item["need_id"])))
        self._need_cache = (tuple(rows), excluded)
        return self._need_cache

    def _need_row(
        self, need: dict, payload: dict, score: dict, evidence: dict, problems: dict, assignments: dict
    ) -> dict:
        decomposition = score.get("score", {})
        features = decomposition.get("features", {})
        signals = score.get("signals", {})
        linked = [evidence[int(value)] for value in payload.get("evidence_ids", []) if int(value) in evidence]
        problem_rows = [
            problems[int(value)] for value in payload.get("problem_ids", []) if int(value) in problems
        ]
        row: dict[str, object] = {
            "need_id": need["id"],
            "stable_key": need["stable_key"],
            "canonical_text": need["canonical_text"],
            "statement_kind": need["statement_kind"],
            "category": need["category"],
            "evidence_status": need["evidence_status"],
            "displayable": 1,
            "score": need["score"],
            "scoring_method": score.get("method", SCORING_METHOD),
            "score_model_version": decomposition.get("model_version", SCORING_MODEL_VERSION),
            "effective_weight_total": decomposition.get("effective_weight_total"),
            "provisional": decomposition.get("provisional"),
            "provisional_reason": decomposition.get("provisional_reason"),
            "unknown_components": _join(score.get("unknowns", [])),
            "sessions": signals.get("sessions"),
            "parent_paths": signals.get("parent_paths"),
            "evidence_row_sessions": signals.get("evidence_row_sessions"),
            "evidence_row_parent_paths": signals.get("evidence_row_parent_paths"),
            "best_rank": signals.get("best_rank"),
            "consensus_signal": signals.get("consensus_signal"),
            "position_signal": signals.get("position_signal"),
            "evidence_rows": signals.get("evidence_rows"),
            "distinct_posts": signals.get("distinct_posts"),
            "content_families": signals.get("content_families"),
            "q_counted_posts": signals.get("q_counted_posts"),
            "q_family_suppressed_posts": signals.get("q_family_suppressed_posts"),
            "q_post_absorbed_families": signals.get("q_post_absorbed_families"),
            "q_suppression_reasons": signals.get("q_suppression_reasons"),
            "duplicate_versions_suppressed": signals.get("duplicate_versions_suppressed"),
            "distinct_known_authors": signals.get("distinct_known_authors"),
            "posts_without_author_ref": signals.get("posts_without_author_ref"),
            "published_dates_parsed": signals.get("published_dates_parsed"),
            "published_dates_unparseable": signals.get("published_dates_unparseable"),
            "published_dates_without_anchor": signals.get("published_dates_without_anchor"),
            "latest_published_at": signals.get("latest_published_at"),
            "window_reference_at": signals.get("window_reference_at"),
            "window_reference_source": signals.get("window_reference_source"),
            "topics": _join(sorted({str(item["topic"]) for item in problem_rows if item["topic"]})),
            "problems": len(problem_rows),
            "problem_ids": _join(sorted(int(item["id"]) for item in problem_rows)),
            "evidence_ids": _join(sorted(int(value) for value in payload.get("evidence_ids", []))),
            "note_ids": _join(sorted({str(item["note_id"]) for item in linked if item["note_id"]})),
            "source_keywords": _join(
                sorted({self.builder.keyword_label(int(item["keyword_id"])) for item in linked})
            ),
            "source_fields": _join(sorted({str(item["source_field"]) for item in linked})),
            "generated_expression": 1 if payload.get("generated_expression") else 0,
            "derived_from_stable_key": payload.get("derived_from_stable_key"),
            "created_at": need["created_at"],
            "updated_at": need["updated_at"],
            "analysis_version": need["analysis_version"],
            "extractor_version": need["extractor_version"],
            "score_json": json.dumps(score, ensure_ascii=False, sort_keys=True) if score else None,
        }
        for name in ("S", "Q", "A", "F"):
            feature = features.get(name, {})
            row[f"{name}_raw"] = feature.get("raw")
            row[f"{name}_normalized"] = feature.get("normalized")
            row[f"{name}_weight"] = feature.get("baseline_weight")
            row[f"{name}_missing_reason"] = feature.get("missing_reason")
        # 归属方式也要上看板：自动合并与人工/规则改判在需求详情里必须能区分。
        row["merge_methods"] = _join(sorted({str(assignments[int(item["id"])]["method"])
                                            for item in problem_rows if int(item["id"]) in assignments}))
        return row

    def evidence_rows(self) -> tuple[dict, ...]:
        """本 run 本分析版本的每条证据都导出，包括没被任何需求采纳的营销与分享句。

        `locatable` 在这里独立重算一次：抽取时算过、入库前查过，导出再查一遍，
        三个口径同源但互为反证——任何一层被改动都会在这里露出来。
        """
        evidence = self.builder.evidence_rows()
        needs = self.builder.needs()
        displayed = {
            int(need["id"])
            for need in needs
            if str(need["evidence_status"]) in DISPLAYED_STATUSES and need["score"] is not None
        }
        on_need: dict[int, list[dict]] = {}
        for need in needs:
            for value in _payload(need).get("evidence_ids", []):
                on_need.setdefault(int(value), []).append(need)
        versions = self._content_versions(evidence)
        suggestions = self._suggestion_versions(evidence)
        cards = self._card_facts()
        obs_cards = self._observation_card_facts()
        operations = self._operation_types()
        problems = self._problem_by_source()
        rows: list[dict] = []
        for identifier in sorted(evidence):
            item = evidence[identifier]
            quote = str(item["quote"])
            start, end = int(item["char_start"]), int(item["char_end"])
            from_suggestion = str(item["source_field"]) == SourceField.suggestion.value
            if from_suggestion:
                container = suggestions.get(int(item["suggestion_version_id"] or 0), {})
                container_text = container.get("text")
                locatable = container_text is not None and quote in str(container_text)
                container_field = SourceField.suggestion.value
                container_kind = "suggestion_version"
                text_sha = container.get("text_sha256")
                captured = container.get("captured_at")
                is_missing = None
            else:
                version = versions.get(
                    int(item["content_version_id"] or 0), {}
                )
                container_text = version.get("text")
                locatable = (
                    container_text is not None and str(container_text)[start:end] == quote
                )
                container_field = version.get("field")
                container_kind = "post_content_version"
                text_sha = version.get("text_sha256")
                captured = version.get("captured_at")
                is_missing = version.get("is_missing")
            card = cards.get((int(item["keyword_id"]), item["post_id"]), {})
            if item["post_observation_id"] is not None and item["post_id"] is not None:
                # C-12：卡片可见字段优先取**证据回链的那次观察**——和评分解析用的是同一条
                # post_queries 行；只有旧库没锚点时才退回本 (词, 帖) 的首次记录。
                linked_card = obs_cards.get((int(item["post_observation_id"]), int(item["post_id"])))
                if linked_card is not None:
                    card = linked_card
            linked = on_need.get(identifier, [])
            problem = problems.get(
                (
                    str(item["source_field"]),
                    item["content_version_id"],
                    item["suggestion_version_id"],
                    core_text(quote),
                    str(item["extractor_version"]),
                )
            )
            rows.append(
                {
                    "evidence_id": identifier,
                    "quote": quote,
                    "source_field": item["source_field"],
                    "container_field": container_field,
                    "char_start": start,
                    "char_end": end,
                    "locatable": 1 if locatable else 0,
                    "container_kind": container_kind,
                    "content_version_id": item["content_version_id"],
                    "suggestion_version_id": item["suggestion_version_id"],
                    "container_sha256": item["container_sha256"],
                    "container_text_sha256": text_sha,
                    "container_captured_at": captured,
                    "container_is_missing": is_missing,
                    "post_id": item["post_id"],
                    "note_id": item["note_id"],
                    "access_url": item["access_url"],
                    "keyword_id": item["keyword_id"],
                    "keyword_normalized": self.builder.keyword_label(int(item["keyword_id"])),
                    "session_id": item["session_id"],
                    "operation_id": item["operation_id"],
                    "operation_type": operations.get(int(item["operation_id"]))
                    if item["operation_id"] is not None
                    else None,
                    "attempt_no": item["attempt_no"],
                    "result_rank": card.get("result_rank"),
                    "published_at_text": card.get("published_at_text"),
                    "post_observation_id": item["post_observation_id"],
                    "published_at_observed_at": item["observation_observed_at"],
                    "suggestion_rank": container.get("rank") if from_suggestion else None,
                    "problem_id": None if problem is None else int(problem["id"]),
                    "counts_as_valid_problem": None
                    if problem is None
                    else int(problem["counts_as_valid_problem_cluster"]),
                    "need_ids": _join(sorted(int(need["id"]) for need in linked)),
                    "need_stable_keys": _join(
                        sorted(str(need["stable_key"]) for need in linked if need["stable_key"])
                    ),
                    "on_displayed_need": 1 if any(int(need["id"]) in displayed for need in linked) else 0,
                    "extractor_version": item["extractor_version"],
                    "analysis_version": item["analysis_version"],
                    "synthetic": item["synthetic"],
                    "created_at": item["created_at"],
                }
            )
        return tuple(rows)

    def _content_versions(self, evidence: dict) -> dict[int, dict]:
        ids = sorted(
            {int(item["content_version_id"]) for item in evidence.values() if item["content_version_id"]}
        )
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        return {
            int(row["id"]): dict(row)
            for row in self.conn.execute(
                f"SELECT id, field, text, text_sha256, captured_at, is_missing FROM post_content_versions"
                f" WHERE id IN ({marks})",
                tuple(ids),
            ).fetchall()
        }

    def _suggestion_versions(self, evidence: dict) -> dict[int, dict]:
        ids = sorted(
            {
                int(item["suggestion_version_id"])
                for item in evidence.values()
                if item["suggestion_version_id"]
            }
        )
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        return {
            int(row["id"]): dict(row)
            for row in self.conn.execute(
                f"SELECT id, rank, text, text_sha256, captured_at FROM suggestion_versions"
                f" WHERE id IN ({marks})",
                tuple(ids),
            ).fetchall()
        }

    def _card_facts(self) -> dict[tuple[int, object], dict]:
        """每个 (词, 帖) 取首次出现那次的卡片可见字段：位次与页面显示的发布时间都是当时的观察。

        只服务 C-12 之前没有观察锚点的旧证据行；新行一律走 `_observation_card_facts`。
        """
        return {
            (int(row["keyword_id"]), row["post_id"]): dict(row)
            for row in self.conn.execute(
                "SELECT keyword_id, post_id, result_rank, published_at_text FROM post_queries"
                " WHERE run_id = ? AND id IN"
                " (SELECT MIN(id) FROM post_queries WHERE run_id = ? GROUP BY keyword_id, post_id)",
                (self.run_id, self.run_id),
            ).fetchall()
        }

    def _observation_card_facts(self) -> dict[tuple[int, int], dict]:
        """按 (卡片观察, 帖) 回链的卡片可见字段：与评分解析同一个来源，一处改两处都跟着走。"""
        return {
            (int(row["post_observation_id"]), int(row["post_id"])): dict(row)
            for row in self.conn.execute(
                "SELECT pq.post_observation_id, pq.post_id, pq.result_rank,"
                " pq.published_at_text, po.observed_at FROM post_queries pq"
                " JOIN post_observations po ON po.id = pq.post_observation_id"
                " WHERE pq.run_id = ? AND po.run_id = ?",
                (self.run_id, self.run_id),
            ).fetchall()
        }

    def _operation_types(self) -> dict[int, str]:
        return {
            int(row["id"]): str(row["operation_type"])
            for row in self.conn.execute(
                "SELECT id, operation_type FROM operations WHERE run_id = ?", (self.run_id,)
            ).fetchall()
        }

    def _problem_by_source(self) -> dict[tuple, dict]:
        return {
            (
                str(row["source_field"]),
                row["content_version_id"],
                row["suggestion_version_id"],
                str(row["canonical_text"]),
                str(row["extractor_version"]),
            ): dict(row)
            for row in self.conn.execute(
                "SELECT * FROM problems WHERE run_id = ? AND extractor_version = ?",
                (self.run_id, self.extractor_version),
            ).fetchall()
        }

    # ------------------------------------------------------------------ 关键词

    def keyword_rows(self) -> tuple[dict, ...]:
        from xhs_discovery.discovery.graph import DiscoveryGraph

        self.need_rows()  # 填 self._keyword_needs：needs.csv 与 keywords.csv 必须同一个展示口径
        view = DiscoveryGraph(self.repo, self.run_id)
        conn = self.conn
        evidence = self.builder.evidence_rows()
        posts = self._grouped(
            "SELECT keyword_id, COUNT(*) AS seen, COUNT(DISTINCT post_id) AS distinct_posts"
            " FROM post_queries WHERE run_id = ? GROUP BY keyword_id"
        )
        suggestions = self._grouped(
            "SELECT so.keyword_id AS keyword_id, COUNT(DISTINCT so.id) AS observations,"
            " COUNT(si.id) AS returned, MIN(si.rank) AS best_rank,"
            " COUNT(DISTINCT so.session_id) AS sessions"
            " FROM suggestion_observations so"
            " LEFT JOIN suggestion_items si ON si.observation_id = so.id"
            " WHERE so.run_id = ? GROUP BY so.keyword_id"
        )
        by_field: dict[tuple[int, str], int] = {}
        problems_seen: dict[int, int] = {}
        for item in evidence.values():
            key = (int(item["keyword_id"]), str(item["source_field"]))
            by_field[key] = by_field.get(key, 0) + 1
            problems_seen[int(item["keyword_id"])] = problems_seen.get(int(item["keyword_id"]), 0) + 1
        seed_normalized = str(self.run["seed_normalized"])
        rows: list[dict] = []
        for node in view.nodes():
            keyword_id = int(node.keyword_id)
            parents = view.parents(keyword_id)
            stats = view.observation_stats(keyword_id)
            card = suggestions.get(keyword_id, {})
            post_row = posts.get(keyword_id, {})
            rows.append(
                {
                    "keyword_id": keyword_id,
                    "normalized": node.normalized,
                    "aliases": _join(self.repo.keyword_aliases(keyword_id)),
                    "origin": node.origin,
                    "depth": node.depth,
                    "status": node.status,
                    "branch_key": node.branch_key,
                    "is_seed": 1 if node.normalized == seed_normalized else 0,
                    "parents": len(parents),
                    "parent_keyword_id": node.parent_keyword_id,
                    "parent_normalized": None
                    if node.parent_keyword_id is None
                    else self.builder.keyword_label(int(node.parent_keyword_id)),
                    "independent_parents": view.independent_parent_count(keyword_id),
                    "suggestion_observations": card.get("observations"),
                    "suggestions_returned": card.get("returned"),
                    "best_suggestion_rank": card.get("best_rank"),
                    "distinct_sessions": stats["distinct_sessions"],
                    "revisit_count": node.revisit_count,
                    "posts_observed": post_row.get("seen"),
                    "distinct_posts": post_row.get("distinct_posts"),
                    "title_evidence": by_field.get((keyword_id, SourceField.title.value), 0),
                    "body_evidence": by_field.get((keyword_id, SourceField.body.value), 0),
                    "suggestion_evidence": by_field.get((keyword_id, SourceField.suggestion.value), 0),
                    "problems_extracted": problems_seen.get(keyword_id, 0),
                    "displayable_needs": self._keyword_needs.get(keyword_id, 0),
                    "first_seen_at": node.first_seen_at,
                    "last_seen_at": node.last_seen_at,
                    "analysis_version": self.analysis_version,
                    "extractor_version": self.extractor_version,
                }
            )
        return tuple(rows)

    def _grouped(self, sql: str) -> dict[int, dict]:
        return {
            int(row["keyword_id"]): dict(row)
            for row in self.conn.execute(sql, (self.run_id,)).fetchall()
        }

    # ------------------------------------------------------------------ 统计与异常

    def statistics(self) -> dict[str, object]:
        conn = self.conn
        run_id = self.run_id
        version = self.analysis_version

        def count(sql: str, params: tuple = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])

        def grouped(sql: str, key: str, params: tuple | None = None) -> dict[str, int]:
            return {
                str(row[key]): int(row["seen"])
                for row in conn.execute(sql, params if params is not None else (run_id,)).fetchall()
            }

        needs_total = count(
            "SELECT COUNT(*) AS n FROM needs WHERE run_id = ? AND analysis_version = ?",
            (run_id, version),
        )
        scored = count(
            "SELECT COUNT(*) AS n FROM needs WHERE run_id = ? AND analysis_version = ?"
            " AND score IS NOT NULL",
            (run_id, version),
        )
        # 导出自己写入的事件不计入统计：否则"报告统计与库同步"这条反读检查永远失败，
        # 因为报告落盘后紧接着就多了 analysis_export 一条。与 report 不登记自身哈希同理。
        marks = ",".join("?" for _ in EXPORT_SELF_EVENTS)
        events = grouped(
            f"SELECT kind, COUNT(*) AS seen FROM run_events WHERE run_id = ?"
            f" AND kind NOT IN ({marks}) GROUP BY kind",
            "kind",
            (run_id, *EXPORT_SELF_EVENTS),
        )
        return {
            "keywords": {
                "total": count("SELECT COUNT(*) AS n FROM run_keywords WHERE run_id = ?", (run_id,)),
                "by_status": grouped(
                    "SELECT status, COUNT(*) AS seen FROM run_keywords WHERE run_id = ? GROUP BY status",
                    "status",
                ),
                "by_origin": grouped(
                    "SELECT origin, COUNT(*) AS seen FROM run_keywords WHERE run_id = ? GROUP BY origin",
                    "origin",
                ),
                "max_depth": conn.execute(
                    "SELECT MAX(depth) AS d FROM run_keywords WHERE run_id = ?", (run_id,)
                ).fetchone()["d"],
            },
            "discovery_edges": grouped(
                "SELECT origin, COUNT(*) AS seen FROM discovery_edges WHERE run_id = ? GROUP BY origin",
                "origin",
            ),
            "branches": grouped(
                "SELECT branch_key, trials AS seen FROM branch_stats WHERE run_id = ?", "branch_key"
            ),
            "posts": {
                "total": count("SELECT COUNT(*) AS n FROM post_queries WHERE run_id = ?", (run_id,)),
                "distinct": count("SELECT COUNT(DISTINCT post_id) AS n FROM post_queries WHERE run_id = ?", (run_id,)),
                "content_versions": count(
                    "SELECT COUNT(*) AS n FROM post_content_versions v"
                    " WHERE EXISTS (SELECT 1 FROM post_queries pq WHERE pq.post_id = v.post_id"
                    " AND pq.run_id = ?)",
                    (run_id,),
                ),
            },
            "observations": {
                "suggestion": count("SELECT COUNT(*) AS n FROM suggestion_observations WHERE run_id = ?", (run_id,)),
                "search": count("SELECT COUNT(*) AS n FROM post_observations WHERE run_id = ?", (run_id,)),
            },
            "operations": grouped(
                "SELECT status, COUNT(*) AS seen FROM operations WHERE run_id = ? GROUP BY status", "status"
            ),
            "analysis": {
                "phrases": grouped(
                    "SELECT kind, COUNT(*) AS seen FROM phrases WHERE run_id = ?"
                    " AND analysis_version = ? GROUP BY kind",
                    "kind",
                    (run_id, version),
                ),
                "problems": count(
                    "SELECT COUNT(*) AS n FROM problems WHERE run_id = ? AND extractor_version = ?",
                    (run_id, self.extractor_version),
                ),
                "evidence": count(
                    "SELECT COUNT(*) AS n FROM evidence WHERE run_id = ? AND analysis_version = ?",
                    (run_id, version),
                ),
                "needs_total": needs_total,
                "needs_scored": scored,
                "needs_without_score": needs_total - scored,
                "needs_by_statement_kind": {
                    str(row["statement_kind"]): int(row["seen"])
                    for row in conn.execute(
                        "SELECT statement_kind, COUNT(*) AS seen FROM needs"
                        " WHERE run_id = ? AND analysis_version = ? GROUP BY statement_kind",
                        (run_id, version),
                    ).fetchall()
                },
                "cluster_assignments_active": count(
                    "SELECT COUNT(*) AS n FROM cluster_assignments WHERE run_id = ?"
                    " AND analysis_version = ? AND superseded_at IS NULL",
                    (run_id, version),
                ),
                "cluster_assignments_superseded": count(
                    "SELECT COUNT(*) AS n FROM cluster_assignments WHERE run_id = ?"
                    " AND analysis_version = ? AND superseded_at IS NOT NULL",
                    (run_id, version),
                ),
            },
            "events": events,
        }

    def budget(self) -> dict[str, object]:
        conn = self.conn
        rows = conn.execute(
            "SELECT budget_key, external_access, COUNT(*) AS seen,"
            " SUM(consumed_units) AS units FROM budget_ledger WHERE run_id = ?"
            " GROUP BY budget_key, external_access",
            (self.run_id,),
        ).fetchall()
        per_key: dict[str, dict[str, object]] = {}
        for row in rows:
            entry = per_key.setdefault(
                str(row["budget_key"]),
                {"limit": None, "used": 0.0, "reserved": 0.0, "rows": 0, "external_rows": 0},
            )
            entry["rows"] += int(row["seen"])
            if int(row["external_access"]) == 1:
                entry["external_rows"] += int(row["seen"])
        used = {
            str(row["budget_key"]): float(row["units"] or 0.0)
            for row in conn.execute(
                "SELECT budget_key, SUM(consumed_units) AS units FROM budget_ledger"
                " WHERE run_id = ? GROUP BY budget_key",
                (self.run_id,),
            ).fetchall()
        }
        for key, value in used.items():
            per_key.setdefault(key, {"limit": None, "used": 0.0, "reserved": 0.0, "rows": 0, "external_rows": 0})
            per_key[key]["used"] = value
        if self.settings is not None:
            ledger = Ledger.from_settings(conn, self.run_id, self.settings)
            for key, view in ledger.status().items():
                entry = per_key.setdefault(
                    key, {"limit": None, "used": 0.0, "reserved": 0.0, "rows": 0, "external_rows": 0}
                )
                entry.update(
                    {
                        "limit": view.limit,
                        "used": view.used,
                        "reserved": view.reserved,
                        "remaining": view.remaining,
                        "exhausted": view.exhausted,
                    }
                )
        return {
            "limits_from_config": self.settings is not None,
            "keys": {key: per_key[key] for key in sorted(per_key)},
            "revisions": [
                {
                    "added": json.loads(str(row["added_json"])),
                    "created_at": row["created_at"],
                    "note": row["note"],
                }
                for row in conn.execute(
                    "SELECT * FROM budget_revisions WHERE run_id = ? ORDER BY id", (self.run_id,)
                ).fetchall()
            ],
            "budget_revision": int(self.run["budget_revision"]),
        }

    def anomalies(self) -> dict[str, object]:
        conn = self.conn
        marks = ",".join("?" for _ in EXPORT_SELF_EVENTS)
        notable = []
        for row in conn.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (self.run_id,)
        ).fetchall():
            kind = str(row["kind"])
            if kind in NOTABLE_EVENTS or kind.startswith(NOTABLE_EVENT_PREFIXES):
                notable.append(
                    {
                        "at": row["at"],
                        "kind": kind,
                        "detail": json.loads(row["detail_json"]) if row["detail_json"] else {},
                    }
                )
        uncertain = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM operations WHERE run_id = ? AND status = ? ORDER BY id",
                (self.run_id, OperationState.uncertain.value),
            ).fetchall()
        ]
        in_flight = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM operations WHERE run_id = ? AND status = ? ORDER BY id",
                (self.run_id, OperationState.in_flight.value),
            ).fetchall()
        ]
        unknown_components: dict[str, int] = {}
        for row in conn.execute(
            "SELECT score_json FROM needs WHERE run_id = ? AND analysis_version = ?"
            " AND score_json IS NOT NULL",
            (self.run_id, self.analysis_version),
        ).fetchall():
            for name in json.loads(str(row["score_json"])).get("unknowns", []):
                unknown_components[str(name)] = unknown_components.get(str(name), 0) + 1
        return {
            "run_status": self.run["status"],
            "run_outcome": self.run["outcome"],
            "pause_reason": self.run["pause_reason"],
            "uncertain_operations": uncertain,
            "in_flight_operations": in_flight,
            "graph_violations": [item.as_dict() for item in self.check().violations],
            "violation_codes": self.integrity_codes(),
            "needs_without_score": [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM needs WHERE run_id = ? AND analysis_version = ?"
                    " AND score IS NULL ORDER BY id",
                    (self.run_id, self.analysis_version),
                ).fetchall()
            ],
            "unknown_components": unknown_components,
            "notable_events": notable,
            "event_kinds": len(
                conn.execute(
                    f"SELECT DISTINCT kind FROM run_events WHERE run_id = ?"
                    f" AND kind NOT IN ({marks}) GROUP BY kind",
                    (self.run_id, *EXPORT_SELF_EVENTS),
                ).fetchall()
            ),
        }

    def integrity_codes(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.check().violations:
            counts[item.code] = counts.get(item.code, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def limitations(self) -> tuple[str, ...]:
        facts: list[str] = []
        if int(self.run["synthetic_fixture"]) == 1 or str(self.run["mode"]) == "fixture":
            facts.append(
                "全部数据来自合成 fixture：note_id、access_url 与作者引用都是合成值，不代表真实平台上的任何内容"
            )
        if str(self.run["mode"]) == "live":
            facts.append(
                "数据来源是真实 xhs-search CLI 的搜索卡片（title_only）：note_id 与 access_url 是平台返回值，"
                "但只有标题——正文未采集（能力不支持，不是「没有」），发布日期与作者引用经常缺失，"
                "评分只覆盖本次采样到的标题样本，不代表全量需求强度"
            )
        facts.append(
            "本轮不接模型：分词、意图、归并全是规则基线，同义改写不会被合并，"
            "需求数量因此偏保守而不是偏全"
        )
        facts.append(
            "页面点赞数留在 post_queries 供 P1 校准，不进任何导出列：点赞不是搜索量，"
            "出现在需求表里一定会被读成需求"
        )
        facts.append(
            "A 与 F 缺证据时按 unknown 处理并重归一化，不当 0 分；分数为 NULL 的需求行不进 needs.csv"
        )
        facts.append(
            "导出只含本 run 与本 analysis_version 的行；跨 run 复现要分别导出再各自对照，"
            "内容家族是跨 run 的内容事实，同一篇文章在两个 run 里会命中同一族"
        )
        facts.append(
            "CSV 空单元格表示库里没有值；以 = + - @ 制表符 或 回车 开头的文本已前置撇号，"
            "程序读取时请按文本使用，不要指望它还能当公式执行"
        )
        if self.settings is None:
            facts.append("runs.config_json 不是完整配置快照，report.json 的预算上限只能留空，不猜额度")
        if not any(str(row["source_field"]) == SourceField.body.value for row in self.builder.evidence_rows().values()):
            facts.append("本批证据只有标题与联想，没有正文：标题不足以支撑的判断已在评分层降为 unknown")
        if self._provisional():
            facts.append(
                "本批样本量低于分位数稳定门槛，report.json 与 needs.csv 只给原始计数与逐分量分解，"
                "不给跨批可比的绝对分位"
            )
        return tuple(facts)

    def _provisional(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS seen, MAX(json_extract(score_json, '$.score.provisional')) AS flag"
            " FROM needs WHERE run_id = ? AND analysis_version = ? AND score_json IS NOT NULL",
            (self.run_id, self.analysis_version),
        ).fetchone()
        return int(row["seen"] or 0) > 0 and bool(row["flag"])

    # ------------------------------------------------------------------ 汇总

    def scoring_facts(self) -> dict[str, object]:
        """评分口径从库里的 score_json 取，不重新实例化一套权重：导出必须复述当时那批的口径。"""
        for need in self.builder.needs():
            score = _score_json(need)
            if score:
                return {
                    "method": score.get("method", SCORING_METHOD),
                    "model_version": score.get("score", {}).get("model_version", SCORING_MODEL_VERSION),
                    "weights": score.get("weights"),
                    "assumptions": score.get("assumptions"),
                    "source_need_id": int(need["id"]),
                }
        return {
            "method": SCORING_METHOD,
            "model_version": SCORING_MODEL_VERSION,
            "weights": None,
            "assumptions": None,
            "source_need_id": None,
        }

    def build(self) -> ExportDataset:
        discovery, withheld = build_discovery_graph(
            self.repo, self.run_id, analysis_version=self.analysis_version
        )
        demand = self.demand()
        merged = merge_graphs(discovery, demand)
        graphml = render_graphml(merged)
        needs, excluded = self.need_rows()
        all_needs = self.builder.needs()
        return ExportDataset(
            run_id=self.run_id,
            analysis_version=self.analysis_version,
            extractor_version=self.extractor_version,
            keywords=self.keyword_rows(),
            needs=needs,
            evidence=self.evidence_rows(),
            graph=merged,
            integrity=self.check(),
            withheld_edges=withheld,
            statistics=self.statistics(),
            anomalies=self.anomalies(),
            exclusions={
                **excluded,
                "needs_total": len(all_needs),
                "scored": sum(1 for need in all_needs if need["score"] is not None),
                "displayed": len(needs),
            },
            limitations=self.limitations(),
            graphml=graphml,
        )


# ------------------------------------------------------------------ 报告


def report_document(
    builder: ExportBuilder,
    dataset: ExportDataset,
    landed: list[LandedFile],
    *,
    export_dir: str,
    snapshot: str | None,
    generated_at: datetime,
) -> dict[str, object]:
    """report.json：运行配置、随机种子、代码与处理版本、预算账本、异常、统计与局限。"""
    run = builder.run
    conn = builder.conn
    summary = dataset.graph.summary()
    # 本次这五个路径都算"当前导出"，不进 history：报告里出现自己的登记行会让同一批事实
    # 第二次导出时内容变了，等于把幂等重跑变成伪冲突。
    current_paths = {f"{export_dir}/{name}" for name in EXPORT_FILENAMES}
    return {
        "meta": {
            "generated_at": generated_at.isoformat(),
            "run_id": dataset.run_id,
            "code_version": CODE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "analysis_version": dataset.analysis_version,
            "extractor_version": dataset.extractor_version,
            "schema_version": migrations.current_version(conn),
            "head_schema_version": migrations.HEAD_VERSION,
            "export_tool": "xhs_discovery.analysis.export",
            "merge_method": MERGE_METHOD,
            "family_method": FAMILY_METHOD,
        },
        "run": {
            "id": run["id"],
            "seed_raw": run["seed_raw"],
            "seed_normalized": run["seed_normalized"],
            "mode": run["mode"],
            "status": run["status"],
            "outcome": run["outcome"],
            "pause_reason": run["pause_reason"],
            "synthetic_fixture": bool(int(run["synthetic_fixture"])),
            "random_seed": int(run["random_seed"]),
            "config_hash": run["config_hash"],
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "updated_at": run["updated_at"],
        },
        "config": {
            "valid_settings_snapshot": builder.settings is not None,
            "snapshot": json.loads(str(run["config_json"])),
            "budget_keys": sorted(builder.budget()["keys"]),
        },
        "budget": builder.budget(),
        "statistics": dataset.statistics,
        "analysis": {
            "scoring": builder.scoring_facts(),
            "display_policy": {
                "statuses_allowed": list(DISPLAYED_STATUSES),
                "requires_non_null_score": True,
                "requires_locatable_evidence": True,
                "suppressed_by_integrity": dataset.exclusions.get("broken_ids", []),
            },
            "exclusions": dataset.exclusions,
            "graph_integrity": dataset.integrity.as_dict(),
        },
        "graph": {
            "merged_summary": summary,
            "withheld_generated_edges": dataset.withheld_edges,
            "node_kinds": {
                kind: sum(1 for node in dataset.graph.nodes if node.kind == kind)
                for kind in sorted({node.kind for node in dataset.graph.nodes})
            },
            "relations": {
                relation: sum(1 for edge in dataset.graph.edges if edge.relation == relation)
                for relation in sorted({edge.relation for edge in dataset.graph.edges})
            },
            "graphml": {
                "declared_types": dataset.graphml.declared_types,
                "stringified_attrs": list(dataset.graphml.stringified_attrs),
                "node_id_scheme": {
                    "format": "节点 id 是 <类型前缀>_<序号>，边 id 是 e_<序号>",
                    "identity_attribute": "ref",
                    "prefixes": {kind: NODE_PREFIX[kind] for kind in sorted(NODE_PREFIX)},
                },
            },
        },
        "exported": {
            "directory": export_dir,
            "snapshot": snapshot,
            "files": [item.as_dict() for item in landed],
            "history": [
                {
                    "kind": str(row["kind"]),
                    "rel_path": str(row["rel_path"]),
                    "sha256": str(row["sha256"]),
                    "created_at": str(row["created_at"]),
                }
                for row in conn.execute(
                    "SELECT * FROM artifacts WHERE run_id = ? AND analysis_version = ?"
                    " ORDER BY id",
                    (dataset.run_id, dataset.analysis_version),
                ).fetchall()
                if str(row["rel_path"]) not in current_paths
            ],
        },
        "anomalies": dataset.anomalies,
        "limitations": list(dataset.limitations),
        "verification": {
            "self_reference_excluded": True,
            "note": "report.json 不登记自身哈希；五类产物的反读校验由 verify_export 在写盘之后独立复核",
        },
    }


# ------------------------------------------------------------------ 落盘


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
    if not cleaned or cleaned.startswith("."):
        raise ValueError(f"非法导出目录名：{value!r}")
    return cleaned


def export_directory(
    paths: RuntimePaths, run_id: str, analysis_version: str, snapshot: str | None = None
) -> Path:
    directory = paths.run_dir(run_id) / EXPORT_DIRNAME / _safe_segment(analysis_version)
    if snapshot:
        directory = directory / _safe_segment(snapshot)
    return directory


@dataclass(frozen=True)
class LandedFile:
    filename: str
    artifact_kind: str
    rel_path: str
    sha256: str
    size_bytes: int
    written: bool
    measure: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """报告里只放"这份内容是什么"，不放 `written`：后者是本次调用有没有重写文件，
        同一批事实第二次导出时为 False，写进报告会让内容寻址的 report.json 自己不稳定。"""
        return {
            "filename": self.filename,
            "artifact_kind": self.artifact_kind,
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            **self.measure,
        }


def _register_export(
    repo: Repository, run_id: str, kind: str, rel_path: str, digest: str, *, analysis_version: str, stamp: str
) -> None:
    """登记当前导出：`artifacts` 是 UI/CLI 取用五类产物的唯一入口，一个 (run, kind, 路径) 一行。"""
    with repo.write():
        row = repo.conn.execute(
            "SELECT sha256 FROM artifacts WHERE run_id = ? AND kind = ? AND rel_path = ?",
            (run_id, kind, rel_path),
        ).fetchone()
        if row is not None:
            if str(row["sha256"]) != digest:
                # 走到这里说明有人在库里改过登记；文件层已被 write_artifact 挡住，这里补纵深。
                raise ExportConflict(
                    f"{kind} 在 artifacts 里登记为 {str(row['sha256'])[:12]}…，本次算出 {digest[:12]}…"
                )
            return
        repo.conn.execute(
            "INSERT INTO artifacts(run_id, kind, rel_path, sha256, created_at, analysis_version)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, kind, rel_path, digest, stamp, analysis_version),
        )


def _measure(filename: str, dataset: ExportDataset) -> dict[str, int]:
    if filename == "keywords.csv":
        return {"rows": len(dataset.keywords)}
    if filename == "needs.csv":
        return {"rows": len(dataset.needs)}
    if filename == "evidence.csv":
        return {"rows": len(dataset.evidence)}
    if filename == "graph.graphml":
        return {"nodes": len(dataset.graph.nodes), "edges": len(dataset.graph.edges)}
    return {}


@dataclass(frozen=True)
class ExportResult:
    run_id: str
    analysis_version: str
    export_dir: str
    files: tuple[LandedFile, ...]
    dataset: ExportDataset
    verification: Verification | None

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "analysis_version": self.analysis_version,
            "export_dir": self.export_dir,
            "files": [item.as_dict() for item in self.files],
            "dataset": self.dataset.summary(),
            "verification": None if self.verification is None else self.verification.as_dict(),
        }


def export_run(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
    at: datetime | None = None,
    snapshot: str | None = None,
    verify: bool = True,
) -> ExportResult:
    """生成并原子落盘五类产物，登记 `artifacts`，然后反读校验。"""
    repo = Repository(conn, migrate=False)
    stamp = at if at is not None else utc_now()
    builder = ExportBuilder(
        repo, run_id, analysis_version=analysis_version, extractor_version=extractor_version,
        generated_at=stamp,
    )
    dataset = builder.build()
    directory = export_directory(paths, run_id, analysis_version, snapshot)
    payloads = {
        "keywords.csv": render_csv(KEYWORD_COLUMNS, dataset.keywords),
        "needs.csv": render_csv(NEED_COLUMNS, dataset.needs),
        "evidence.csv": render_csv(EVIDENCE_COLUMNS, dataset.evidence),
        "graph.graphml": dataset.graphml.document,
    }
    landed: list[LandedFile] = []
    for filename, kind in EXPORT_FILES:
        rel_path = paths.relative(directory / filename)
        if kind == "report_json":
            text = render_json(
                report_document(
                    builder, dataset, landed, export_dir=paths.relative(directory),
                    snapshot=snapshot, generated_at=stamp,
                )
            )
        else:
            text = payloads[filename]
        body = text.encode("utf-8")
        try:
            artifact = write_artifact(
                conn, paths, run_id, kind="export", payload=body, rel_path=rel_path, at=stamp
            )
        except ArtifactConflict as exc:
            raise ExportConflict(
                f"{filename}：{exc}；同一 analysis_version 的内容变了，"
                "请用 snapshot 参数写入新的快照目录，别覆盖已登记的历史导出"
            ) from exc
        _register_export(
            repo, run_id, kind, artifact.rel_path, artifact.sha256,
            analysis_version=analysis_version, stamp=stamp.isoformat(),
        )
        landed.append(
            LandedFile(
                filename=filename,
                artifact_kind=kind,
                rel_path=artifact.rel_path,
                sha256=artifact.sha256,
                size_bytes=len(body),
                written=artifact.file_written,
                measure=_measure(filename, dataset),
            )
        )
    verification = (
        verify_export(
            conn, paths, run_id, analysis_version=analysis_version,
            extractor_version=extractor_version, snapshot=snapshot,
        )
        if verify
        else None
    )
    repo.record_event(
        run_id,
        "analysis_export",
        detail={
            "analysis_version": analysis_version,
            "extractor_version": extractor_version,
            "export_dir": paths.relative(directory),
            "snapshot": snapshot,
            "files": [item.as_dict() for item in landed],
            "dataset": dataset.summary(),
            "verification": None if verification is None else verification.as_dict(),
        },
        at=stamp,
    )
    if verification is not None and not verification.ok:
        repo.record_event(
            run_id, "export_verification_failed", detail=verification.as_dict(), at=stamp
        )
        raise ExportVerificationError(
            "导出反读校验未通过：" + "；".join(verification.problems)
        )
    return ExportResult(
        run_id=run_id,
        analysis_version=analysis_version,
        export_dir=paths.relative(directory),
        files=tuple(landed),
        dataset=dataset,
        verification=verification,
    )


# ------------------------------------------------------------------ 反读校验


@dataclass(frozen=True)
class Verification:
    run_id: str
    analysis_version: str
    checks: tuple[dict, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "analysis_version": self.analysis_version,
            "checks": len(self.checks),
            "passed": sum(1 for item in self.checks if item["ok"]),
            "problems": list(self.problems),
            "detail": list(self.checks),
        }


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.problems: list[str] = []

    def add(self, name: str, ok: object, detail: dict | None = None) -> None:
        passed = bool(ok)
        self.items.append({"check": name, "ok": passed, "detail": detail or {}})
        if not passed:
            self.problems.append(f"{name} — {json.dumps(detail or {}, ensure_ascii=False)[:400]}")

    def verification(self, run_id: str, analysis_version: str) -> Verification:
        return Verification(
            run_id=run_id,
            analysis_version=analysis_version,
            checks=tuple(self.items),
            problems=tuple(self.problems),
        )


def _value_matches(declared: str, value: str) -> bool:
    """按 GraphML 声明类型能否解析这个值。类型声明错了，别家的图工具会在半个文件上崩掉。"""
    try:
        if declared == "boolean":
            return value in ("true", "false")
        if declared == "long":
            int(value)
        elif declared == "double":
            float(value)
        elif declared != "string":
            return False
    except ValueError:
        return False
    return True


def _verify_csv(
    checks: _Checks, *, filename: str, text: str, columns: tuple[str, ...], rows: Iterable[dict]
) -> tuple[list[str], list[list[str]]]:
    header, parsed = parse_csv(text)
    rows = list(rows)
    checks.add(
        f"{filename}.表头与列定义一致",
        header == list(columns),
        {
            "missing": [name for name in columns if name not in header],
            "extra": [name for name in header if name not in columns],
        },
    )
    checks.add(f"{filename}.行数与库一致", len(parsed) == len(rows), {"file": len(parsed), "db": len(rows)})
    ragged = [index for index, row in enumerate(parsed) if len(row) != len(header)]
    checks.add(f"{filename}.每行列数一致", not ragged, {"rows": ragged[:5]})
    expected = [[csv_cell(row.get(name)) for name in columns] for row in rows]
    diffs: list[dict] = []
    for index, (got_row, want_row) in enumerate(zip(parsed, expected)):
        for position, (got, want) in enumerate(zip(got_row, want_row)):
            if got != want:
                diffs.append(
                    {
                        "row": index,
                        "column": header[position] if position < len(header) else position,
                        "read": got[:80],
                        "rebuilt": want[:80],
                    }
                )
                if len(diffs) >= 5:
                    break
        if len(diffs) >= 5:
            break
    checks.add(
        f"{filename}.逐格与重建一致", not diffs and len(parsed) == len(expected), {"diffs": diffs}
    )
    offenders = [cell for row in parsed for cell in row if cell[:1] in FORMULA_CHARS]
    checks.add(f"{filename}.无未转义的公式字符", not offenders, {"cells": offenders[:5]})
    return header, parsed


def _verify_needs_policy(
    checks: _Checks,
    conn: sqlite3.Connection,
    run_id: str,
    analysis_version: str,
    header: list[str],
    parsed: list[list[str]],
) -> None:
    """展示口径的独立复核：不看导出代码怎么算的，只看文件里的每一行在库里是什么。

    被改过的文件是**陌生输入**：列缺失、行残缺、need_id 非数字都要变成一条失败的检查，
    而不是让校验器抛异常——校验器崩了等于把"文件被改过"伪装成"校验没跑"。
    """
    needed = ("need_id", "score", "evidence_status", "statement_kind", "evidence_rows")
    missing = [name for name in needed if name not in header]
    checks.add("needs.csv.含展示口径所需列", not missing, {"missing": missing})
    if missing:
        return

    def cell(row: list[str], name: str) -> str:
        index = header.index(name)
        return row[index] if index < len(row) else ""

    offenders = [
        row[0]
        for row in parsed
        if cell(row, "evidence_status") not in DISPLAYED_STATUSES
        or cell(row, "score") == ""
        or cell(row, "statement_kind") in ("solution_idea", "search_intent_candidate")
        or cell(row, "evidence_rows") in ("", "0")
    ]
    checks.add("needs.csv.每行都可展示且有证据", not offenders, {"offenders": offenders[:5]})
    ids: list[int] = []
    unparsable: list[str] = []
    for row in parsed:
        try:
            ids.append(int(cell(row, "need_id")))
        except ValueError:
            unparsable.append(cell(row, "need_id"))
    checks.add("needs.csv.行号是整数", not unparsable, {"offenders": unparsable[:5]})
    if not ids:
        checks.add(
            "needs.csv.与库里的行同口径",
            not unparsable,
            {"note": "本版本没有可展示需求", "offenders": unparsable[:5]},
        )
        return
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, score, evidence_status FROM needs WHERE run_id = ? AND analysis_version = ?"
        f" AND id IN ({marks})",
        (run_id, analysis_version, *ids),
    ).fetchall()
    drift = [
        str(row["id"])
        for row in rows
        if row["score"] is None or str(row["evidence_status"]) not in DISPLAYED_STATUSES
    ]
    checks.add(
        "needs.csv.与库里的行同口径",
        not drift and len(rows) == len(ids),
        {"drift": drift, "in_file": len(ids), "found_in_db": len(rows)},
    )


def _verify_graphml(checks: _Checks, *, text: str, graph: Graph) -> None:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        checks.add("graph.graphml.可解析", False, {"error": str(exc)})
        return
    namespace = root.tag.split("}")[0].lstrip("<{") if "}" in root.tag else ""
    tag = lambda name: f"{{{namespace}}}{name}" if namespace else name  # noqa: E731
    keys = {
        key.get("id"): (key.get("for"), key.get("attr.name"), key.get("attr.type"))
        for key in root.findall(tag("key"))
    }
    element = root.find(tag("graph"))
    nodes = [] if element is None else element.findall(tag("node"))
    edges = [] if element is None else element.findall(tag("edge"))
    checks.add("graph.graphml.有向图声明", element is not None and element.get("edgedefault") == "directed",
               {"edgedefault": None if element is None else element.get("edgedefault")})
    checks.add("graph.graphml.节点数与库一致", len(nodes) == len(graph.nodes),
               {"file": len(nodes), "db": len(graph.nodes)})
    checks.add("graph.graphml.边数与库一致", len(edges) == len(graph.edges),
               {"file": len(edges), "db": len(graph.edges)})
    identifiers = [item.get("id") for item in nodes + edges]
    checks.add(
        "graph.graphml.元素 id 唯一且非空",
        all(identifiers) and len(set(identifiers)) == len(identifiers),
        {"duplicated": [name for name in set(identifiers) if identifiers.count(name) > 1]},
    )
    bad_domain: list[dict] = []
    bad_type: list[dict] = []
    refs: dict[str, str] = {}
    labels: dict[str, str] = {}
    for item, domain in [(node, "node") for node in nodes] + [(edge, "edge") for edge in edges]:
        for data in item.findall(tag("data")):
            meta = keys.get(data.get("key"))
            if meta is None or meta[0] != domain:
                bad_domain.append({"element": item.get("id"), "key": data.get("key")})
                continue
            _, name, declared = meta
            value = data.text or ""
            if not _value_matches(declared, value):
                bad_type.append({"element": item.get("id"), "attr": name, "type": declared, "value": value[:40]})
            if domain == "node" and name == "ref":
                refs[item.get("id")] = value
            if domain == "node" and name == "label":
                labels[item.get("id")] = value
    checks.add("graph.graphml.key 域与用法一致", not bad_domain, {"offenders": bad_domain[:5]})
    checks.add("graph.graphml.声明类型与值兼容", not bad_type, {"offenders": bad_type[:5]})
    checks.add(
        "graph.graphml.节点身份齐全",
        sorted(refs.values()) == sorted(node.node_id for node in graph.nodes),
        {"file": len(refs), "db": len(graph.nodes)},
    )
    checks.add(
        "graph.graphml.前缀能区分节点类型",
        all(value.split(":")[0] in NODE_PREFIX.values() for value in refs.values()),
        {"offenders": [value for value in refs.values() if value.split(":")[0] not in NODE_PREFIX.values()]},
    )
    want_labels = {node.node_id: xml_text(node.label) for node in graph.nodes}
    wrong_labels = [key for key, value in labels.items() if refs.get(key) in want_labels
                    and want_labels[refs[key]] != value]
    checks.add("graph.graphml.标签与库里一致", not wrong_labels, {"offenders": wrong_labels[:5]})
    relation_key = next(
        (key_id for key_id, meta in keys.items() if meta[0] == "edge" and meta[1] == "relation"),
        None,
    )
    got_edges = sorted(
        (
            refs.get(edge.get("source")),
            refs.get(edge.get("target")),
            _data_value(edge, tag, keys, relation_key),
        )
        for edge in edges
    )
    want_edges = sorted((edge.source, edge.target, edge.relation) for edge in graph.edges)
    checks.add(
        "graph.graphml.边三元组与库一致",
        got_edges == want_edges,
        {"missing_in_file": [list(item) for item in want_edges if item not in got_edges][:5],
         "extra_in_file": [list(item) for item in got_edges if item not in want_edges][:5]},
    )


def _data_value(element, tag, keys: dict, key_id: str | None) -> str | None:
    if key_id is None:
        return None
    for data in element.findall(tag("data")):
        if data.get("key") == key_id:
            return data.text or ""
    return None


def _verify_report(
    checks: _Checks,
    *,
    text: str,
    paths: RuntimePaths,
    run_id: str,
    dataset: ExportDataset,
    builder: ExportBuilder,
) -> None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        checks.add("report.json.可解析", False, {"error": str(exc)})
        return
    required = {
        "meta", "run", "config", "budget", "statistics", "analysis",
        "graph", "exported", "anomalies", "limitations", "verification",
    }
    missing = sorted(required - set(payload))
    checks.add("report.json.段落齐全", not missing, {"missing": missing})
    meta = payload.get("meta", {})
    checks.add(
        "report.json.归属正确",
        meta.get("run_id") == run_id and meta.get("analysis_version") == dataset.analysis_version,
        {"run_id": meta.get("run_id"), "analysis_version": meta.get("analysis_version")},
    )
    needed_versions = [
        name
        for name in (
            "code_version", "protocol_version", "extractor_version", "schema_version",
            "head_schema_version", "merge_method", "family_method", "generated_at",
        )
        if not meta.get(name)
    ]
    checks.add("report.json.版本与时间戳齐全", not needed_versions, {"missing": needed_versions})
    run = payload.get("run", {})
    checks.add(
        "report.json.含运行配置与随机种子",
        "snapshot" in payload.get("config", {}) and run.get("random_seed") == int(builder.run["random_seed"])
        and run.get("config_hash") == str(builder.run["config_hash"]),
        {"random_seed": run.get("random_seed"), "config_keys": sorted(payload.get("config", {}))},
    )
    budget = payload.get("budget", {})
    checks.add(
        "report.json.含预算账本",
        isinstance(budget.get("keys"), dict) and isinstance(budget.get("revisions"), list),
        {"keys": sorted(budget.get("keys") or {}), "revisions": len(budget.get("revisions") or [])},
    )
    limitations = payload.get("limitations") or []
    checks.add("report.json.含局限", len(limitations) >= 5, {"count": len(limitations)})
    anomalies = payload.get("anomalies") or {}
    checks.add(
        "report.json.异常清单与自检一致",
        anomalies.get("violation_codes") == dataset.anomalies["violation_codes"]
        and anomalies.get("needs_without_score") == dataset.anomalies["needs_without_score"],
        {"file": anomalies.get("violation_codes"), "db": dataset.anomalies["violation_codes"]},
    )
    statistics = payload.get("statistics")
    checks.add(
        "report.json.统计与库同步",
        statistics == dataset.statistics,
        {"note": "导出之后库里又写入了新事实，报告统计已经过期"},
    )
    files = (payload.get("exported") or {}).get("files") or []
    names = sorted(item.get("filename") for item in files)
    data_files = sorted(name for name in EXPORT_FILENAMES if name != "report.json")
    checks.add(
        "report.json.四类数据产物齐列",
        names == data_files and payload.get("verification", {}).get("self_reference_excluded") is True,
        {"listed": names, "want": data_files},
    )
    stale: list[str] = []
    for item in files:
        digest = item.get("sha256")
        if digest is None:
            continue
        try:
            target = paths.resolve_within_run(str(item.get("rel_path")), run_id)
        except ValueError:
            stale.append(str(item.get("filename")))
            continue
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            stale.append(str(item.get("filename")))
    checks.add("report.json.内嵌哈希与磁盘一致", not stale, {"stale": stale})


EXPORT_FILENAMES = tuple(filename for filename, _ in EXPORT_FILES)


def verify_export(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
    snapshot: str | None = None,
) -> Verification:
    """把写出去的文件当陌生输入读回来：解析、逐格对照、按声明类型解析 GraphML、核对报告内嵌哈希。

    校验的对照物是**重新从库里重建的同一批行**，不是导出时的内存对象——
    这样"文件被改过"、"库里又写了新事实"、"转义把数据吃掉了一块"三种情况都能被发现。
    """
    repo = Repository(conn, migrate=False)
    builder = ExportBuilder(
        repo, run_id, analysis_version=analysis_version, extractor_version=extractor_version
    )
    dataset = builder.build()
    checks = _Checks()
    directory = export_directory(paths, run_id, analysis_version, snapshot)
    texts: dict[str, str] = {}
    for filename, kind in EXPORT_FILES:
        rel_path = paths.relative(directory / filename)
        registered = conn.execute(
            "SELECT sha256 FROM artifacts WHERE run_id = ? AND kind = ? AND rel_path = ?",
            (run_id, kind, rel_path),
        ).fetchone()
        landed = conn.execute(
            "SELECT sha256, kind FROM raw_artifacts WHERE run_id = ? AND rel_path = ?",
            (run_id, rel_path),
        ).fetchone()
        checks.add(
            f"{filename}.已登记导出",
            registered is not None and landed is not None and str(landed["kind"]) == "export",
            {
                "artifacts": registered is not None,
                "raw_artifacts": landed is not None,
                "artifact_kind": None if landed is None else str(landed["kind"]),
            },
        )
        try:
            target = paths.resolve_within_run(rel_path, run_id)
        except ValueError as exc:
            checks.add(f"{filename}.在 run 目录内", False, {"error": str(exc), "rel_path": rel_path})
            continue
        if not target.is_file():
            checks.add(f"{filename}.文件存在", False, {"rel_path": rel_path})
            continue
        body = target.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        checks.add(
            f"{filename}.哈希与登记一致",
            registered is not None
            and landed is not None
            and str(registered["sha256"]) == digest
            and str(landed["sha256"]) == digest,
            {"file": digest, "artifacts": None if registered is None else str(registered["sha256"])},
        )
        try:
            texts[filename] = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            checks.add(f"{filename}.UTF-8 可读", False, {"error": str(exc)})
    for filename, columns, rows in (
        ("keywords.csv", KEYWORD_COLUMNS, dataset.keywords),
        ("needs.csv", NEED_COLUMNS, dataset.needs),
        ("evidence.csv", EVIDENCE_COLUMNS, dataset.evidence),
    ):
        text = texts.get(filename)
        if text is None:
            continue
        header, parsed = _verify_csv(checks, filename=filename, text=text, columns=columns, rows=rows)
        if filename == "needs.csv":
            _verify_needs_policy(checks, conn, run_id, analysis_version, header, parsed)
    if "graph.graphml" in texts:
        _verify_graphml(checks, text=texts["graph.graphml"], graph=dataset.graph)
    if "report.json" in texts:
        _verify_report(checks, text=texts["report.json"], paths=paths, run_id=run_id,
                       dataset=dataset, builder=builder)
    if directory.is_dir():
        unexpected = sorted(
            item.name for item in directory.iterdir() if item.is_file() and item.name not in EXPORT_FILENAMES
        )
        checks.add("导出目录只含五类产物", not unexpected, {"extra": unexpected})
    return checks.verification(run_id, analysis_version)


__all__ = [
    "EVIDENCE_COLUMNS",
    "EXPORT_DIRNAME",
    "EXPORT_FILES",
    "EXPORT_FILENAMES",
    "FORMULA_CHARS",
    "KEYWORD_COLUMNS",
    "NEED_COLUMNS",
    "ExportBuilder",
    "ExportConflict",
    "ExportDataset",
    "ExportResult",
    "ExportVerificationError",
    "GraphMLPlan",
    "LandedFile",
    "Verification",
    "csv_cell",
    "export_directory",
    "export_run",
    "merge_graphs",
    "parse_csv",
    "render_csv",
    "render_graphml",
    "render_json",
    "report_document",
    "verify_export",
    "xml_text",
]
