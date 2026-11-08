"""P0-E-UI-r2：UI 视图模型层（`xhs_discovery.ui.support`）专项验证。

这些函数是页面与已验收应用服务之间的"取数整形"逻辑，不依赖 Streamlit 运行时，因此可离线
直测。测试钉的是任务里明确要求的交付约束，而不是渲染像素：

- 门禁：live 缺任何一项配置都按配置口径拒绝（不是"缺能力"），模型开关仍然一律拒绝，
  两者都不伪装可用；
- 只读固定批次：双图/详情/排行榜从批次产物重建，绝不碰可变 needs 表；
- 分层排行榜（C-17）：分组键=类别+总权重+S/Q/A/F 分量档案，总权重相同但缺失分量不同
  不得混成同一精确名次；显性问题/推断需求分区；无证据/构想/候选不入榜，小样本保留
  provisional 与原始计数；
- 主题/意图过滤经 has_problem / merged_into 边还原问题归属，并按上限截断且给出过滤说明；
- GraphML 节点 id 前缀（kw/need/topic/prob…）与 ref 属性解析、发现路径回溯；
- 选择对象解析（C-16）：以安装版 Streamlit `PydeckSelectionState` 的真实结构
  `objects={layer_id:[metadata]}` 为准，兼容旧 list 形态；清空/无效 ref/跨页残留不串详情；
- 图可用性：布局按画布自动适配为像素坐标，TextLayer 图集字符集自动包含当前中文；
- 后台启动（可审计）：真实子进程验证 创建→完成、rerun 不二次启动、活跃 worker 拒绝、
  失败可读、pause/extend/resume 同一 run 账本连续（隔离 tmp 数据根，不是只 mock Popen）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from xhs_discovery import app as services
from xhs_discovery.config import load_settings
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.repository import Repository
from xhs_discovery.ui import support

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "offline-demo.yaml"


@pytest.fixture
def base_settings():
    return load_settings(CONFIG)


# --------------------------------------------------------------------- 创建页：门禁与派生
def test_derive_settings_refuses_live_without_complete_config(base_settings):
    """live 不再是"缺能力"，但缺配置就是不可用：派生前完整重校验必须挡住。"""
    from xhs_discovery.config import ConfigError

    with pytest.raises(ConfigError) as err:
        support.derive_settings(
            base_settings, mode="live", seed="上海中考",
            budget={}, sampling={}, llm_enabled=False, embedding_enabled=False,
        )
    assert "run.mode=live" in str(err.value)


def test_derive_settings_accepts_live_only_with_complete_fields(tmp_path, base_settings):
    """表单里配齐绝对工具路径/共享锁目录/地域口径后，live 派生成功；工具文件是否存在由创建时的
    来源快照负责核对，不在配置层猜测。"""
    derived = support.derive_settings(
        base_settings, mode="live", seed="上海中考",
        budget={"suggest_attempts": 3, "search_attempts": 3},
        sampling={}, llm_enabled=False, embedding_enabled=False,
        xhs_search={
            "node_path": str(tmp_path / "node.exe"),
            "cli_path": str(tmp_path / "xhs-search.mjs"),
            "profile": "default",
            "browser": "msedge",
            "lock_dir": str(tmp_path / "shared_locks"),
            "locations": ["上海"],
        },
    )
    assert derived.run.mode == "live"
    assert derived.sources.xhs_search.lock_dir == str(tmp_path / "shared_locks")
    readiness = support.live_readiness(derived)
    assert readiness["ready"] is True and readiness["notes"]


def test_live_readiness_reports_incomplete_config_without_raising(tmp_path, base_settings):
    readiness = support.live_readiness(
        base_settings.model_copy(update={"run": base_settings.run.model_copy(update={"mode": "live"})})
    )
    assert readiness["ready"] is False
    assert "sources.xhs_search" in " ".join(readiness["notes"])


def test_derive_settings_rejects_models_on(base_settings):
    for llm, emb in ((True, False), (False, True)):
        with pytest.raises(services.UnsupportedCapabilityError):
            support.derive_settings(
                base_settings, mode="fixture", seed="上海中考",
                budget={}, sampling={}, llm_enabled=llm, embedding_enabled=emb,
            )


def test_derive_settings_applies_budget_and_sampling_overrides(base_settings):
    derived = support.derive_settings(
        base_settings, mode="fixture", seed="上海 分班考",
        budget={"suggest_attempts": 5, "search_attempts": 2},
        sampling={"high_value_posts": 4},
        llm_enabled=False, embedding_enabled=False,
    )
    assert derived.budget.suggest_attempts == 5
    assert derived.budget.search_attempts == 2
    assert derived.sampling.high_value_posts == 4
    assert derived.run.seed_raw == "上海 分班考"


def test_write_derived_config_roundtrips(base_settings, tmp_path: Path):
    from xhs_discovery.paths import RuntimePaths

    paths = RuntimePaths(tmp_path / "data")
    paths.root.mkdir(parents=True, exist_ok=True)
    rel = support.write_derived_config(base_settings, paths, "run-x")
    assert Path(rel).is_file()
    again = load_settings(Path(rel))
    assert again.budget.suggest_attempts == base_settings.budget.suggest_attempts


# --------------------------------------------------------------------- 排行榜口径
def _need(**overrides) -> dict:
    row = {
        "need_id": "1", "canonical_text": "怎么排志愿", "statement_kind": "explicit_problem",
        "evidence_status": "verified", "displayable": "1", "score": "0.5",
        "effective_weight_total": "0.7", "provisional": "1", "provisional_reason": "样本不足",
        "evidence_rows": "2", "distinct_posts": "2", "source_keywords": "上海中考",
        "unknown_components": "",
    }
    row.update(overrides)
    return row


def test_leaderboard_excludes_no_evidence_and_candidates():
    needs = [
        _need(need_id="1"),
        _need(need_id="2", evidence_rows="0"),              # 无证据 → 不入榜
        _need(need_id="3", statement_kind="solution_idea"),  # 构想 → 不入榜
        _need(need_id="4", statement_kind="search_intent_candidate"),
        _need(need_id="5", evidence_status="unverified_candidate"),  # 未验证 → 不入榜
        _need(need_id="6", score=""),                        # 无分 → 不入榜
    ]
    board = support.leaderboard(needs)
    assert [b["need_id"] for b in board] == ["1"]


def test_leaderboard_groups_by_effective_weight_not_mixed_rank():
    needs = [
        _need(need_id="a", effective_weight_total="0.7", score="0.40"),
        _need(need_id="b", effective_weight_total="0.7", score="0.55"),
        _need(need_id="c", effective_weight_total="0.55", score="0.90"),
    ]
    board = support.leaderboard(needs)
    order = [b["need_id"] for b in board]
    # 权重组 0.7 名次靠前（内含更高分）；组内按分数降序 b>a；不同权重组不共名次
    assert order == ["b", "a", "c"]
    groups = {b["need_id"]: (b["weight_group_rank"], b["group_size"]) for b in board}
    assert groups["b"] == (1, 2) and groups["a"] == (1, 2)
    assert groups["c"] == (2, 1)


def test_leaderboard_keeps_provisional_and_raw_counts():
    board = support.leaderboard([_need(need_id="p", provisional="1", provisional_reason="小样本")])
    assert board[0]["provisional"] == "1"
    assert board[0]["provisional_reason"] == "小样本"
    assert board[0]["distinct_posts"] == "2" and board[0]["evidence_rows"] == "2"


# --------------------------------------------------------- C-17：分量档案分组
PROFILE_COMMON = dict(
    displayable="1", score="0.8", evidence_status="verified", statement_kind="explicit_problem",
    evidence_rows="1", effective_weight_total="0.825", provisional="1",
    provisional_reason="小样本", S_weight="0.35", Q_weight="0.3", A_weight="0.175", F_weight="0.175",
    S_normalized="0.8", Q_normalized="0.8",
)


def _profile_need(need_id: str, **overrides) -> dict:
    return dict(PROFILE_COMMON, need_id=need_id, canonical_text=f"need {need_id}", **overrides)


def test_leaderboard_same_total_different_missing_component_are_split():
    """独立复核探针（C-17）：A/F 权重各 0.175、总 0.825，一条缺 A、一条缺 F，
    只按 effective_weight_total 分组会把不同证据完整度混进同一精确名次——必须分开。"""
    a = _profile_need("a", unknown_components="A", A_normalized="", F_normalized="0.8",
                      A_missing_reason="无同时性证据")
    b = _profile_need("b", unknown_components="F", A_normalized="0.8", F_missing_reason="无时间戳")
    board = support.leaderboard([a, b])
    ranks = {item["need_id"]: item["weight_group_rank"] for item in board}
    assert ranks["a"] != ranks["b"], f"缺失分量不同不得共组：{ranks}"
    by_id = {item["need_id"]: item for item in board}
    assert by_id["a"]["missing_reasons"].get("A") == "无同时性证据"
    assert by_id["b"]["missing_reasons"].get("F") == "无时间戳"
    assert by_id["a"]["unknown_components"] == "A" and by_id["b"]["unknown_components"] == "F"
    assert by_id["a"]["provisional"] == "1" and by_id["a"]["provisional_reason"] == "小样本"


def test_leaderboard_identical_profile_shares_group_and_orders_by_score():
    a = _profile_need("a", score="0.6")
    b = _profile_need("b", score="0.9")
    board = support.leaderboard([a, b])
    assert [item["need_id"] for item in board] == ["b", "a"]
    assert board[0]["weight_group_rank"] == board[1]["weight_group_rank"]
    assert board[0]["group_size"] == 2 and [board[0]["rank_in_group"], board[1]["rank_in_group"]] == [1, 2]


def test_leaderboard_explicit_and_inferred_never_share_group():
    """显性问题/推断需求保持明确分区：档案与总权重完全相同也不共精确名次组。"""
    explicit = _profile_need("x")
    inferred = _profile_need("y", statement_kind="inferred_need")
    board = support.leaderboard([explicit, inferred])
    ranks = {item["need_id"]: item["weight_group_rank"] for item in board}
    assert ranks["x"] != ranks["y"]
    kinds = {item["group_statement_kind"] for item in board}
    assert kinds == {"explicit_problem", "inferred_need"}


def test_leaderboard_group_order_stable_and_weight_dominant():
    low = _profile_need("l", score="0.95", effective_weight_total="0.65")
    high = _profile_need("h", score="0.5")
    board = support.leaderboard([low, high])
    assert [item["need_id"] for item in board] == ["h", "l"], "高权重档案组先于低权重组，即使分数更低"


# --------------------------------------------------------------------- GraphML / 分图 / 过滤
GRAPHML = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="k_ref" for="node" attr.name="ref" attr.type="string"/>
  <key id="k_kind" for="node" attr.name="kind" attr.type="string"/>
  <key id="k_label" for="node" attr.name="label" attr.type="string"/>
  <key id="k_intent" for="node" attr.name="intent" attr.type="string"/>
  <key id="e_rel" for="edge" attr.name="relation" attr.type="string"/>
  <key id="e_src" for="edge" attr.name="source_ref" attr.type="string"/>
  <key id="e_tgt" for="edge" attr.name="target_ref" attr.type="string"/>
  <graph id="G" edgedefault="directed">
    <node id="kw_0"><data key="k_ref">kw:1</data><data key="k_kind">keyword</data><data key="k_label">上海中考</data></node>
    <node id="kw_1"><data key="k_ref">kw:2</data><data key="k_kind">keyword</data><data key="k_label">上海中考 志愿</data></node>
    <node id="need_0"><data key="k_ref">need:5</data><data key="k_kind">need</data><data key="k_label">怎么排志愿</data></node>
    <node id="prob_0"><data key="k_ref">prob:7</data><data key="k_kind">problem</data><data key="k_label">不会排志愿</data><data key="k_intent">流程疑问</data></node>
    <node id="topic_0"><data key="k_ref">topic:志愿</data><data key="k_kind">topic</data><data key="k_label">志愿</data></node>
    <edge id="e0" source="kw_0" target="kw_1"><data key="e_rel">suggests</data><data key="e_src">kw:1</data><data key="e_tgt">kw:2</data></edge>
    <edge id="e1" source="prob_0" target="need_0"><data key="e_rel">merged_into</data><data key="e_src">prob:7</data><data key="e_tgt">need:5</data></edge>
    <edge id="e2" source="topic_0" target="prob_0"><data key="e_rel">has_problem</data><data key="e_src">topic:志愿</data><data key="e_tgt">prob:7</data></edge>
  </graph>
</graphml>
"""


