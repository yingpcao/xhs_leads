"""P0-28：关键词发现图 + 主题→问题→需求→证据图，以及"展示需求必须有有效引用"的自检。

图是**读出来的**，不是写出来的：`analysis/graph.py` 不碰任何写事务，所有节点与边都由库里的
`needs / problems / evidence / cluster_assignments / discovery_edges` 重建。因此这里钉的是：

- 需求图能一路点下去：Topic → Problem → Need → Evidence → Post（联想候选只到联想版本行），
  Problem 再经 `extracted_from` 回指观察到它的那个 Keyword；
- 节点 id 带类型前缀：`problems.id` 与 `needs.id` 都是各自表里的小整数，`prob:1` 与 `need:1`
  必须同时存在且不是同一个节点；
- `SUGGESTS` 只认平台真实返回的边，我们自己生成的变体词进不了图，且被挡掉的条数要报出来；
- 自检能分辨三种"看起来一样"的状态：展示需求没有证据引用（`need_without_evidence`）、
  引用的证据行不在本 run 本版本（`evidence_row_missing`）、原句在版本行里定位不到
  （`evidence_quote_broken`）；另有 `orphan_evidence`（没人认领的证据）与
  `problem_in_several_clusters`（同一问题两个有效归属）；
- 撤销的归属不画进图，但行仍在库里；换 `analysis_version` 各得一张图，互不掺假。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.graph import (
    REL_CANDIDATE_FROM,
    REL_EXTRACTED_FROM,
    REL_HAS_PROBLEM,
    REL_MERGED_INTO,
    REL_OBSERVED_IN,
    REL_SUPPORTED_BY,
    REL_SUGGESTS,
    DemandGraphBuilder,
    build_discovery_graph,
    build_run_graph,
    node_id,
)
from xhs_discovery.analysis.store import revoke_cluster_assignment
from xhs_discovery.models.enums import CandidateOrigin
from xhs_discovery.models.source import KeywordText

from tests.conftest import T0
from tests.test_analysis_clustering import (
    HELP_QUESTION,
    ZHONGKAO,
    cluster,
    explicit_of,
    observe_under,
    payload_of,
)
from tests.test_analysis_extraction import (
    at,
    observe_body,
    observe_suggestions,
    observe_titles,
    prepare,
    rows,
)

SECOND_VERSION = "p0-rule-baseline.4"


def build(storage, *, analysis_version=ANALYSIS_VERSION, extractor_version=EXTRACTOR_VERSION):
    builder = DemandGraphBuilder(
        storage.repo,
        storage.run_id,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
    )
    graph = builder.build()
    return SimpleNamespace(builder=builder, graph=graph, integrity=builder.check(graph))


def relations(graph, *, source: str | None = None) -> list[str]:
    return [
        edge.relation
        for edge in graph.edges
        if source is None or edge.source == source
    ]


def node(graph, identifier: str):
    return next((item for item in graph.nodes if item.node_id == identifier), None)


def codes(integrity) -> list[str]:
    return [row.code for row in integrity.violations]


# ------------------------------------------------------------------ 需求图链路


def test_the_demand_graph_chains_topic_problem_need_evidence_to_post(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, "第二批到底怎么排？")
    cluster(storage)
    graph = build(storage).graph
    summary = graph.summary()
    assert summary["nodes_by_kind"]["topic"] == 1
    assert summary["nodes_by_kind"]["keyword"] == 1
    assert summary["nodes_by_kind"]["problem"] == 2
    assert summary["nodes_by_kind"]["evidence"] == 2
    # 正文来自同一篇笔记：两张证据共用一个 post 节点，图不会因为它来自两次观察就复制帖子身份
    assert summary["nodes_by_kind"]["post"] == 1
    assert summary["edges_by_relation"][REL_HAS_PROBLEM] == 2
    assert summary["nodes_by_kind"]["need"] == len(
        rows(
            storage.conn,
            "SELECT id FROM needs WHERE run_id = ? AND analysis_version = ?",
            (storage.run_id, ANALYSIS_VERSION),
        )
    )

    topic = node(graph, node_id("topic", "上海中考"))
    assert topic is not None and topic.label == "上海中考"
    explicit = explicit_of(storage)[0]
    explicit_node = node(graph, node_id("need", explicit["id"]))
    assert explicit_node.attributes["displayable"] is True
    assert explicit_node.attributes["statement_kind"] == "explicit_problem"
    assert REL_HAS_PROBLEM in relations(graph, source=topic.node_id)
    problems = [item for item in graph.nodes if item.kind == "problem"]
    assert problems and all(
        REL_MERGED_INTO in relations(graph, source=item.node_id) for item in problems
    )
    supported = [
        edge for edge in graph.edges if edge.source == explicit_node.node_id
        and edge.relation == REL_SUPPORTED_BY
    ]
    assert len(supported) == len(payload_of(explicit)["evidence_ids"])
    for edge in supported:
        evidence = node(graph, edge.target)
        assert evidence.attributes["source_field"] in {"title", "body"}
        assert REL_OBSERVED_IN in relations(graph, source=evidence.node_id), (
            "帖子证据必须能一路点到那篇帖子；点不到的原句不算有效引用"
        )
        post = node(graph, graph.outgoing(evidence.node_id, REL_OBSERVED_IN)[0].target)
        assert post.attributes["access_url"].startswith("https://www.xiaohongshu.com/explore/")
        assert post.attributes["synthetic"] == 1, "fixture 证据要一眼看出是模拟的"
    keyword = node(graph, node_id("keyword", keyword_id))
    assert keyword is not None
    assert REL_EXTRACTED_FROM in [
        edge.relation
        for edge in graph.edges
        if edge.target == keyword.node_id
    ]


def test_node_ids_are_type_prefixed_so_integer_ids_cannot_collide(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    graph = build(storage).graph
    identifiers = [item.node_id for item in graph.nodes]
    assert len(identifiers) == len(set(identifiers))
    problem_ids = {item.node_id for item in graph.by_kind("problem")}
    need_ids = {item.node_id for item in graph.by_kind("need")}
    assert all(item.startswith("prob:") for item in problem_ids)
    assert all(item.startswith("need:") for item in need_ids)
    shared = {item.split(":")[1] for item in problem_ids} & {item.split(":")[1] for item in need_ids}
    assert shared, "两张表的整数 id 本来就会撞上，前缀是唯一区分手段"
    assert not (problem_ids & need_ids)
    for edge in graph.edges:
        assert edge.source in identifiers and edge.target in identifiers, "图里不许有悬空端点"


def test_the_inferred_need_shows_where_it_was_derived_from(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    graph = build(storage).graph
    inferred = rows(
        storage.conn,
        "SELECT * FROM needs WHERE run_id = ? AND statement_kind = 'inferred_need'",
        (storage.run_id,),
    )[0]
    need_node = node(graph, node_id("need", inferred["id"]))
    assert need_node.attributes["generated_expression"] is True
    assert need_node.attributes["displayable"] is True, "推断需求可展示，但措辞不是用户写的"
    derived = graph.outgoing(need_node.node_id, "derived_from")
    assert len(derived) == 1
    parent = node(graph, derived[0].target)
    assert parent.attributes["statement_kind"] == "explicit_problem"
    # 推断表达引用的是父簇那批原句证据：措辞是生成的，句子不是。
    assert [edge.target.split(":")[1] for edge in graph.outgoing(need_node.node_id, REL_SUPPORTED_BY)] == [
        str(value) for value in payload_of(inferred)["evidence_ids"]
    ]


# ------------------------------------------------------------------ 联想与营销


def test_a_suggestion_candidate_reaches_a_suggestion_version_and_never_a_post(storage) -> None:
    """联想不冒充帖子事实：它在图上唯一的出边是 `candidate_from`，且不可展示。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    cluster(storage)
    run = build(storage)
    graph = run.graph
    candidate = rows(
        storage.conn,
        "SELECT * FROM needs WHERE run_id = ? AND statement_kind = 'search_intent_candidate'",
        (storage.run_id,),
    )[0]
    need_node = node(graph, node_id("need", candidate["id"]))
    assert need_node.attributes["displayable"] is False
    assert need_node.attributes["evidence_status"] == "unverified_candidate"
    outgoing = graph.outgoing(need_node.node_id)
    assert [edge.relation for edge in outgoing] == [REL_CANDIDATE_FROM]
    suggestion = node(graph, outgoing[0].target)
    assert suggestion.kind == "suggestion"
    assert suggestion.label == "上海中考志愿怎么填"
    assert graph.by_kind("evidence") and not any(
        edge.relation == REL_OBSERVED_IN and edge.source == need_node.node_id
        for edge in graph.edges
    )
    assert run.integrity.ok, "候选本来就该长这样：自检不许把它当成违规"


