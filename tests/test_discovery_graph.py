"""P0-14：观察入库与发现图读取视图。

守住的口径（DATA_MODEL 第 4 节）：
- `run_keywords` 只有"首次归属"（父/深度/分支），用来稳定调度与展示；
- 完整多父关系只在 `discovery_edges`，每条边带平台 rank、会话与观察时间；
- 队列去重不得抹掉观察：同一个词被看到 N 次必须还能统计出 N 次与最好排名；
- 环路要能枚举出可达前缀并显式报告截断，不能"看起来只有一条路径"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from xhs_discovery.discovery.graph import MAX_PATH_DEPTH, DiscoveryGraph
from xhs_discovery.discovery.ingest import ingest_suggestion
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.enums import CandidateOrigin, OperationType, ResultState
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.source import KeywordText, OperationContext, SuggestionResult
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.normalize import KeywordNormalizer

from tests.conftest import T0, build_run_identity


def session_for(session_id: str, collected_at: datetime) -> SessionRef:
    return SessionRef(
        session_id=session_id,
        collected_at=collected_at,
        source_name="fixture",
        source_version="0.0.1+test",
        synthetic=True,
    )


@dataclass
class GraphEnv:
    repo: Repository
    run_id: str
    normalizer: KeywordNormalizer = field(default_factory=KeywordNormalizer)
    observations: int = 0

    def register(
        self,
        text: str,
        *,
        raw: str | None = None,
        depth: int = 0,
        parent: int | None = None,
        status: str = "queued",
        branch_key: str | None = None,
    ) -> int:
        """把词登记进全局身份层并绑定到 run（首次归属）。"""
        normalized = self.normalizer.normalize(text).normalized
        keyword_id = self.repo.ensure_keyword(normalized, raw or text, seen_at=T0)
        branch_id = None
        if branch_key is not None:
            self.repo.ensure_branch(self.run_id, branch_key, label=branch_key)
            branch_id = self.repo.branch_id(self.run_id, branch_key)
        self.repo.upsert_run_keyword(
            self.run_id,
            keyword_id,
            parent_keyword_id=parent,
            depth=depth,
            branch_id=branch_id,
            origin=CandidateOrigin.observed.value,
            status=status,
            seen_at=T0,
        )
        return keyword_id

    def context(self, keyword_id: int, *, revisit_seq: int = 0) -> OperationContext:
        text = self.repo.conn.execute(
            "SELECT normalized_text FROM keywords WHERE id = ?", (keyword_id,)
        ).fetchone()["normalized_text"]
        return OperationContext(
            run_id=self.run_id,
            keyword_id=keyword_id,
            keyword=KeywordText(raw=text, normalized=text),
            operation_type=OperationType.suggest,
            revisit_seq=revisit_seq,
            attempt_no=1,
        )

    def edge(self, parent_id: int, child_id: int, *, origin: CandidateOrigin = CandidateOrigin.observed,
             rank: int | None = None, session_id: str | None = None) -> None:
        self.repo.record_discovery_edge(
            self.run_id, parent_id, child_id,
            origin=origin, rank=rank, session_id=session_id, observed_at=T0,
        )

    def observe(self, parent_id: int, texts: list[str], *, revisit_seq: int = 0,
                branch_key: str | None = None,
                state: ResultState = ResultState.completed,
                error: ErrorInfo | None = None):
        """一次联想观察：开操作 → 开 attempt → 入库（边与节点都由 ingest 写）。"""
        self.observations += 1
        operation_id = self.repo.open_operation(
            self.run_id, parent_id, OperationType.suggest, revisit_seq, branch_key=branch_key
        )
        attempt_id = self.repo.start_attempt(operation_id, 1)
        result = SuggestionResult(
            state=state,
            context=self.context(parent_id, revisit_seq=revisit_seq),
            session=session_for(f"sess-{self.observations}", T0 + timedelta(minutes=self.observations)),
            items=[{"text": text, "rank": index + 1} for index, text in enumerate(texts)],
            error=error,
        )
        return self.ingest(result, operation_id, attempt_id, branch_key=branch_key)

    def ingest(self, result: SuggestionResult, operation_id: int, attempt_id: int,
               *, branch_key: str | None = None):
        return ingest_suggestion(
            self.repo, self.run_id, result,
            normalizer=self.normalizer,
            operation_id=operation_id,
            attempt_id=attempt_id,
            branch_key=branch_key,
        )


@pytest.fixture
def env(storage) -> GraphEnv:
    return GraphEnv(repo=storage.repo, run_id=storage.run_id)


@pytest.fixture
def graph(storage, env: GraphEnv) -> DiscoveryGraph:
    return DiscoveryGraph(storage.repo, env.run_id)


def detail_of(env: GraphEnv, kind: str) -> list[dict]:
    return [json.loads(row["detail_json"]) for row in env.repo.events(env.run_id, kind=kind)]


def test_observed_children_create_edges_nodes_and_ranks(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考", branch_key="b-root")
    ingest = env.observe(seed, ["上海中考志愿填报", "上海中考分数线"], branch_key="b-root")
    assert ingest.created is True
    assert [child.normalized for child in ingest.children] == ["上海中考志愿填报", "上海中考分数线"]
    assert [child.rank for child in ingest.children] == [1, 2]
    assert all(child.is_new_in_run for child in ingest.children)

    edges = graph.edges()
    assert [(e.parent_keyword_id, e.child_keyword_id, e.rank) for e in edges] == [
        (seed, ingest.children[0].keyword_id, 1),
        (seed, ingest.children[1].keyword_id, 2),
    ]
    assert all(edge.origin is CandidateOrigin.observed for edge in edges)
    assert edges[0].session_id == "sess-1"

    child = graph.node(ingest.children[0].keyword_id)
    assert child.depth == 1
    assert child.parent_keyword_id == seed
    assert child.status == "queued"
    assert child.branch_key == "b-root"
    assert child.origin == "observed"
    # 子词同时写三处：观察项、内容版本、发现图，缺一处后面的证据定位就会静默失真
    versions = env.repo.conn.execute(
        "SELECT rank, text FROM suggestion_versions WHERE observation_id = ? ORDER BY rank",
        (ingest.observation_id,),
    ).fetchall()
    assert [row["rank"] for row in versions] == [1, 2]


def test_multi_parent_keeps_every_edge_but_one_stable_owner(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    a = env.register("上海中考志愿填报", depth=1, parent=seed)
    b = env.register("上海中考分数线", depth=1, parent=seed)
    c = env.register("上海中考名额分配", depth=1, parent=seed)
    child_id = env.observe(a, ["上海中考建档线"]).children[0].keyword_id
    env.observe(b, ["上海中考建档线"])
    env.observe(c, ["上海中考建档线"])

    assert {edge.parent_keyword_id for edge in graph.parents(child_id)} == {a, b, c}
    # 首次归属只用于稳定展示与调度，不裁掉其他父边
    assert graph.node(child_id).parent_keyword_id == a
    assert graph.independent_parent_count(child_id) == 3
    assert graph.children(a) and graph.children(b)


def test_same_parent_repeat_observation_adds_edge_but_not_node(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    child_id = env.observe(seed, ["上海中考建档线", "上海中考分数线"]).children[0].keyword_id
    env.observe(seed, ["上海中考建档线"], revisit_seq=1)

    # 边按 (父, 子, 会话) 唯一：两次会话各一条，重复观察不会伪装成多父共识
    assert len(graph.parents(child_id)) == 2
    assert graph.independent_parent_count(child_id) == 1
    stats = graph.observation_stats(child_id)
    assert stats["observed_times"] == 2
    assert stats["best_rank"] == 1
    assert stats["distinct_sessions"] == 2
    assert len([node for node in graph.nodes() if node.keyword_id == child_id]) == 1


def test_self_observation_is_recorded_as_observation_not_an_edge(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    ingest = env.observe(seed, ["上海中考", "上海中考自招"])
    assert ingest.self_loop_ranks == (1,)
    assert [child.keyword_id for child in ingest.children] != [seed]
    assert [edge.child_keyword_id for edge in graph.edges()] == [ingest.children[0].keyword_id]
    rows = env.repo.conn.execute(
        "SELECT text_raw, rank FROM suggestion_items WHERE observation_id = ? ORDER BY rank",
        (ingest.observation_id,),
    ).fetchall()
    assert [row["rank"] for row in rows] == [1, 2]
    assert rows[0]["text_raw"] == "上海中考"


def test_replay_of_same_attempt_creates_no_duplicate_identity(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    first = env.observe(seed, ["上海中考建档线", "上海中考分数线"])
    operation_id = int(
        env.repo.operation_by_key(env.run_id, seed, OperationType.suggest, 0)["id"]
    )
    attempt_id = int(env.repo.attempts(operation_id)[0]["id"])

    replay = env.ingest(
        SuggestionResult(
            state=ResultState.completed,
            context=env.context(seed),
            session=session_for("sess-1", T0 + timedelta(minutes=1)),
            items=[{"text": "上海中考建档线", "rank": 1}, {"text": "上海中考分数线", "rank": 2}],
        ),
        operation_id,
        attempt_id,
    )
    assert replay.created is False
    assert replay.observation_id == first.observation_id
    assert [child.normalized for child in replay.children] == [child.normalized for child in first.children]
    assert all(child.is_new_in_run is False for child in replay.children)
    assert len(graph.edges()) == 2
    assert env.repo.conn.execute(
        "SELECT COUNT(*) AS c FROM suggestion_observations"
    ).fetchone()["c"] == 1


def test_generated_edges_never_count_as_platform_consensus(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    probe = env.register("上海中考怎么选", depth=1, parent=seed)
    env.edge(seed, probe, origin=CandidateOrigin.generated)
    env.observe(seed, ["上海中考建档线"])

    assert [edge.origin for edge in graph.edges(origin=CandidateOrigin.generated)] == [
        CandidateOrigin.generated
    ]
    assert len(graph.edges(origin=CandidateOrigin.observed)) == 1
    assert graph.independent_parent_count(probe) == 0
    assert graph.parents(probe)[0].session_id is None
    assert graph.parents(probe)[0].rank is None


def test_cycle_reports_truncated_branches_instead_of_looking_acyclic(env: GraphEnv, graph: DiscoveryGraph):
    a = env.register("上海中考")
    b = env.register("上海中考志愿填报", depth=1, parent=a)
    c = env.register("上海中考志愿排序", depth=2, parent=b)
    env.edge(a, b, rank=1, session_id="sess-1")
    env.edge(b, c, rank=1, session_id="sess-2")
    env.edge(c, a, rank=2, session_id="sess-3")

    assert graph.paths_to(c) == ((a, b, c),)
    assert [edge.parent_keyword_id for edge in graph.parents(a)] == [c]
    events = detail_of(env, "graph_path_truncated")
    assert events and events[-1]["keyword_id"] == c
    assert events[-1]["skipped_branches"] >= 1


def test_depth_cap_truncates_without_silently_losing_the_event(env: GraphEnv, graph: DiscoveryGraph):
    ids = [env.register(f"链{i}", depth=i) for i in range(6)]
    for parent, child in zip(ids, ids[1:]):
        env.edge(parent, child)
    assert graph.paths_to(ids[5], max_depth=MAX_PATH_DEPTH) == (tuple(ids),)
    assert graph.paths_to(ids[5], max_depth=3) == ()
    assert any(item["max_depth"] == 3 for item in detail_of(env, "graph_path_truncated"))


def test_diamond_paths_are_all_enumerated(env: GraphEnv, graph: DiscoveryGraph):
    root = env.register("上海中考")
    left = env.register("上海中考志愿填报", depth=1, parent=root)
    right = env.register("上海中考分数线", depth=1, parent=root)
    shared = env.observe(left, ["上海中考建档线"]).children[0].keyword_id
    env.observe(right, ["上海中考建档线"])
    env.edge(root, left, rank=1, session_id="sess-0")
    env.edge(root, right, rank=2, session_id="sess-0")

    paths = graph.paths_to(shared)
    assert len(paths) == 2
    assert (root, left, shared) in paths and (root, right, shared) in paths
    assert all(path[-1] == shared for path in paths)
    assert all(path[0] == root for path in paths)


def test_describe_assembles_aliases_parents_paths_and_observations(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    child_id = env.observe(seed, ["上海2026中考建档线"]).children[0].keyword_id
    # 同一身份的另一种写法只多一条别名，不多一个节点
    assert env.repo.ensure_keyword("上海2026中考建档线", "上海２０２６中考建档线", seen_at=T0) == child_id

    described = graph.describe(child_id)
    assert described["normalized"] == "上海2026中考建档线"
    assert set(described["aliases"]) == {"上海2026中考建档线", "上海２０２６中考建档线"}
    assert [edge["parent_keyword_id"] for edge in described["parents"]] == [seed]
    assert described["paths_to_root"] == [f"{seed} > {child_id}"]
    assert described["observations"]["observed_times"] == 1
    assert described["independent_parents"] == 1
    assert graph.node(1234567) is None
    assert graph.paths_to(1234567) == ()


def test_graph_views_are_scoped_to_the_run(env: GraphEnv, storage):
    seed = env.register("上海中考")
    child_id = env.observe(seed, ["上海中考建档线"]).children[0].keyword_id

    storage.repo.create_run(
        build_run_identity("run-b", seed=KeywordText(raw="北京中考", normalized="北京中考")),
        config_json="{}",
    )
    second = GraphEnv(repo=storage.repo, run_id="run-b")
    other_seed = second.register("北京中考")
    other_child = second.observe(other_seed, ["北京中考分数线"]).children[0].keyword_id

    first_graph = DiscoveryGraph(storage.repo, "run-a")
    b_graph = DiscoveryGraph(storage.repo, "run-b")
    assert [edge.child_keyword_id for edge in first_graph.edges()] == [child_id]
    assert [edge.child_keyword_id for edge in b_graph.edges()] == [other_child]
    assert first_graph.node(other_seed) is None
    assert first_graph.paths_to(other_seed) == ()
    assert first_graph.observation_stats(other_child)["observed_times"] == 0


def test_describe_rejects_keyword_outside_the_run(env: GraphEnv, graph: DiscoveryGraph):
    foreign = env.repo.ensure_keyword("外部词", "外部词", seen_at=T0)
    with pytest.raises(ValueError, match="不在 run"):
        graph.describe(foreign)


def test_nodes_filter_by_status_and_edges_keep_operation_id(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    child_id = env.observe(seed, ["上海中考建档线"]).children[0].keyword_id
    env.repo.upsert_run_keyword(
        env.run_id, child_id, parent_keyword_id=seed, depth=9,
        branch_id=None, origin="observed", status="explored", seen_at=T0,
    )
    assert [node.keyword_id for node in graph.nodes(status="explored")] == [child_id]
    assert [node.keyword_id for node in graph.nodes(status="queued")] == [seed]
    edge = graph.edges()[0]
    operation_id = env.repo.operation_by_key(env.run_id, seed, OperationType.suggest, 0)["id"]
    assert edge.operation_id == int(operation_id)
    assert edge.as_dict()["origin"] == "observed"
    # 首次归属不被后续 upsert 改写：depth/parent 只在第一次发现时固定
    node = graph.node(child_id)
    assert node.depth == 1 and node.parent_keyword_id == seed


def test_ingest_refuses_unregistered_keyword_and_wrong_operation(env: GraphEnv):
    context = OperationContext(
        run_id=env.run_id, keyword_id=None,
        keyword=KeywordText(raw="上海中考", normalized="上海中考"),
        operation_type=OperationType.suggest, revisit_seq=0, attempt_no=1,
        allow_pre_registry=True,
    )
    with pytest.raises(ValueError, match="预入库身份只用于数据源逻辑键"):
        env.ingest(
            SuggestionResult(state=ResultState.completed, context=context,
                             session=session_for("sess-x", T0), items=[]),
            operation_id=1, attempt_id=1,
        )

    registered = env.register("上海中考")
    search_context = OperationContext(
        run_id=env.run_id, keyword_id=registered,
        keyword=KeywordText(raw="上海中考", normalized="上海中考"),
        operation_type=OperationType.search, revisit_seq=0, attempt_no=1,
    )
    # 契约层先拒绝：搜索上下文包不进 SuggestionResult，入库层的同名校验只是纵深防御
    with pytest.raises(ValidationError, match="只能用于 suggest"):
        SuggestionResult(
            state=ResultState.completed, context=search_context,
            session=session_for("sess-x", T0), items=[],
        )


def test_failed_observation_still_records_a_row_with_zero_items(env: GraphEnv, graph: DiscoveryGraph):
    seed = env.register("上海中考")
    ingest = env.observe(
        seed, [], revisit_seq=7,
        state=ResultState.failed,
        error=ErrorInfo(
            code="WORD_PAGE_TIMEOUT", message="超时", category="network",
            retryable=True, human_action_required=False,
        ),
    )
    assert ingest.children == ()
    assert ingest.state is ResultState.failed
    assert graph.edges() == ()
    row = env.repo.conn.execute(
        "SELECT item_count, state FROM suggestion_observations WHERE id = ?",
        (ingest.observation_id,),
    ).fetchone()
    assert (row["item_count"], row["state"]) == (0, "failed")
    assert graph.observation_stats(seed)["observed_times"] == 0