@pytest.fixture
def parsed(tmp_path: Path):
    target = tmp_path / "graph.graphml"
    target.write_text(GRAPHML, encoding="utf-8")
    nodes, edges = support.parse_graphml(str(target))
    return nodes, edges


def test_parse_graphml_reads_ref_kind_and_edges(parsed):
    nodes, edges = parsed
    refs = {n.get("ref") for n in nodes}
    assert refs == {"kw:1", "kw:2", "need:5", "prob:7", "topic:志愿"}
    relations = {e.get("relation") for e in edges}
    assert relations == {"suggests", "merged_into", "has_problem"}


def test_split_graphs_separates_discovery_and_demand(parsed):
    nodes, edges = parsed
    discovery, demand = support.split_graphs(nodes, edges)
    assert {n["ref"] for n in discovery["nodes"]} == {"kw:1", "kw:2"}
    assert [e["relation"] for e in discovery["edges"]] == ["suggests"]
    assert {n["ref"] for n in demand["nodes"]} == {"need:5", "prob:7", "topic:志愿"}


def test_filter_demand_topic_and_intent(parsed):
    nodes, edges = parsed
    _, demand = support.split_graphs(nodes, edges)
    needs = [_need(need_id="5")]

    kept, kept_edges, note = support.filter_demand(
        demand["nodes"], demand["edges"], needs, topics=["topic:志愿"], intents=["流程疑问"], max_nodes=100
    )
    assert any(n["ref"] == "need:5" for n in kept)
    assert note["filtered_topic"] == 0 and note["filtered_intent"] == 0

    _, _, note_drop = support.filter_demand(
        demand["nodes"], demand["edges"], needs, topics=["topic:别的"], intents=[], max_nodes=100
    )
    assert note_drop["filtered_topic"] == 1

    _, _, note_intent = support.filter_demand(
        demand["nodes"], demand["edges"], needs, topics=[], intents=["无关意图"], max_nodes=100
    )
    assert note_intent["filtered_intent"] == 1