def test_marketing_evidence_is_unadopted_but_not_orphaned(storage) -> None:
    """广告句：证据与问题行都在，只是没有需求采纳它。这与"孤立证据"是两件事。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考辅导机构一对一报名，怎么收费？"])
    cluster(storage)
    run = build(storage)
    assert run.graph.by_kind("need") == ()
    assert run.integrity.ok, run.integrity.as_dict()
    assert run.integrity.checked["evidence_rows"] == 1
    assert run.integrity.checked["evidence_not_on_a_need"] == 1


# ------------------------------------------------------------------ 发现图


def test_generated_variants_never_become_suggests_edges(storage) -> None:
    keyword_id = prepare(storage)
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    child = storage.repo.ensure_keyword("上海中考志愿怎么填", "上海中考志愿怎么填", seen_at=T0)
    storage.repo.upsert_run_keyword(
        storage.run_id, child, depth=1, origin="generated", status="queued", seen_at=T0
    )
    storage.conn.execute(
        "INSERT INTO discovery_edges(run_id, parent_keyword_id, child_keyword_id, rank,"
        " session_id, observed_at, origin) VALUES (?, ?, ?, ?, ?, ?, 'generated')",
        (storage.run_id, keyword_id, child, 9, "s-gen", at(3).isoformat()),
    )
    storage.conn.commit()
    graph, withheld = build_discovery_graph(storage.repo, storage.run_id)
    assert withheld == 1, "被挡掉的边要报数，静默少一条等于伪造图"
    assert [edge.relation for edge in graph.edges] and all(
        edge.relation == REL_SUGGESTS for edge in graph.edges
    )
    assert all(
        edge.attributes["session_id"] != "s-gen" for edge in graph.edges
    ), "generated 变体不许出现在推荐边里"
    assert len(graph.edges) == len(
        [
            row for row in rows(storage.conn, "SELECT * FROM discovery_edges WHERE run_id = ?",
                               (storage.run_id,))
            if row["origin"] == CandidateOrigin.observed.value
        ]
    )


def test_discovery_nodes_carry_their_own_problem_count(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    cluster(storage)
    graph, _withheld = build_discovery_graph(storage.repo, storage.run_id)
    seed = node(graph, node_id("keyword", keyword_id))
    assert seed.attributes["problems_extracted"] == 1
    assert seed.label == "上海中考"


# ------------------------------------------------------------------ 自检反例


def test_a_displayed_need_without_any_reference_is_refused(storage) -> None:
    """P0 退出门槛的直接形态：展示需求的证据引用为空 → 违规，且不许悄悄画成孤立节点。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    need = explicit_of(storage)[0]
    storage.conn.execute(
        "UPDATE needs SET evidence_complete_json = ? WHERE id = ?",
        (json.dumps({"evidence_ids": []}, ensure_ascii=False), need["id"]),
    )
    storage.conn.commit()
    run = build(storage)
    assert "need_without_evidence" in codes(run.integrity)
    violation = next(
        row for row in run.integrity.violations if row.code == "need_without_evidence"
    )
    assert violation.node_id == node_id("need", need["id"])
    assert run.integrity.ok is False


