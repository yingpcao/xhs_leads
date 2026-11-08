"""双图谱读取视图（P0-28）。

`PROJECT_SPEC.md` 第 9 节要的是两张不同的图，共用一套节点/边表示才能一次导出：

- **发现图** Keyword → SUGGESTS → Keyword：复盘搜索路径、分支收益与覆盖。边只认平台真实
  返回（`discovery_edges.origin = 'observed'`）：把我们自己生成的变体词也画成推荐边，
  等于把"我们想过这个词"伪装成"平台把这个词递给用户过"，多父共识立刻失真。
  被挡在图外的边数随图一起返回，不静默吞掉。
- **需求图** Topic → Problem → Need → Evidence(Post)，Problem 通过 `extracted_from`
  回指 Keyword；推断需求另有 `derived_from` 指向父簇，联想意图候选只指向联想版本行。

节点 id 一律带类型前缀（`kw:`、`topic:`、`prob:`、`need:`、`ev:`、`post:`、`sugg:`）：
`problems.id` 与 `needs.id` 都是各自表里的小整数，不加前缀会在同一张图里撞成两个含义不同的
节点，GraphML 也分不出该给哪种 key 声明类型。

本模块**只读不写**，并自带一致性自检（`DemandGraphBuilder.check`）：每个展示需求都必须能找到
属于本 run 本分析版本的证据行，且原句仍能在它引用的版本行里定位；每条证据都必须被某个问题行
认领（P0.4 的"无孤立证据"）。检查不过就返回违规清单，不静默丢边——"图里少了几条"和
"库里本来就没有"必须是两件能区分的事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.discovery.extract import core_text
from xhs_discovery.discovery.graph import DiscoveryGraph
from xhs_discovery.models.enums import CandidateOrigin, SourceField, StatementKind
from xhs_discovery.storage.repository import Repository

# 节点类型 → id 前缀。前缀同时是导出里 `kind` 属性的取值来源。
NODE_PREFIX = {
    "keyword": "kw",
    "topic": "topic",
    "problem": "prob",
    "need": "need",
    "evidence": "ev",
    "post": "post",
    "suggestion": "sugg",
}

REL_SUGGESTS = "suggests"
REL_HAS_PROBLEM = "has_problem"
REL_EXTRACTED_FROM = "extracted_from"
REL_MERGED_INTO = "merged_into"
REL_SUPPORTED_BY = "supported_by"
REL_DERIVED_FROM = "derived_from"
REL_OBSERVED_IN = "observed_in"
REL_CANDIDATE_FROM = "candidate_from"

# 允许上看板的需求状态：`unverified_candidate`（构想、联想候选）永远排在这之外。
DISPLAYED_STATUSES = ("verified", "inferred")


def node_id(kind: str, identity: object) -> str:
    if kind not in NODE_PREFIX:
        raise ValueError(f"未知的节点类型 {kind!r}")
    return f"{NODE_PREFIX[kind]}:{identity}"


def _payload(need: dict) -> dict:
    raw = need.get("evidence_complete_json")
    return json.loads(raw) if raw else {}


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


@dataclass(frozen=True)
class GraphNode:
    kind: str
    node_id: str
    label: str
    attributes: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, **self.attributes}


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    attributes: dict[str, object] = field(default_factory=dict)

    @property
    def edge_id(self) -> str:
        return f"{self.source}~{self.relation}~>{self.target}"

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            **self.attributes,
        }


@dataclass(frozen=True)
class Graph:
    run_id: str
    analysis_version: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    def node_ids(self) -> set[str]:
        return {node.node_id for node in self.nodes}

    def by_kind(self, kind: str) -> tuple[GraphNode, ...]:
        return tuple(node for node in self.nodes if node.kind == kind)

    def outgoing(self, source: str, relation: str | None = None) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self.edges
            if edge.source == source and (relation is None or edge.relation == relation)
        )

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "analysis_version": self.analysis_version,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "nodes_by_kind": _counts([node.kind for node in self.nodes]),
            "edges_by_relation": _counts([edge.relation for edge in self.edges]),
        }


# ------------------------------------------------------------------ 发现图


def build_discovery_graph(
    repo: Repository, run_id: str, *, analysis_version: str = ANALYSIS_VERSION
) -> tuple[Graph, int]:
    """Keyword 图；返回 (图, 被挡在图外的非 observed 边数)。

    `analysis_version` 只影响节点上那个"这个词抽到几条问题"的计数口径：换版本重跑分析时，
    发现图的形状不变，但每个词的问题数必须跟着换成新版本的事实，否则导出里会出现
    旧版本的计数配上新版本的需求图。
    """
    view = DiscoveryGraph(repo, run_id)
    conn = repo.conn
    # `problems` 表没有 keyword_id（问题身份是全局句子 + 抽取器版本），它的来源词记在证据行上。
    # 一条证据对应一个问题行，所以按证据数回看"这个词下面抽到了几条问题"是同一件事，
    # 而且带 run 与版本口径。
    problems_seen = {
        int(row["keyword_id"]): int(row["problems"])
        for row in conn.execute(
            "SELECT keyword_id, COUNT(*) AS problems FROM evidence WHERE run_id = ?"
            " AND analysis_version = ? GROUP BY keyword_id",
            (run_id, analysis_version),
        ).fetchall()
    }
    nodes = [
        GraphNode(
            kind="keyword",
            node_id=node_id("keyword", node.keyword_id),
            label=node.normalized,
            attributes={
                "origin": node.origin,
                "depth": node.depth,
                "status": node.status,
                "branch_key": node.branch_key,
                "revisit_count": node.revisit_count,
                "problems_extracted": problems_seen.get(node.keyword_id, 0),
            },
        )
        for node in view.nodes()
    ]
    observed = view.edges(origin=CandidateOrigin.observed)
    edges = [
        GraphEdge(
            source=node_id("keyword", edge.parent_keyword_id),
            target=node_id("keyword", edge.child_keyword_id),
            relation=REL_SUGGESTS,
            attributes={
                "rank": edge.rank,
                "session_id": edge.session_id,
                "observed_at": edge.observed_at,
                "operation_id": edge.operation_id,
            },
        )
        for edge in observed
    ]
    return (
        Graph(
            run_id=run_id,
            analysis_version=analysis_version,
            nodes=tuple(nodes),
            edges=tuple(edges),
        ),
        len(view.edges()) - len(edges),
    )


# ------------------------------------------------------------------ 需求图


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    node_id: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "node_id": self.node_id, **self.detail}


@dataclass(frozen=True)
class GraphIntegrity:
    run_id: str
    analysis_version: str
    violations: tuple[Violation, ...]
    checked: dict[str, int]

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted({row.code for row in self.violations}))

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "analysis_version": self.analysis_version,
            "checked": self.checked,
            "violation_codes": _codes(self.violations),
            "violations": [row.as_dict() for row in self.violations],
        }


def _codes(violations: tuple[Violation, ...]) -> dict[str, int]:
    return _counts([row.code for row in violations])


class DemandGraphBuilder:
    """Topic → Problem → Need → Evidence(Post)；一切节点与边都从库里的行重建。"""

    def __init__(
        self,
        repo: Repository,
        run_id: str,
        *,
        analysis_version: str = ANALYSIS_VERSION,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        if not run_id:
            raise ValueError("需求图必须按 run 构建：跨 run 的证据链不是同一次采样")
        self.repo = repo
        self.conn = repo.conn
        self.run_id = run_id
        self.analysis_version = analysis_version
        self.extractor_version = extractor_version
        self._labels: dict[int, str] = {}

    # ------------------------------------------------------------------ 取数

    def needs(self) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM needs WHERE run_id = ? AND analysis_version = ? ORDER BY id",
                (self.run_id, self.analysis_version),
            ).fetchall()
        ]

    def evidence_rows(self, ids: Iterable[int] = ()) -> dict[int, dict]:
        """本 run 本分析版本的证据行，带上定位要的容器文本与回链要的 note_id/链接。

        `observation_observed_at` 是发布时间文本所属卡片观察的时刻（C-12 锚点）：
        导出与展示从这里回查，不再按 (keyword, post) 猜首条记录。
        """
        sql = (
            "SELECT e.*, v.text AS container_text, s.text AS suggestion_text, s.rank AS suggestion_rank,"
            " p.note_id, p.access_url, po.observed_at AS observation_observed_at"
            " FROM evidence e"
            " LEFT JOIN post_content_versions v ON v.id = e.content_version_id"
            " LEFT JOIN suggestion_versions s ON s.id = e.suggestion_version_id"
            " LEFT JOIN posts p ON p.id = e.post_id"
            " LEFT JOIN post_observations po ON po.id = e.post_observation_id"
            "  AND po.run_id = e.run_id"
            " WHERE e.run_id = ? AND e.analysis_version = ?"
        )
        params: list[object] = [self.run_id, self.analysis_version]
        ids = sorted({int(value) for value in ids})
        if ids:
            # 显式给 id 时也要带 run/版本条件：需求载荷里的整数不是通行证。
            marks = ",".join("?" for _ in ids)
            sql += f" AND e.id IN ({marks})"
            params.extend(ids)
        sql += " ORDER BY e.id"
        return {
            int(row["id"]): dict(row)
            for row in self.conn.execute(sql, tuple(params)).fetchall()
        }

    def problems(self) -> dict[int, dict]:
        return {
            int(row["id"]): dict(row)
            for row in self.conn.execute(
                "SELECT * FROM problems WHERE run_id = ? AND extractor_version = ? ORDER BY id",
                (self.run_id, self.extractor_version),
            ).fetchall()
        }

    def assignments(self) -> dict[int, dict]:
        """只取当前有效归属：撤销掉的边不画进图，但行仍然留在库里可复核。"""
        return {
            int(row["problem_id"]): dict(row)
            for row in self.conn.execute(
                "SELECT * FROM cluster_assignments WHERE run_id = ? AND analysis_version = ?"
                " AND superseded_at IS NULL ORDER BY id",
                (self.run_id, self.analysis_version),
            ).fetchall()
        }

    def keyword_label(self, keyword_id: int) -> str:
        if keyword_id not in self._labels:
            row = self.conn.execute(
                "SELECT normalized_text FROM keywords WHERE id = ?", (keyword_id,)
            ).fetchone()
            self._labels[keyword_id] = str(row["normalized_text"]) if row else f"#{keyword_id}"
        return self._labels[keyword_id]

    # ------------------------------------------------------------------ 构图

    def build(self) -> Graph:
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []

        def add(node: GraphNode) -> str:
            nodes.setdefault(node.node_id, node)
            return node.node_id

        def link(source: str, target: str, relation: str, **attributes: object) -> None:
            edges.append(GraphEdge(source=source, target=target, relation=relation, attributes=dict(attributes)))

        needs = self.needs()
        referenced = {
            int(evidence_id)
            for need in needs
            for evidence_id in _payload(need).get("evidence_ids", [])
        }
        evidence = self.evidence_rows(referenced)
        problems = self.problems()
        assignments = self.assignments()
        # 问题 ↔ 证据是一比一（抽取一批写一行），靠"来源字段 + 版本行 + 归并后文本 + 抽取器版本"
        # 精确配对，这样 `extracted_from` 回指的原词才是**这个词**下的观察，
        # 而不是同簇别条证据的；换抽取器版本重跑时也不会串到旧版本的问题行上。
        by_source = {
            (str(row["source_field"]), row["content_version_id"], row["suggestion_version_id"],
             core_text(str(row["quote"])), str(row["extractor_version"])): int(row["id"])
            for row in evidence.values()
        }

        for need in needs:
            need_node = add(
                GraphNode(
                    kind="need",
                    node_id=node_id("need", need["id"]),
                    label=str(need["canonical_text"]),
                    attributes={
                        "statement_kind": str(need["statement_kind"]),
                        "evidence_status": str(need["evidence_status"]),
                        "stable_key": need["stable_key"],
                        "score": need["score"],
                        "analysis_version": str(need["analysis_version"]),
                        "displayable": str(need["evidence_status"]) in DISPLAYED_STATUSES,
                        "generated_expression": bool(_payload(need).get("generated_expression")),
                    },
                )
            )
            payload = _payload(need)
            for evidence_id in payload.get("evidence_ids", []):
                row = evidence.get(int(evidence_id))
                if row is None:
                    continue
                # 需求→证据与证据→帖子/联想在同一次遍历里发出：分两趟写，
                # "有需求没证据"和"有证据没需求"在图上会长成同一个样子。
                evidence_node = self._add_evidence(add, link, row)
                link(need_node, evidence_node, REL_SUPPORTED_BY)
            if not payload.get("evidence_ids") and payload.get("suggestion_version_id") is not None:
                # 联想意图候选没有证据行（它不是帖子事实），靠联想版本行回指原词与位次：
                # 图上它只允许出现 `candidate_from` 这一条内容边，永远连不到 post 节点。
                self._link_suggestion_source(add, link, need_node, int(payload["suggestion_version_id"]))
            for problem_id in payload.get("problem_ids", []):
                problem = problems.get(int(problem_id))
                if problem is None:
                    continue
                problem_node = self._add_problem(add, link, problem)
                assignment = assignments.get(int(problem_id))
                if assignment is not None:
                    link(
                        problem_node,
                        need_node,
                        REL_MERGED_INTO,
                        auto_merged=assignment["auto_merged"],
                        similarity=assignment["similarity"],
                        method=str(assignment["method"]),
                        conflicts=json.loads(assignment["attributes_conflict_json"] or "[]"),
                    )
                evidence_id = by_source.get(
                    (
                        str(problem["source_field"]),
                        problem["content_version_id"],
                        problem["suggestion_version_id"],
                        str(problem["canonical_text"]),
                        str(problem["extractor_version"]),
                    )
                )
                row = evidence.get(int(evidence_id)) if evidence_id is not None else None
                if row is not None:
                    keyword_node = add(
                        GraphNode(
                            kind="keyword",
                            node_id=node_id("keyword", row["keyword_id"]),
                            label=self.keyword_label(int(row["keyword_id"])),
                            attributes={"from_evidence": int(row["id"])},
                        )
                    )
                    link(problem_node, keyword_node, REL_EXTRACTED_FROM, session_id=str(row["session_id"]))
            # 派生关系要能追溯到**每一个**父簇：同轮廓多父簇并成一组时引用全列（C-11-r2）。
            derived_keys = payload.get("derived_from_stable_keys") or (
                [payload["derived_from_stable_key"]] if payload.get("derived_from_stable_key") else []
            )
            for derived in derived_keys:
                parent = next((item for item in needs if item["stable_key"] == derived), None)
                if parent is not None:
                    link(
                        need_node,
                        node_id("need", parent["id"]),
                        REL_DERIVED_FROM,
                        generated_expression=True,
                    )
        return Graph(
            run_id=self.run_id,
            analysis_version=self.analysis_version,
            nodes=tuple(nodes.values()),
            edges=tuple(edges),
        )

    def _link_suggestion_source(self, add, link, need_node: str, version_id: int) -> None:
        row = self.conn.execute(
            "SELECT sv.text, sv.rank, so.session_id FROM suggestion_versions sv"
            " JOIN suggestion_observations so ON so.id = sv.observation_id WHERE sv.id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            return
        suggestion_node = add(
            GraphNode(
                kind="suggestion",
                node_id=node_id("suggestion", version_id),
                label=str(row["text"]),
                attributes={"rank": int(row["rank"]), "session_id": str(row["session_id"])},
            )
        )
        link(need_node, suggestion_node, REL_CANDIDATE_FROM, evidence_id=None)

    def _add_problem(self, add, link, problem: dict) -> str:
        problem_node = add(
            GraphNode(
                kind="problem",
                node_id=node_id("problem", problem["id"]),
                label=str(problem["canonical_text"]),
                attributes={
                    "intent": problem["intent"],
                    "source_type": str(problem["source_type"]),
                    "object": problem["object"],
                    "location": problem["location"],
                    "year": problem["year"],
                    "polarity": problem["polarity"],
                    "counts_as_valid": problem["counts_as_valid_problem_cluster"],
                },
            )
        )
        topic = problem["topic"]
        if topic:
            topic_node = add(
                GraphNode(
                    kind="topic",
                    node_id=node_id("topic", str(topic)),
                    label=str(topic),
                    attributes={"source": "problem_topic"},
                )
            )
            link(topic_node, problem_node, REL_HAS_PROBLEM)
        return problem_node

    def _add_evidence(self, add, link, row: dict) -> str:
        evidence_node = add(
            GraphNode(
                kind="evidence",
                node_id=node_id("evidence", row["id"]),
                label=str(row["quote"]),
                attributes={
                    "source_field": str(row["source_field"]),
                    "char_start": row["char_start"],
                    "char_end": row["char_end"],
                    "note_id": row["note_id"],
                    "access_url": row["access_url"],
                    "session_id": str(row["session_id"]),
                    "keyword_id": row["keyword_id"],
                    "operation_id": row["operation_id"],
                    "attempt_no": row["attempt_no"],
                    "content_version_id": row["content_version_id"],
                    "suggestion_version_id": row["suggestion_version_id"],
                    "synthetic": row["synthetic"],
                },
            )
        )
        if str(row["source_field"]) == SourceField.suggestion.value:
            version_id = row["suggestion_version_id"]
            if version_id is not None:
                suggestion_node = add(
                    GraphNode(
                        kind="suggestion",
                        node_id=node_id("suggestion", version_id),
                        label=str(row["suggestion_text"]),
                        attributes={
                            "rank": None if row["suggestion_rank"] is None else int(row["suggestion_rank"]),
                            "synthetic": row["synthetic"],
                        },
                    )
                )
                link(evidence_node, suggestion_node, REL_CANDIDATE_FROM)
            return evidence_node
        post_node = add(
            GraphNode(
                kind="post",
                node_id=node_id("post", row["post_id"]),
                label=str(row["note_id"]),
                attributes={"access_url": row["access_url"], "synthetic": row["synthetic"]},
            )
        )
        link(evidence_node, post_node, REL_OBSERVED_IN)
        return evidence_node

    # ------------------------------------------------------------------ 自检

    def check(self, graph: Graph) -> GraphIntegrity:
        """展示需求必须能点到原文；对不上的地方报违规，不改图、不删边。"""
        violations: list[Violation] = []
        needs = self.needs()
        evidence = self.evidence_rows()
        problems = self.problems()
        claimed = {
            (str(row["source_field"]), row["content_version_id"], row["suggestion_version_id"],
             str(row["canonical_text"]), str(row["extractor_version"]))
            for row in problems.values()
        }
        referenced: set[int] = set()

        for need in needs:
            payload = _payload(need)
            need_node = node_id("need", need["id"])
            ids = [int(value) for value in payload.get("evidence_ids", [])]
            referenced.update(ids)
            if str(need["evidence_status"]) in DISPLAYED_STATUSES and not ids:
                violations.append(
                    Violation(
                        code="need_without_evidence",
                        message="展示需求没有任何证据引用，不能出现在看板上",
                        node_id=need_node,
                        detail={"statement_kind": str(need["statement_kind"])},
                    )
                )
            for evidence_id in ids:
                row = evidence.get(evidence_id)
                if row is None:
                    violations.append(
                        Violation(
                            code="evidence_row_missing",
                            message="需求引用的证据行不属于本 run 与本分析版本",
                            node_id=need_node,
                            detail={"evidence_id": evidence_id},
                        )
                    )
                    continue
                violations.extend(self._check_quote(need_node, row))
                violations.extend(self._check_fact_kind(need, payload, row))

        for evidence_id, row in evidence.items():
            key = (
                str(row["source_field"]),
                row["content_version_id"],
                row["suggestion_version_id"],
                core_text(str(row["quote"])),
                str(row["extractor_version"]),
            )
            if key not in claimed:
                violations.append(
                    Violation(
                        code="orphan_evidence",
                        message="证据行没有被任何问题行认领（P0.4 门槛：无孤立证据）",
                        node_id=node_id("evidence", evidence_id),
                    )
                )

        for row in self.conn.execute(
            "SELECT problem_id, COUNT(*) AS seen FROM cluster_assignments"
            " WHERE run_id = ? AND analysis_version = ? AND superseded_at IS NULL"
            " GROUP BY problem_id HAVING seen > 1",
            (self.run_id, self.analysis_version),
        ).fetchall():
            violations.append(
                Violation(
                    code="problem_in_several_clusters",
                    message="同一个问题在本版本下有多个有效归属",
                    node_id=node_id("problem", int(row["problem_id"])),
                    detail={"active_assignments": int(row["seen"])},
                )
            )

        return GraphIntegrity(
            run_id=self.run_id,
            analysis_version=self.analysis_version,
            violations=tuple(violations),
            checked={
                "needs": len(needs),
                "displayed_needs": sum(
                    1 for need in needs if str(need["evidence_status"]) in DISPLAYED_STATUSES
                ),
                "evidence_rows": len(evidence),
                "evidence_not_on_a_need": len(set(evidence) - referenced),
                "problems": len(problems),
                "nodes": len(graph.nodes),
                "edges": len(graph.edges),
            },
        )

    @staticmethod
    def _check_quote(need_node: str, row: dict) -> list[Violation]:
        """原句必须落在它自己引用的那一行版本文本里（失效引用反例）。"""
        quote = str(row["quote"])
        start, end = int(row["char_start"]), int(row["char_end"])
        if str(row["source_field"]) == SourceField.suggestion.value:
            container = row["suggestion_text"]
            located = container is not None and quote in str(container)
        else:
            container = row["container_text"]
            located = container is not None and str(container)[start:end] == quote
        if located:
            return []
        return [
            Violation(
                code="evidence_quote_broken",
                message="原句在它引用的版本行里定位不到（失效引用）",
                node_id=node_id("evidence", int(row["id"])),
                detail={"need_node": need_node, "char_start": start, "char_end": end},
            )
        ]

    @staticmethod
    def _check_fact_kind(need: dict, payload: dict, row: dict) -> list[Violation]:
        kind = str(need["statement_kind"])
        from_suggestion = str(row["source_field"]) == SourceField.suggestion.value
        violations: list[Violation] = []
        if payload.get("counts_as_valid_need") and from_suggestion:
            violations.append(
                Violation(
                    code="suggestion_as_post_fact",
                    message="联想文本被当成帖子事实计入有效需求",
                    node_id=node_id("evidence", int(row["id"])),
                    detail={"need_id": str(need["id"])},
                )
            )
        if kind == StatementKind.search_intent_candidate.value and not from_suggestion:
            violations.append(
                Violation(
                    code="candidate_uses_post_evidence",
                    message="联想意图候选挂上了帖子证据",
                    node_id=node_id("evidence", int(row["id"])),
                    detail={"need_id": str(need["id"])},
                )
            )
        return violations


def build_run_graph(
    repo: Repository,
    run_id: str,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
) -> tuple[Graph, Graph, GraphIntegrity]:
    """一次给出两张图与需求图自检结果，供导出与看板共用。"""
    discovery, _withheld = build_discovery_graph(repo, run_id, analysis_version=analysis_version)
    builder = DemandGraphBuilder(
        repo, run_id, analysis_version=analysis_version, extractor_version=extractor_version
    )
    demand = builder.build()
    return discovery, demand, builder.check(demand)


__all__ = [
    "DISPLAYED_STATUSES",
    "NODE_PREFIX",
    "DemandGraphBuilder",
    "Graph",
    "GraphEdge",
    "GraphIntegrity",
    "GraphNode",
    "REL_CANDIDATE_FROM",
    "REL_DERIVED_FROM",
    "REL_EXTRACTED_FROM",
    "REL_HAS_PROBLEM",
    "REL_MERGED_INTO",
    "REL_OBSERVED_IN",
    "REL_SUPPORTED_BY",
    "REL_SUGGESTS",
    "Violation",
    "build_discovery_graph",
    "build_run_graph",
    "node_id",
]