def test_filter_demand_caps_and_reports(parsed):
    nodes, edges = parsed
    _, demand = support.split_graphs(nodes, edges)
    kept, _, note = support.filter_demand(demand["nodes"], demand["edges"], [], topics=[], intents=[], max_nodes=1)
    assert note["capped"] is True and len(kept) <= 1
    assert note["max_nodes"] == 1 and note["total_nodes"] == len(demand["nodes"])


# --------------------------------------------------------------------- 布局 / 路径 / 选择
def test_layout_positions_deterministic_and_edgeless_safe(parsed):
    nodes, _ = parsed
    discovery, _ = support.split_graphs(nodes, [])
    pos_a = support.layout_positions(discovery["nodes"], [], seed=7)
    pos_b = support.layout_positions(discovery["nodes"], [], seed=7)
    assert pos_a == pos_b
    assert set(pos_a) == {"kw:1", "kw:2"}


def test_keyword_path_walks_parent_chain():
    keywords = [
        {"keyword_id": "1", "normalized": "上海中考", "parent_keyword_id": ""},
        {"keyword_id": "2", "normalized": "上海中考 志愿", "parent_keyword_id": "1"},
        {"keyword_id": "3", "normalized": "上海中考 志愿 排名", "parent_keyword_id": "2"},
    ]
    assert support.keyword_path(keywords, "3") == ["上海中考", "上海中考 志愿", "上海中考 志愿 排名"]
    assert support.keyword_path(keywords, "1") == ["上海中考"]