def test_evidence_rows_of_another_batch_are_not_an_escape_route(storage) -> None:
    """引用的证据行不属于本 run 本版本 → `evidence_row_missing`，不是"当作没有"。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    storage.conn.execute("DELETE FROM evidence WHERE run_id = ?", (storage.run_id,))
    storage.conn.commit()
    run = build(storage)
    assert codes(run.integrity).count("evidence_row_missing") == len(
        [need for need in run.builder.needs() if payload_of(need)["evidence_ids"]]
    )
    assert run.integrity.checked["evidence_rows"] == 0


def test_a_moved_quote_is_a_broken_reference(storage) -> None:
    """失效引用：版本行被换掉后，原句不再落在它引用的那一行里。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    version_id = rows(
        storage.conn, "SELECT content_version_id AS id FROM evidence WHERE run_id = ?",
        (storage.run_id,),
    )[0]["id"]
    storage.conn.execute(
        "UPDATE post_content_versions SET text = ? WHERE id = ?", ("帖子被改过了", version_id)
    )
    storage.conn.commit()
    run = build(storage)
    assert set(codes(run.integrity)) == {"evidence_quote_broken"}
    broken = [row for row in run.integrity.violations if row.code == "evidence_quote_broken"]
    assert len(broken) == 2, "同一句被显性簇与推断需求各引用一次，两处都要报"
    assert broken[0].detail["char_start"] == 0


