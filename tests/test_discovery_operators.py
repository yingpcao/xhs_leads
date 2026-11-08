"""P0-15：四类扩展算子，以及 generated 与 observed 的硬性分界。

平台真给的（observed）只能来自联想 items；本地推出来的（generated）必须带来源片段，
否则"这个词从哪来"就查不回去。裁剪、横向与探针全部是确定性字符串操作：
关掉模型与随机也必须能跑出同样的候选，重放才谈得上逐字段比对。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_discovery.config import load_settings
from xhs_discovery.discovery.operators import (
    OPERATORS,
    ExpansionInput,
    OperatorSettings,
    content_lateral,
    expand,
    is_degenerate_query,
    phrase_fallback,
    problem_probe,
    register_candidates,
    suggest_vertical,
)
from xhs_discovery.models.enums import CandidateOrigin
from xhs_discovery.models.source import KeywordText
from xhs_discovery.text.normalize import KeywordNormalizer, normalize_text

from tests.conftest import T0

NORMALIZER = KeywordNormalizer()
DOMAIN = ("建档线", "名额分配", "自招", "中职")
ROOT = Path(__file__).resolve().parents[1]


def child(rank: int, text: str, *, keyword_id: int | None = None):
    return SimpleNamespace(
        rank=rank,
        keyword_id=keyword_id or (100 + rank),
        raw=text,
        normalized=NORMALIZER.normalize(text).normalized,
        is_new_in_run=True,
    )


def make_input(
    text: str = "上海中考",
    *,
    children=(),
    posts=(),
    evidence=(),
    exhausted=(),
    seen=(),
    keyword_id: int = 1,
    depth: int = 0,
) -> ExpansionInput:
    normalized = NORMALIZER.normalize(text)
    return ExpansionInput(
        run_id="run-a",
        keyword_id=keyword_id,
        keyword=KeywordText(raw=text, normalized=normalized.normalized),
        normalized=normalized,
        branch_key="b-root",
        depth=depth,
        children=tuple(children),
        posts=tuple(posts),
        evidence_ids=tuple(evidence),
        exhausted_children=tuple(exhausted),
        seen_normalized=frozenset(seen),
    )


def settings(**overrides) -> OperatorSettings:
    payload = dict(rank_cutoff=3, max_candidates_per_operator=3, domain_terms=DOMAIN)
    payload.update(overrides)
    return OperatorSettings(**payload)


def test_suggest_vertical_keeps_only_high_rank_platform_suggestions():
    context = make_input(
        children=[child(1, "上海中考志愿填报"), child(3, "上海中考分数线"), child(5, "上海中考杂词")],
    )
    proposals = suggest_vertical(context, settings())
    assert [p.text for p in proposals] == ["上海中考志愿填报", "上海中考分数线"]
    assert all(p.origin is CandidateOrigin.observed for p in proposals)
    assert all("第" in p.rationale for p in proposals)


def test_suggest_vertical_skips_self_and_exhausted_children():
    context = make_input(
        keyword_id=7,
        children=[child(1, "上海中考", keyword_id=7), child(2, "上海中考自招"), child(3, "上海中考中职")],
        exhausted=("上海中考自招",),
    )
    proposals = suggest_vertical(context, settings())
    assert [p.text for p in proposals] == ["上海中考中职"]


def test_suggest_vertical_capped_per_operator():
    context = make_input(children=[child(i, f"上海中考子词{i}") for i in range(1, 4)])
    assert len(suggest_vertical(context, settings())) == 3
    assert len(suggest_vertical(context, settings(max_candidates_per_operator=2))) == 2


def test_suggest_vertical_stops_at_the_depth_ceiling():
    """到深度上限就断链：不设上限时平台会把同一个补全逐层续在父词尾部，追到 depth 11 / 28 字。"""
    ceiling = make_input(
        "上海水丰路美食", depth=5, children=[child(1, "上海水丰路美食探店攻略")]
    )
    assert suggest_vertical(ceiling, settings()) == []
    below = make_input(
        "上海水丰路美食", depth=4, children=[child(1, "上海水丰路美食探店攻略")]
    )
    assert [p.text for p in suggest_vertical(below, settings())] == ["上海水丰路美食探店攻略"]
    assert [p.text for p in suggest_vertical(ceiling, settings(max_depth=9))] == [
        "上海水丰路美食探店攻略"
    ]


# 真实退化样本取自 run-20261031T021531Z：4 个失败操作全在这些词上（3 个 NO_SUGGESTIONS + 1 个验证码）
DEGENERATE_QUERIES = (
    "上海水丰路美食探店攻略推荐必吃榜推荐菜推荐菜推荐菜推荐菜",
    "上海水丰路美食打卡攻略推荐必吃榜美食推荐必吃榜推荐必吃榜",
    "上海水丰路美食街攻略必吃榜推荐必吃榜美食推荐必吃榜推荐",
    "上海市杨浦区水丰路美食打卡攻略美食推荐必吃榜推荐菜推荐",
)
# 同一次运行里 depth 4~5 的正常长尾词，最长 21 字，必须原样放行
LIVE_QUERIES = (
    "上海市杨浦区水丰路美食打卡攻略美食推荐必吃",
    "上海水丰路美食探店攻略推荐必吃榜",
    "上海水丰路必吃美食榜单推荐必吃",
    "杨浦区水丰路美食推荐必吃",
)


@pytest.mark.parametrize("text", DEGENERATE_QUERIES)
def test_degenerate_guard_flags_repeated_completion(text):
    assert is_degenerate_query(normalize_text(text), max_repeats=2)


@pytest.mark.parametrize("text", LIVE_QUERIES)
def test_degenerate_guard_keeps_real_long_tail_queries(text):
    assert not is_degenerate_query(normalize_text(text), max_repeats=2)


def test_degenerate_guard_is_threshold_driven():
    """阈值是配置项：0 关闭，调高到不低于实际重复次数就放行——退化判定不该是硬编码的。"""
    text = normalize_text(DEGENERATE_QUERIES[0])  # "推荐" 在该串里出现 5 次
    assert not is_degenerate_query(text, max_repeats=0)
    assert is_degenerate_query(text, max_repeats=4)
    assert not is_degenerate_query(text, max_repeats=5)


def test_expand_drops_degenerate_candidates_and_says_why():
    """退化词不入队，但必须留下拒绝原因：否则"这一支为什么不再长"会和"没采到"混成一回事。"""
    rejected: list[str] = []
    context = make_input(
        "上海水丰路美食探店攻略推荐必吃榜推荐菜推荐菜推荐菜",
        children=[
            child(1, "上海水丰路美食探店攻略推荐必吃榜推荐菜推荐菜推荐菜推荐菜"),
            child(2, "上海水丰路美食探店攻略推荐必吃榜"),
        ],
    )
    candidates = expand(context, settings(), enabled=("suggest_vertical",), rejected=rejected)
    assert [c.keyword.normalized for c in candidates] == ["上海水丰路美食探店攻略推荐必吃榜"]
    assert len(rejected) == 1 and "退化" in rejected[0]


def test_expand_reports_the_depth_ceiling_as_a_note():
    rejected: list[str] = []
    context = make_input(
        "上海水丰路美食", depth=5, children=[child(1, "上海水丰路美食探店攻略")]
    )
    assert expand(context, settings(), enabled=("suggest_vertical",), rejected=rejected) == []
    assert len(rejected) == 1 and "深度上限" in rejected[0]


def test_phrase_fallback_only_when_no_suggestions_left():
    with_children = make_input(
        "上海中考 志愿填报 流程", children=[child(1, "上海中考志愿填报")]
    )
    assert phrase_fallback(with_children, settings()) == []

    drained = make_input(
        "上海中考 志愿填报 流程", children=[child(1, "上海中考志愿填报")], exhausted=("上海中考志愿填报",)
    )
    proposals = phrase_fallback(drained, settings())
    # 由长到短逐个保留完整前缀，短到只剩单字/等于原词的跳过
    assert [p.text for p in proposals] == ["上海中考 志愿填报", "上海中考"]
    assert proposals[0].origin is CandidateOrigin.generated
    assert "耗尽" in proposals[0].rationale

    empty = make_input("上海中考 志愿 排序 方法")
    assert [p.text for p in phrase_fallback(empty, settings())][:2] == ["上海中考 志愿 排序", "上海中考 志愿"]


def test_phrase_fallback_never_leaves_a_half_sentence_or_reuses_seen_text():
    """裁到"不/年/无"这类结构词尾巴会造出另一个事实，必须整词跳过。"""
    tail = make_input("上海2025中考 不要 中职")
    assert [p.text for p in phrase_fallback(tail, settings())] == ["上海2025中考"]

    seen = make_input("上海中考 志愿 排序", seen=("上海中考 志愿",))
    assert [p.text for p in phrase_fallback(seen, settings())] == ["上海中考"]

    single = make_input("上海")
    assert phrase_fallback(single, settings()) == []


def test_content_lateral_needs_observed_titles_and_domain_terms():
    context = make_input(
        "上海中考",
        posts=[SimpleNamespace(title="上海中考建档线附近还有书读吗"), SimpleNamespace(title="中职到底怎么选")],
    )
    proposals = content_lateral(context, settings())
    assert [p.text for p in proposals] == ["上海中考 中职", "上海中考 建档线"]
    assert all(p.origin is CandidateOrigin.generated for p in proposals)
    assert "1 篇" in next(p.rationale for p in proposals if p.text == "上海中考 建档线")

    no_terms = content_lateral(context, settings(domain_terms=()))
    assert no_terms == []
    no_titles = content_lateral(make_input("上海中考"), settings())
    assert no_titles == []


def test_content_lateral_skips_terms_already_inside_the_anchor():
    context = make_input(
        "上海中考建档线", posts=[SimpleNamespace(title="上海中考建档线 是多少")],
    )
    assert content_lateral(context, settings()) == []


def test_problem_probe_requires_source_evidence():
    without = make_input("上海中考")
    assert problem_probe(without, settings()) == []

    with_evidence = make_input("上海中考", evidence=(11, 12))
    proposals = problem_probe(with_evidence, settings())
    assert [p.text for p in proposals] == [
        "上海中考 怎么选",
        "上海中考 需要什么条件",
        "上海中考 有哪些坑",
    ]
    assert all(p.origin is CandidateOrigin.generated for p in proposals)
    assert all(p.source_evidence_ids == (11, 12) for p in proposals)

    capped = problem_probe(make_input("上海中考", evidence=(11,)), settings(max_candidates_per_operator=2))
    assert len(capped) == 2


def test_expand_orders_operators_deterministically_and_dedupes():
    context = make_input(
        "上海中考 志愿 排序",
        children=[child(1, "上海中考 志愿 排序 方法"), child(2, "上海中考 志愿")],
        posts=[SimpleNamespace(title="上海中考建档线怎么查")],
        evidence=(5,),
    )
    candidates = expand(context, settings())
    origins = [candidate.origin.value for candidate in candidates]
    operators = [candidate.operator for candidate in candidates]
    assert operators[:2] == ["suggest_vertical", "suggest_vertical"]
    assert "content_lateral" in operators and "problem_probe" in operators
    # 同一批内按规范化文本去重：联想已给出的"上海中考 志愿"不再由回退算子重复产出
    assert len(set(candidate.keyword.normalized for candidate in candidates)) == len(candidates)
    assert "generated" in origins and "observed" in origins
    assert all(candidate.parents[0].raw == "上海中考 志愿 排序" for candidate in candidates)
    assert all(candidate.depth == 1 and candidate.branch_id == "b-root" for candidate in candidates)
    assert all(candidate.selection_reason for candidate in candidates)


def test_expand_never_proposes_the_parent_itself():
    context = make_input("上海中考", children=[child(1, "上海中考")], evidence=(1,))
    candidates = expand(context, settings())
    assert "上海中考" not in [candidate.keyword.normalized for candidate in candidates]


def test_expand_rejects_unknown_operator_names():
    context = make_input("上海中考")
    with pytest.raises(ValueError, match="未实现的扩展算子"):
        expand(context, settings(), enabled=("suggest_vertical", "llm_rewrite"))


def test_operator_registry_matches_configurable_namespaces():
    """配置里的算子名单必须与注册表一致：拼错的算子名不能静默少跑一类扩展。"""
    default = load_settings(ROOT / "config" / "default.yaml")
    assert set(default.scheduler.operators) == set(OPERATORS)


def test_expand_is_reproducible_without_models_or_randomness():
    context = make_input(
        "上海中考 志愿 排序",
        children=[child(1, "上海中考 志愿 排序 方法")],
        posts=[SimpleNamespace(title="建档线与名额分配")],
        evidence=(9,),
    )
    first = expand(context, settings())
    second = expand(make_input(
        "上海中考 志愿 排序",
        children=[child(1, "上海中考 志愿 排序 方法")],
        posts=[SimpleNamespace(title="建档线与名额分配")],
        evidence=(9,),
    ), settings())
    assert [c.keyword.normalized for c in first] == [c.keyword.normalized for c in second]
    assert [c.origin for c in first] == [c.origin for c in second]


def test_operator_settings_reads_its_thresholds_from_scheduler_config():
    loaded = load_settings(ROOT / "config" / "default.yaml")
    built = OperatorSettings.from_settings(loaded, ("建档线",))
    assert built.domain_terms == ("建档线",)
    assert built.rank_cutoff == 3 and built.max_candidates_per_operator == 3
    assert built.probe_templates and all("{topic}" in t for t in built.probe_templates)
    # 深度与退化阈值必须是配置项，不能是算子里的隐含常量
    assert built.max_depth == loaded.scheduler.max_depth
    assert built.max_segment_repeats == loaded.scheduler.max_segment_repeats


def test_register_candidates_separates_generated_edges_from_observed_ones(storage):
    from xhs_discovery.discovery.graph import DiscoveryGraph

    repo, run_id = storage.repo, storage.run_id
    repo.ensure_branch(run_id, "b-root", label="根分支")
    parent = repo.ensure_keyword("上海中考", "上海中考", seen_at=T0)
    repo.upsert_run_keyword(
        run_id, parent, parent_keyword_id=None, depth=0,
        branch_id=repo.branch_id(run_id, "b-root"), origin="seed", status="queued", seen_at=T0,
    )

    context = make_input("上海中考", children=[child(1, "上海中考志愿填报")], evidence=(3,))
    candidates = expand(context, settings(domain_terms=("建档线",)), enabled=("suggest_vertical", "problem_probe"))
    registered = register_candidates(
        repo, run_id, list(candidates), parent_keyword_id=parent, branch_key="b-root", seen_at=T0
    )
    graph = DiscoveryGraph(repo, run_id)

    observed = [kid for kid, candidate in registered if candidate.origin is CandidateOrigin.observed]
    generated = [kid for kid, candidate in registered if candidate.origin is CandidateOrigin.generated]
    assert observed and generated
    # observed 的 SUGGESTS 边由 ingest 写；算子登记阶段不得替平台造边
    assert [e.child_keyword_id for e in graph.edges(origin=CandidateOrigin.observed)] == []
    assert {e.child_keyword_id for e in graph.edges(origin=CandidateOrigin.generated)} == set(generated)
    assert all(e.session_id is None for e in graph.edges())
    assert all(e.rank is None for e in graph.edges())

    for keyword_id, candidate in registered:
        row = repo.run_keyword(run_id, keyword_id)
        assert row["status"] == "queued"
        assert row["origin"] == candidate.origin.value
        assert row["depth"] == 1
    assert graph.independent_parent_count(generated[0]) == 0


def test_register_candidates_keeps_existing_ownership(storage):
    from xhs_discovery.discovery.graph import DiscoveryGraph

    repo, run_id = storage.repo, storage.run_id
    repo.ensure_branch(run_id, "b-root", label="根分支")
    other = repo.ensure_branch(run_id, "b-other", label="另一分支")
    parent = repo.ensure_keyword("上海中考", "上海中考", seen_at=T0)
    existing = repo.ensure_keyword("上海中考志愿填报", "上海中考志愿填报", seen_at=T0)
    repo.upsert_run_keyword(
        run_id, existing, parent_keyword_id=parent, depth=1, branch_id=other,
        origin="observed", status="explored", seen_at=T0,
    )
    candidate = suggest_vertical(
        make_input("上海中考", children=[child(1, "上海中考志愿填报")]), settings()
    )[0].as_candidate(make_input("上海中考"), )
    registered = register_candidates(
        repo, run_id, [candidate], parent_keyword_id=parent, branch_key="b-root", seen_at=T0
    )
    assert [kid for kid, _ in registered] == [existing]
    row = repo.run_keyword(run_id, existing)
    # 已探索过的词不因再次被提议而重置状态或改归属
    assert row["status"] == "explored" and row["branch_id"] == other