def test_extract_selection_ids_defensive():
    class FakeSel:
        objects = [{"feature": {"id": "need:5"}}, {"id": "kw:2"}, {"id": "no-colon"}]

    class Holder:
        selection = FakeSel()

    assert support.extract_selection_ids(Holder()) == ["need:5", "kw:2"]
    assert support.extract_selection_ids(None) == []
    assert support.extract_selection_ids(object()) == []


# --------------------------------------------------------- C-16：真实 Streamlit 选择结构
def _state_with(objects):
    from types import SimpleNamespace

    return SimpleNamespace(selection={"indices": {"nodes": [0]}, "objects": objects})


def test_extract_selection_ids_real_layer_dict_shape():
    """安装版 Streamlit 的 PydeckSelectionState.objects 是 dict[layer_id, list[metadata]]；
    迭代 dict 键只会拿到层名，必须展开每层的 metadata 列表取节点 id。"""
    state = _state_with({"nodes": [{"id": "kw:1", "label": "上海中考"}]})
    assert support.extract_selection_ids(state) == ["kw:1"]
    multi = _state_with({"nodes": [{"id": "need:5"}, {"id": "kw:1"}], "labels": [{"id": "topic:志愿"}]})
    assert support.extract_selection_ids(multi) == ["need:5", "kw:1", "topic:志愿"]


def test_extract_selection_ids_clear_and_invalid_shapes():
    assert support.extract_selection_ids(_state_with({})) == []          # 清空选择
    assert support.extract_selection_ids(_state_with({"nodes": []})) == []
    assert support.extract_selection_ids(_state_with(None)) == []
    assert support.extract_selection_ids(_state_with({"nodes": "not-a-list"})) == []
    assert support.extract_selection_ids(_state_with({"nodes": ["kw:1", 42, None]})) == []
    assert support.extract_selection_ids(_state_with({"nodes": [{"id": ""}, {"id": "nocolon"},
                                                                 {"ref": "prob:7"}]})) == ["prob:7"]


def test_extract_selection_ids_dedupes_and_ignores_nonselection_state():
    state = _state_with({"nodes": [{"id": "kw:1"}], "dup": [{"id": "kw:1"}]})
    assert support.extract_selection_ids(state) == ["kw:1"]
    assert support.extract_selection_ids(type("S", (), {"selection": None})()) == []


def test_valid_selection_refs_only_current_graph_nodes_cross_page_isolated():
    """点选详情只认当前图节点集合内的 ref：另一页/另一 run 的选择残留一律丢弃。"""
    state = _state_with({"nodes": [{"id": "kw:1"}, {"id": "kw:999"}]})
    discovery_visible = {"kw:1", "kw:2"}
    demand_visible = {"need:5", "prob:7"}
    assert support.valid_selection_refs(state, discovery_visible) == ["kw:1"]
    assert support.valid_selection_refs(state, demand_visible) == []


# --------------------------------------------------------- 图可用性：画布适配 / 中文字符集 / 序列化配置
def test_fit_positions_to_canvas_spreads_points_across_canvas():
    """spring_layout 的 scale=6 输出必须等比适配到画布像素：35 点不再挤在约 30px 内。"""
    import math

    positions = {f"n{i}": [math.cos(i), math.sin(i) * 2] for i in range(35)}
    fitted = support.fit_positions_to_canvas(positions, width=900, height=600, margin=60)
    xs = [p[0] for p in fitted.values()]
    ys = [p[1] for p in fitted.values()]
    assert min(xs) >= 60 - 1e-6 and max(xs) <= 840 + 1e-6
    assert min(ys) >= 60 - 1e-6 and max(ys) <= 540 + 1e-6
    # 适配后图幅至少占可用区一个方向撑满（等比缩放），点间距是数据坐标的几十倍
    spread_x = max(xs) - min(xs)
    assert spread_x >= 780 * 0.99 - 1e-6 or (max(ys) - min(ys)) >= 480 * 0.99 - 1e-6
    # 等比：任意两点距离 = 原距离 × 同一比例（不发生拉伸变形）
    scale = spread_x / (max(positions["n0"][0] for _ in [0]) - min(p[0] for p in positions.values()))
    d_src = math.dist(positions["n0"], positions["n1"])
    assert math.dist(fitted["n0"], fitted["n1"]) == pytest.approx(d_src * scale)


def test_fit_positions_single_point_and_empty():
    assert support.fit_positions_to_canvas({}) == {}
    one = support.fit_positions_to_canvas({"only": [3.3, -1.2]})
    assert one["only"] == pytest.approx([450.0, 300.0])  # 退化范围：居中


def test_label_character_set_includes_cjk_and_dedupes():
    chars = support.label_character_set(["上海中考", "how to", "上海"])
    assert {"上", "海", "中", "考"} <= set(chars)
    assert chars == sorted(chars)
    assert " " not in chars