def test_evidence_that_no_problem_claimed_is_orphaned(storage) -> None:
    """问题行没了，证据就是孤立证据——它不再是任何需求的可点原文。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    storage.conn.execute("DELETE FROM cluster_assignments WHERE run_id = ?", (storage.run_id,))
    storage.conn.execute("DELETE FROM problems WHERE run_id = ?", (storage.run_id,))
    storage.conn.commit()
    run = build(storage)
    assert set(codes(run.integrity)) == {"orphan_evidence"}
    assert len(run.integrity.violations) == run.integrity.checked["evidence_rows"]
    assert run.graph.by_kind("problem") == ()


# ------------------------------------------------------------------ 版本与撤销


def test_revoked_membership_leaves_the_graph_but_stays_in_the_database(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    need = explicit_of(storage)[0]
    problem_id = payload_of(need)["problem_ids"][0]
    revoke_cluster_assignment(
        storage.repo,
        storage.run_id,
        problem_id=problem_id,
        analysis_version=ANALYSIS_VERSION,
        reason="人工判定不是同一件需求",
        at=at(9),
    )
    run = build(storage)
    assert rows(
        storage.conn, "SELECT * FROM cluster_assignments WHERE problem_id = ?", (problem_id,)
    ), "撤销不删行"
    problem_node = node(run.graph, node_id("problem", problem_id))
    assert problem_node is not None
    assert relations(run.graph, source=problem_node.node_id) == [REL_EXTRACTED_FROM], (
        "撤销后的归属不再画边，但问题与来源词仍然连得通"
    )
    assert run.integrity.ok, "撤销是合法状态，自检不许把它报成违规"


def test_each_analysis_version_gets_its_own_graph(storage) -> None:
    """结论层各画一张图，身份层仍然共用：帖子与关键词是跨版本的事实，需求和证据不是。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    cluster(storage, analysis_version=SECOND_VERSION)
    current = build(storage)
    other = build(storage, analysis_version=SECOND_VERSION)
    assert current.integrity.ok and other.integrity.ok

    def identifiers(graph, kind: str) -> set[str]:
        return {item.node_id for item in graph.by_kind(kind)}

    for kind in ("need", "evidence"):
        assert identifiers(current.graph, kind) & identifiers(other.graph, kind) == set(), (
            f"{kind} 节点按分析版本各一批，重叠就是掺假"
        )
    for kind in ("post", "keyword", "topic", "problem"):
        assert identifiers(current.graph, kind) == identifiers(other.graph, kind), (
            f"{kind} 属于身份/来源层：换分析版本不该复制出第二份"
        )
    for item in current.graph.by_kind("need"):
        assert item.attributes["analysis_version"] == ANALYSIS_VERSION
    assert len(current.graph.by_kind("need")) == len(other.graph.by_kind("need"))


def test_rebuilding_from_rows_is_deterministic(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION, "志愿填报怎么改"])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    cluster(storage)
    first = build(storage)
    second = build(storage)
    assert [item.node_id for item in first.graph.nodes] == [
        item.node_id for item in second.graph.nodes
    ]
    assert [item.edge_id for item in first.graph.edges] == [
        item.edge_id for item in second.graph.edges
    ]
    assert first.integrity.as_dict() == second.integrity.as_dict()


def test_build_run_graph_returns_both_views_and_the_integrity_report(storage) -> None:
    keyword_id = prepare(storage)
    question = "名额分配怎么申请"
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    observe_titles(storage, keyword_id, [HELP_QUESTION, question])
    other = storage.repo.ensure_keyword("中考", "中考", seen_at=T0)
    observe_under(storage, other, ZHONGKAO, [question], tag="kw2", session_id="s-kw2")
    cluster(storage)
    discovery, demand, integrity = build_run_graph(storage.repo, storage.run_id)
    assert integrity.ok, integrity.as_dict()
    assert discovery.edges and {edge.relation for edge in discovery.edges} == {REL_SUGGESTS}
    assert demand.summary()["nodes_by_kind"]["need"] >= 2
    assert integrity.checked["needs"] == len(rows(
        storage.conn, "SELECT id FROM needs WHERE run_id = ?", (storage.run_id,)
    ))
    conflicts = [
        need for need in explicit_of(storage) if "#conflict[" in need["canonical_text"]
    ]
    assert conflicts, "同一句在两个主题词下被看到：分裂簇也要进图"
    split_node = node(demand, node_id("need", conflicts[0]["id"]))
    assert split_node.attributes["displayable"] is True
    assert split_node.attributes["stable_key"] == conflicts[0]["stable_key"]