def test_build_deck_serialization_pixels_zoom_and_atlas_no_cdn():
    nodes = [
        {"ref": "kw:1", "kind": "keyword", "label": "上海中考"},
        {"ref": "kw:2", "kind": "topic", "label": "中考 志愿"},
    ]
    edges = [{"source_ref": "kw:1", "target_ref": "kw:2", "relation": "suggests"}]
    positions = support.fit_positions_to_canvas({"kw:1": [-6.0, -6.0], "kw:2": [6.0, 6.0]})
    deck = support.build_deck(nodes, edges, positions, {"keyword": [214, 96, 60], "topic": [90, 130, 210]})
    text = deck.to_json()
    payload = json.loads(text)
    layers = {layer["id"]: layer for layer in payload["layers"]}
    # 初始视野：像素坐标 + zoom=0（OrthographicView 下 1 单位=1px），不需要用户先放大
    assert payload["initialViewState"]["zoom"] == 0
    # 节点半径为像素值（不是 1.4 这类世界单位）；pydeck 把字符串访问器序列化为 "@@=字段名"
    assert support.RADIUS_PX["keyword"] >= 8 and support.DEFAULT_RADIUS_PX >= 8
    assert layers["nodes"]["getRadius"].endswith("radius")
    assert layers["nodes"]["pickable"] is True
    # TextLayer 图集字符集：必须是浏览器能直接解释的字面量 JSON 数组，而不是 pydeck
    # 把 list[str] 编译成的 "@@=[上, 海]" 函数表达式（r2 复核：TextLayer 抛
    # "function is not iterable"，中文标签整层消失）。
    charset = layers["labels"]["characterSet"]
    assert isinstance(charset, list) and charset, "characterSet 需随图层携带且非空"
    assert all(isinstance(ch, str) and ch for ch in charset)
    assert "上" in charset and "中" in charset
    assert not any("@@=" in str(ch) for ch in charset)
    assert '"characterSet": "@@=' not in text
    assert layers["labels"]["characterSet"] == support.label_character_set(["上海中考", "中考 志愿"])
    # 不引入 CDN / 地图 token / 外部瓦片
    lowered = text.lower()
    for needle in ("http://", "https://", "mapbox", "api_key", "token"):
        assert needle not in lowered, f"图配置不得引入外部依赖：{needle}"
    assert payload["views"][0]["@@type"] == "OrthographicView"


def test_need_detail_traces_evidence_and_source_keyword():
    dataset = {
        "needs": [_need(need_id="5", source_keywords="上海中考")],
        "evidence": [
            {"evidence_id": "11", "need_ids": "5", "quote": "志愿到底怎么排", "source_field": "title",
             "locatable": "1", "synthetic": "1", "note_id": "syn-note-1", "keyword_normalized": "上海中考"},
            {"evidence_id": "12", "need_ids": "9", "quote": "无关", "source_field": "title",
             "locatable": "1", "synthetic": "1", "note_id": "x", "keyword_normalized": "y"},
        ],
        "keywords": [
            {"keyword_id": "1", "normalized": "上海中考", "parent_keyword_id": ""},
        ],
    }
    detail = support.need_detail(dataset, "5")
    assert detail is not None
    assert [e["evidence_id"] for e in detail["evidence"]] == ["11"]
    assert detail["discovery_paths"].get("上海中考") == ["上海中考"]
    assert support.need_detail(dataset, "404") is None


# =========================================================================
# 后台启动（可审计）：真实子进程、隔离 tmp 数据根；不是只 mock Popen。
# =========================================================================

DEMO_YAML = ROOT / "config" / "offline-demo.yaml"


def _ui_sandbox(target: Path) -> Path:
    """临时项目根：只复制配置与合成数据集，数据根落在 tmp 下，绝不碰用户运行数据库。"""
    (target / "config").mkdir(parents=True, exist_ok=True)
    (target / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
    for rel in ("config/domain_terms.txt", "config/intent_rules.yaml"):
        shutil.copyfile(ROOT / rel, target / rel)
    shutil.copyfile(
        ROOT / "tests" / "fixtures" / "shanghai_zhongkao.json",
        target / "tests" / "fixtures" / "shanghai_zhongkao.json",
    )
    demo = DEMO_YAML.read_text(encoding="utf-8")
    tiny = (
        demo.replace("suggest_attempts: 14", "suggest_attempts: 4")
        .replace("search_attempts: 6", "search_attempts: 2")
        .replace("calibration_suggest_attempts: 3", "calibration_suggest_attempts: 1")
    )
    (target / "config" / "tiny.yaml").write_text(tiny, encoding="utf-8")
    (target / "config" / "full.yaml").write_text(demo, encoding="utf-8")
    return target


def _sandbox_conn(sandbox: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sandbox / "data" / "xhs_discovery.db")
    conn.row_factory = sqlite3.Row
    return conn


def _run_status(sandbox: Path, run_id: str) -> str:
    row = _sandbox_conn(sandbox).execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    return str(row["status"]) if row else ""


def _wait_launch(paths: RuntimePaths, run_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = support.worker_launch_status(paths, run_id)
        if status["state"] in ("exited", "failed"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"后台进程未在 {timeout}s 内退出：{status}")


@pytest.fixture
def ui_launch_sandbox(tmp_path_factory) -> Path:
    return _ui_sandbox(tmp_path_factory.mktemp("eui_r2_sandbox"))


# ---------------------------------------------------------- 记录与真实完成/失败可读
def test_launch_worker_records_pid_and_completes(ui_launch_sandbox: Path):
    paths = RuntimePaths(ui_launch_sandbox / "data")
    paths.root.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-c", "print('worker-alive-中文输出')"]
    record = support.launch_worker(command, paths, "r2-print", cwd=ui_launch_sandbox)
    assert record["launch_ok"] and record["pid"] > 0
    assert support.read_launch_record(paths, "r2-print")["pid"] == record["pid"]
    done = _wait_launch(paths, "r2-print", timeout=60)
    assert done["state"] == "exited" and done["exit_code"] == 0
    assert "worker-alive-中文输出" in (paths.root / support.LAUNCH_DIRNAME / "r2-print.out.log").read_text(encoding="utf-8")


def test_launch_worker_failure_is_readable(ui_launch_sandbox: Path):
    paths = RuntimePaths(ui_launch_sandbox / "data")
    command = [sys.executable, "-c", "import sys; sys.stderr.write('模拟启动后失败\\n'); sys.exit(3)"]
    record = support.launch_worker(command, paths, "r2-fail", cwd=ui_launch_sandbox)
    assert record["launch_ok"]
    done = _wait_launch(paths, "r2-fail", timeout=60)
    assert done["state"] == "exited" and done["exit_code"] == 3
    excerpt = support.launch_log_excerpt(paths, "r2-fail")
    assert "模拟启动后失败" in excerpt, "子进程报错必须能读回页面展示"


def test_launch_worker_command_not_found_records_create_error():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        paths = RuntimePaths(Path(tmp) / "data")
        paths.root.mkdir(parents=True, exist_ok=True)
        bogus = str(Path(tmp) / "no-such-executable.exe")
        record = support.launch_worker([bogus, "run"], paths, "r2-nocreate", cwd=tmp)
        assert not record["launch_ok"] and record["create_error"]
        status = support.worker_launch_status(paths, "r2-nocreate")
        assert status["state"] == "failed" and "进程创建失败" in status["detail"]


# ---------------------------------------------------------- 互斥：活跃 worker 拒绝 + rerun 不二次启动
def test_plan_worker_launch_rejects_while_worker_lock_held(tmp_path: Path):
    """真实 OS 锁证据（活 pid 的 run 锁 + 全局锁）在场时，启动决策必须拒绝，
    且这不是靠 session_state 兜底。"""
    paths = RuntimePaths(tmp_path / "data")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.run_dir("r2-occupied").mkdir(parents=True, exist_ok=True)
    payload = {"run_id": "r2-occupied", "pid": os.getpid(), "token": "holder-token"}
    (paths.run_dir("r2-occupied") / "worker.lock").write_text(json.dumps(payload), encoding="utf-8")
    (paths.global_lock_path).write_text(json.dumps(payload), encoding="utf-8")

    conn = storage_db.connect(paths.db_path)
    try:
        Repository(conn)  # 建表：status() 要查 worker_leases
        occupied, detail = support.worker_occupied(conn, paths, "r2-occupied")
        assert occupied and "pid" in detail
        result = support.plan_worker_launch(
            conn, paths, "r2-occupied", [sys.executable, "-c", "print('must-not-run')"],
            session_launched=set(), cwd=tmp_path,
        )
        assert result["launched"] is False and result["reason"] == "occupied"
        assert not (paths.root / support.LAUNCH_DIRNAME / "r2-occupied.json").exists(), \
            "被拒绝的启动不得创建进程或登记"
    finally:
        conn.close()


def _migrated_conn(paths: RuntimePaths) -> sqlite3.Connection:
    """确保数据根库表存在后返回连接（worker_occupied 要查 worker_leases）。"""
    paths.root.mkdir(parents=True, exist_ok=True)
    conn = storage_db.connect(paths.db_path)
    Repository(conn)
    return conn


def test_plan_worker_launch_rerun_does_not_relaunch(ui_launch_sandbox: Path):
    paths = RuntimePaths(ui_launch_sandbox / "data")
    session: set[str] = set()
    command = [sys.executable, "-c", "import time; time.sleep(2)"]
    conn = _migrated_conn(paths)
    try:
        first = support.plan_worker_launch(conn, paths, "r2-rerun", command,
                                           session_launched=session, cwd=ui_launch_sandbox)
        assert first["launched"] is True
        running = support.worker_launch_status(paths, "r2-rerun")
        assert running["state"] == "running", "Popen 创建≠运行确认，此处用真实 poll 证明仍在跑"
        again = support.plan_worker_launch(conn, paths, "r2-rerun", command,
                                          session_launched=session, cwd=ui_launch_sandbox)
        assert again["launched"] is False and again["reason"] == "session"
        assert support.read_launch_record(paths, "r2-rerun")["pid"] == first["record"]["pid"], \
            "rerun 后不得有第二个进程"
    finally:
        conn.close()
    _wait_launch(paths, "r2-rerun", timeout=60)


def test_plan_worker_launch_allows_next_launch_after_previous_exited(ui_launch_sandbox: Path):
    """r3 复核项 3：session_launched 不得按 run_id 永久拦截合法的新启动。

    同一个 session 集合、同一个 run：前一个进程确认退出后，必须能再次发起启动
    （真实"追加额度→恢复、再追加→再恢复"的前提）。判定依据是进程状态，不是集合成员。
    """
    paths = RuntimePaths(ui_launch_sandbox / "data")
    conn = _migrated_conn(paths)
    session: set[str] = set()
    command = [sys.executable, "-c", "import time; time.sleep(0.2)"]
    try:
        first = support.plan_worker_launch(conn, paths, "r3-relaunch", command,
                                           session_launched=session, cwd=ui_launch_sandbox)
        assert first["launched"] is True
        assert "r3-relaunch" in session
        _wait_launch(paths, "r3-relaunch", timeout=60)
        second = support.plan_worker_launch(conn, paths, "r3-relaunch", command,
                                            session_launched=session, cwd=ui_launch_sandbox)
        assert second["launched"] is True, f"进程已退出仍被拦：{second['detail']}"
        assert second["record"]["pid"] != first["record"]["pid"]
    finally:
        conn.close()
    _wait_launch(paths, "r3-relaunch", timeout=60)


def test_plan_worker_launch_refuses_while_recorded_pid_alive(ui_launch_sandbox: Path):
    """本进程没有 Popen 句柄（服务重启等）时，磁盘登记里的 pid 仍活 → 拒绝第二个。"""
    paths = RuntimePaths(ui_launch_sandbox / "data")
    run_id = "r3-record-live"
    conn = _migrated_conn(paths)
    try:
        launch_dir = paths.root / support.LAUNCH_DIRNAME
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "pid": os.getpid(), "launch_ok": True,
                        "command": [], "cwd": str(ui_launch_sandbox)}),
            encoding="utf-8",
        )
        state, detail = support.last_launch_process_state(paths, run_id)
        assert state == "live", detail
        outcome = support.plan_worker_launch(
            conn, paths, run_id, [sys.executable, "-c", "print('must-not-run')"],
            session_launched=set(), cwd=ui_launch_sandbox)
        assert outcome["launched"] is False and outcome["reason"] == "previous_process"
        assert support.read_launch_record(paths, run_id)["pid"] == os.getpid(), "被拒的启动不得覆盖登记"
    finally:
        conn.close()


def test_plan_worker_launch_refuses_unknown_process_state(ui_launch_sandbox: Path, monkeypatch):
    """进程状态查不到（unknown）一律拒绝，绝不当作已退出放行第二个 worker。"""
    import xhs_discovery.storage.worker as worker_module

    paths = RuntimePaths(ui_launch_sandbox / "data")
    run_id = "r3-record-unknown"
    launch_dir = paths.root / support.LAUNCH_DIRNAME
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "pid": 424242, "launch_ok": True,
                    "command": [], "cwd": str(ui_launch_sandbox)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(worker_module, "probe_pid", lambda pid: "unknown")
    conn = _migrated_conn(paths)
    try:
        state, detail = support.last_launch_process_state(paths, run_id)
        assert state == "unknown", detail
        outcome = support.plan_worker_launch(
            conn, paths, run_id, [sys.executable, "-c", "print('must-not-run')"],
            session_launched=set(), cwd=ui_launch_sandbox)
        assert outcome["launched"] is False and outcome["reason"] == "previous_process"
    finally:
        conn.close()


# ---------------------------------------------------------- 真实 UI 创建 → 后台完成 → 同一 run 控制
def test_real_background_run_completes_and_is_readable(ui_launch_sandbox: Path):
    """经启动服务真实跑一个 fixture run 到完成，页面侧数据链（固定批次→数据集）可反读。

    覆盖 r1 复核缺口："已后台启动 worker" 不再是 Popen 返回即宣称，而是有登记、
    有退出码回报、产物可核对。
    """
    paths = RuntimePaths(ui_launch_sandbox / "data")
    command = [
        sys.executable, "-m", "xhs_discovery", "run", "--mode", "fixture",
        "--seed", "上海中考", "--config", "config/tiny.yaml", "--run", "r2-bg-run",
    ]
    conn = _migrated_conn(paths)
    try:
        outcome = support.plan_worker_launch(
            conn, paths, "r2-bg-run", command, session_launched=set(), cwd=ui_launch_sandbox)
    finally:
        conn.close()
    assert outcome["launched"] is True
    done = _wait_launch(paths, "r2-bg-run", timeout=240)
    assert done["state"] == "exited" and done["exit_code"] == 0, support.launch_log_excerpt(paths, "r2-bg-run")
    assert _run_status(ui_launch_sandbox, "r2-bg-run") in ("paused", "completed")

    batches = [b for b in services.completed_batches(_repo(ui_launch_sandbox), "r2-bg-run")
               if b["verification_ok"]]
    assert batches, "后台完成后应存在通过完整性校验的固定批次"
    dataset = support.load_batch_dataset(paths, "r2-bg-run", batches[-1]["snapshot"])
    assert dataset["needs"], "needs.csv 应可反读"
    assert dataset["verification"]["ok"] is True


def _repo(sandbox: Path) -> Repository:
    conn = storage_db.connect(sandbox / "data" / "xhs_discovery.db")
    return Repository(conn, migrate=False)


def test_pause_extend_resume_same_run_via_launcher(ui_launch_sandbox: Path):
    """pause→extend→resume 全部作用于同一 run：账本连续（只增不清零）、批次追加、
    resume 由启动服务后台发起并经 WorkerControl 判定互斥。

    使用 r2-bg-run（tiny 预算：探索到预算耗尽即 paused）作为被控 run。
    """
    paths = RuntimePaths(ui_launch_sandbox / "data")
    run_id = "r2-bg-run"
    services.execute_run(str(ui_launch_sandbox / "config" / "tiny.yaml"),
                         ui_launch_sandbox, mode="fixture", seed="上海中考", run_id=run_id)
    conn = _migrated_conn(paths)
    try:
        status_before = support.worker_occupied(conn, paths, run_id)
        assert status_before[0] is False, "初始化任务已完成并放锁"

        run = _repo(ui_launch_sandbox).get_run(run_id)
        assert run is not None and str(run["status"]) == "paused", \
            f"tiny 预算应停在可恢复的 paused：{run and run['status']}"
        used_before = _sandbox_conn(ui_launch_sandbox).execute(
            "SELECT COALESCE(SUM(consumed_units),0) AS t FROM budget_ledger"
            " WHERE run_id = ? AND budget_key = 'suggest_attempts'", (run_id,)
        ).fetchone()["t"]
        batches_before = len([b for b in services.completed_batches(_repo(ui_launch_sandbox), run_id)
                              if b["verification_ok"]])

        # extend 只追加额度；resume 走后台启动服务（与页面同一代码路径）
        services.extend_run(str(ui_launch_sandbox / "config" / "tiny.yaml"), ui_launch_sandbox,
                            run_id, suggest_budget=2, note="r2 测试追加")
        command = [
            sys.executable, "-m", "xhs_discovery", "resume",
            "--run", run_id, "--config", "config/tiny.yaml", "--max-slots", "1",
        ]
        outcome = support.plan_worker_launch(
            conn, paths, run_id, command, session_launched=set(), cwd=ui_launch_sandbox)
        assert outcome["launched"] is True
    finally:
        conn.close()
    done = _wait_launch(paths, run_id, timeout=240)
    assert done["state"] == "exited" and done["exit_code"] == 0, support.launch_log_excerpt(paths, run_id)

    db_conn = _sandbox_conn(ui_launch_sandbox)
    revisions = [json.loads(str(r["added_json"]))
                 for r in db_conn.execute(
                     "SELECT added_json FROM budget_revisions WHERE run_id = ?", (run_id,)).fetchall()]
    assert revisions and revisions[-1]["suggest_attempts"] == 2.0, "extend 必须进同一 run 的账本修订"
    used_after = db_conn.execute(
        "SELECT COALESCE(SUM(consumed_units),0) AS t FROM budget_ledger"
        " WHERE run_id = ? AND budget_key = 'suggest_attempts'", (run_id,)
    ).fetchone()["t"]
    assert float(used_after) >= float(used_before), "同一账本继续累加，不清零"
    applied = db_conn.execute(
        "SELECT COUNT(*) AS c FROM control_requests WHERE run_id = ? AND action='extend'"
        " AND applied_at IS NOT NULL", (run_id,)
    ).fetchone()["c"]
    assert applied == 1
    batches_after = len([b for b in services.completed_batches(_repo(ui_launch_sandbox), run_id)
                         if b["verification_ok"]])
    assert batches_after >= batches_before, "恢复后新观察形成新批次，旧批次保留"


def test_pause_consumed_at_safe_point_by_real_background_worker(ui_launch_sandbox: Path):
    """真实并发：后台 worker 运行中，页面侧 pause 请求由 worker 在安全点消费（不改历史算法）。

    用 full 预算起一个新 run，等它拿锁进入 running 后立即 pause；随后拒绝第二次启动
    （互斥在 worker 进程内由 OS 锁保证，页面判定用 worker_occupied）。
    """
    paths = RuntimePaths(ui_launch_sandbox / "data")
    run_id = "r2-pause-run"
    command = [
        sys.executable, "-m", "xhs_discovery", "run", "--mode", "fixture",
        "--seed", "上海 分班考", "--config", "config/full.yaml", "--run", run_id,
    ]
    conn = _migrated_conn(paths)
    try:
        outcome = support.plan_worker_launch(
            conn, paths, run_id, command, session_launched=set(), cwd=ui_launch_sandbox)
        assert outcome["launched"] is True
        lock = paths.run_dir(run_id) / "worker.lock"
        deadline = time.time() + 90
        while not lock.exists():
            if time.time() > deadline:
                raise AssertionError(f"worker 未能在 90s 内持锁：{support.launch_log_excerpt(paths, run_id)}")
            time.sleep(0.05)
        occupied, detail = support.worker_occupied(conn, paths, run_id)
        assert occupied, f"运行中的 worker 应被判占用：{detail}"
        refused = support.plan_worker_launch(
            conn, paths, run_id, command, session_launched=set(), cwd=ui_launch_sandbox)
        assert refused["launched"] is False and refused["reason"] == "occupied", \
            "活跃 worker 在场时第二个启动必须被拒绝（不靠 session_state）"

        services.pause_run(str(ui_launch_sandbox / "config" / "full.yaml"), ui_launch_sandbox,
                           run_id, note="r2 测试暂停")
    finally:
        conn.close()
    done = _wait_launch(paths, run_id, timeout=240)
    assert done["state"] == "exited" and done["exit_code"] == 0, support.launch_log_excerpt(paths, run_id)
    assert _run_status(ui_launch_sandbox, run_id) == "paused", "暂停须由 worker 在安全点应用后落库"
    row = _sandbox_conn(ui_launch_sandbox).execute(
        "SELECT COUNT(*) AS c FROM control_requests WHERE run_id = ? AND action='pause'"
        " AND applied_at IS NOT NULL", (run_id,)
    ).fetchone()
    assert row["c"] == 1, "pause 请求应被运行中的 worker 消费并留下应用时间"
