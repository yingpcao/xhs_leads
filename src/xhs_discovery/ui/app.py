"""P0-E-UI 看板本体（P0-31～P0-34）：五类离线页面 + 真实 ui 启动。

- 创建任务、运行看板、关键词发现图、需求图/证据详情、导出。
- 全部复用 `xhs_discovery.app` 已验收的应用服务与存储/账本/worker，不复制算法。
- 默认与 fixture 同一条离线路径：不接模型、不下载权重、不读凭据、不代做登录。live 只在创建页
  显式配齐已测工具的绝对路径与共享锁目录后才可用，缺配置按配置口径拒绝；能力边界（title_only、
  点赞非搜索量、故障即暂停）写在创建页与批次说明里，不把 live 冒充成"全量采集"。
- worker 由独立进程持锁（OS 锁 + 租约，见 `storage.worker`），页面 rerun 不重复启动、
  不终止后台任务；进程互斥只信 `WorkerControl`，不把 session_state 当互斥。
- 双图用 pydeck `OrthographicView` + 直角坐标节点/边（`st.pydeck_chart` 支持 on_select），
  无地图 token、无远程 CDN、无外部服务；同时提供节点选择/搜索作为键盘可访问入口，
  与图点选共用详情。点击选择事件的真实渲染属浏览器视觉验收项。
- 需求详情/排行榜只读所选已完成批次的产物文件（经哈希+内嵌哈希完整性校验），
  绝不把正在被后台 worker 重写的可变 needs 表当"上次已完成批次"展示。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from xhs_discovery import app as services
from xhs_discovery.config import ConfigError
from xhs_discovery.ui import support

KIND_COLOR = {
    "keyword": [214, 96, 60],
    "topic": [90, 130, 210],
    "problem": [130, 90, 190],
    "need": [60, 160, 90],
    "evidence": [210, 170, 60],
    "post": [120, 120, 120],
    "suggestion": [60, 170, 170],
}
STATEMENT_LABEL = {
    "explicit_problem": "显性问题",
    "inferred_need": "推断需求",
    "solution_idea": "方案构想（不上看板）",
    "search_intent_candidate": "搜索意图候选（不上看板）",
}


# ---------------------------------------------------------------- 恢复用配置定位
def _resolve_run_config(repo, paths, run_id: str) -> str:
    """为后台 `resume` 子进程定位一份可读配置：优先复用创建页写下的派生配置，否则由
    库中 run.config_json 重放出的 Settings 现写一份。两者都指向同一数据根。"""
    derived = paths.root / "ui_configs" / f"{run_id}.yaml"
    if derived.is_file():
        return str(derived)
    settings = services.stored_settings(repo, run_id)
    return support.write_derived_config(settings, paths, run_id)


# ---------------------------------------------------------------- 图渲染辅助
def _deck_for(gnodes: list[dict], gedges: list[dict], positions: dict) -> object:
    """委托 support.build_deck：像素直角坐标、zoom=0 初始即适配画布，中文字符集随图层携带。"""
    return support.build_deck(gnodes, gedges, positions, KIND_COLOR)


def _provenance_note(mode: str) -> str:
    """按 run 的模式说清 note_id 的来历：live 的 id 是平台返回值，不能一概写成合成值。"""
    if str(mode) == "live":
        return (
            "live run：note_id / access_url 是本机 CLI 从平台返回的值，看板不打开它们、不代做登录；"
            "正文与作者公开笔记不在 title_only 能力范围内（未采集 ≠ 没有）。"
        )
    return "note_id / access_url 均为合成 fixture 值，仅作数据展示，不是真实平台可访问链接。"


def _show_need_detail(detail: dict, mode: str) -> None:
    need = detail["need"]
    st.markdown(f"#### 需求 #{need.get('need_id')}：{need.get('canonical_text')}")
    st.caption(_provenance_note(mode))
    st.write(
        f"statement_kind=`{STATEMENT_LABEL.get(need.get('statement_kind'), need.get('statement_kind'))}` ｜ "
        f"evidence_status=`{need.get('evidence_status')}` ｜ score=`{need.get('score')}` ｜ "
        f"effective_weight_total=`{need.get('effective_weight_total')}`"
    )
    if str(need.get("provisional")) == "1":
        st.warning(f"小样本 provisional：{need.get('provisional_reason') or '（未给出原因）'}；保留原始计数，不做跨批绝对分位比较。")
    st.markdown("**S/Q/A/F 分解**（原始 / 归一化 / 权重 / 缺失原因）")
    rows = []
    for letter, name in (("S", "规模"), ("Q", "强度"), ("A", "可行动"), ("F", "新鲜度")):
        rows.append(
            {
                "维度": f"{letter}·{name}",
                "原始": need.get(f"{letter}_raw"),
                "归一化": need.get(f"{letter}_normalized"),
                "权重": need.get(f"{letter}_weight"),
                "缺失原因": need.get(f"{letter}_missing_reason"),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if need.get("unknown_components"):
        st.info(f"未知/推断分量：{need.get('unknown_components')}")

    st.markdown("**发现路径**（原始关键词 → … → 当前，逐词可回溯到种子）")
    if detail["discovery_paths"]:
        for term, chain in detail["discovery_paths"].items():
            st.write(f"- `{term}`：{' → '.join(chain) if chain else '（无父链记录）'}")
    else:
        st.caption("（此需求未回链到可解析的源关键词）")

    st.markdown(f"**可定位原始证据（{len(detail['evidence'])} 条）**")
    ev_rows = []
    for ev in detail["evidence"]:
        ev_rows.append(
            {
                "原文引用": ev.get("quote"),
                "字段": ev.get("source_field"),
                "可定位": "是" if str(ev.get("locatable")) == "1" else "否",
                "synthetic": "合成" if str(ev.get("synthetic")).lower() in ("1", "true") else "",
                f"note_id（{'平台返回值' if str(mode) == 'live' else '合成值'}）": ev.get("note_id"),
                "关键词": ev.get("keyword_normalized"),
            }
        )
    st.dataframe(pd.DataFrame(ev_rows), hide_index=True, use_container_width=True)


def _show_keyword_detail(dataset: dict, keyword_id: str) -> None:
    row = next((k for k in dataset["keywords"] if str(k.get("keyword_id")) == str(keyword_id)), None)
    if row is None:
        st.info("该关键词节点不在此批次的 keywords.csv 中（可能只在发现图里出现）。")
        return
    st.markdown(f"#### 关键词 #{row.get('keyword_id')}：{row.get('normalized')}")
    chain = support.keyword_path(dataset["keywords"], row.get("keyword_id"))
    st.write(f"发现路径：{' → '.join(chain) if chain else '（自身即种子）'}")
    st.json({key: row.get(key) for key in
             ("origin", "depth", "status", "is_seed", "parent_normalized", "independent_parents",
              "suggestion_observations", "distinct_posts", "problems_extracted", "displayable_needs")})


def _graph_page(kind: str, repo, paths, run_ids: list[str], config_path: str) -> None:
    title = "关键词发现图" if kind == "discovery" else "需求图 / 证据详情"
    st.subheader(title)
    if not run_ids:
        st.info("还没有 run：到「创建任务」页新建，或选择指向已有数据根的配置文件。")
        return
    run_id = st.selectbox("选择 run", run_ids, key=f"{kind}_run")
    batches = [b for b in services.completed_batches(repo, run_id) if b["verification_ok"]]
    if not batches:
        st.warning("该 run 尚无已完成的分析批次：双图/详情/排行榜只读通过完整性校验的固定批次产物。")
        return
    snapshots = [b["snapshot"] for b in batches]
    snapshot = st.selectbox("批次（snapshot）", snapshots, index=len(snapshots) - 1, key=f"{kind}_snap")
    try:
        dataset = support.load_batch_dataset(paths, run_id, snapshot)
    except services.AppError as exc:
        st.error(f"批次读取失败：{exc}")
        return

    nodes, edges = support.parse_graphml(dataset["graphml"])
    discovery, demand = support.split_graphs(nodes, edges)

    if kind == "discovery":
        gnodes, gedges = list(discovery["nodes"]), list(discovery["edges"])
        total = len(gnodes)
        max_nodes = st.slider("最多显示节点数", 50, 800, 400, key=f"{kind}_cap")
        capped = total > max_nodes
        if capped:
            gnodes = gnodes[:max_nodes]
            refs = {n.get("ref") for n in gnodes}
            gedges = [e for e in gedges if e.get("source_ref") in refs and e.get("target_ref") in refs]
        st.caption(
            f"发现图：keyword 节点 {total} 个，suggests 边 {len(discovery['edges'])} 条"
            f"{'；已按上限截断，仅显示 ' + str(len(gnodes)) + ' 个' if capped else ''}。"
            "点选/搜索节点查看其发现路径与调度事实。"
        )
        topics_sel: list[str] = []
        intents_sel: list[str] = []
    else:
        topic_labels = {n.get("ref"): (n.get("label") or n.get("ref")) for n in demand["nodes"] if n.get("kind") == "topic"}
        intents = sorted({n.get("intent", "") for n in demand["nodes"] if n.get("kind") == "problem" and n.get("intent")})
        col_t, col_i, col_c = st.columns(3)
        topics_sel = col_t.multiselect("主题过滤（按需求归属问题的 topic）", sorted(topic_labels),
                                       format_func=lambda r: topic_labels.get(r, r), key=f"{kind}_topics")
        intents_sel = col_i.multiselect("意图过滤", intents, key=f"{kind}_intents")
        max_nodes = col_c.slider("最多显示节点数", 50, 800, 400, key=f"{kind}_cap")
        gnodes, gedges, note = support.filter_demand(
            demand["nodes"], demand["edges"], dataset["needs"],
            topics=topics_sel, intents=intents_sel, max_nodes=max_nodes,
        )
        st.caption(
            f"需求图：总节点 {note['total_nodes']}，过滤后显示 {note['shown_nodes']}"
            f"（主题剔除 {note['filtered_topic']}、意图剔除 {note['filtered_intent']}）"
            f"{'，已按上限截断' if note['capped'] else ''}。"
        )
        if not topics_sel and not intents_sel and note["total_nodes"] > note["shown_nodes"]:
            st.caption("（仅因节点上限截断；未选择主题/意图，不是过滤掉内容。）")

        st.markdown(
            "**需求排行榜**（分层键=表述类别+有效权重+S/Q/A/F 分量档案：总权重相同但缺失分量不同、"
            "或显性问题与推断需求，都不并成同一精确名次；无证据/构想/候选不入榜）"
        )
        board = support.leaderboard(dataset["needs"])
        if not board:
            st.info("此批次没有可展示的需求（可能全部证据不足或尚未跑完分析）。")
        else:
            board_rows = [
                {
                    "权重层名次": b["weight_group_rank"], "层内条数": b["group_size"],
                    "有效权重": round(b["effective_weight_total"], 4), "score": b["score"],
                    "need_id": b["need_id"], "类别": STATEMENT_LABEL.get(b["statement_kind"], b["statement_kind"]),
                    "证据状态": b["evidence_status"], "provisional": b["provisional"],
                    "证据行数": b["evidence_rows"], "去重帖数": b["distinct_posts"],
                    "文本": b["canonical_text"][:36],
                }
                for b in board
            ]
            st.dataframe(pd.DataFrame(board_rows), hide_index=True, use_container_width=True)

    positions = support.fit_positions_to_canvas(
        support.layout_positions(gnodes, gedges, seed=int(abs(hash(run_id))) % 9973 or 1)
    )
    chart = st.pydeck_chart(
        _deck_for(gnodes, gedges, positions),
        on_select="rerun", selection_mode="single-object", key=f"{kind}_chart",
        use_container_width=True,
    )
    # 只接受当前图节点里的合法 ref：空/未知图层/别的 run 或页面的选择残留一律忽略
    visible_refs = {n.get("ref") for n in gnodes if n.get("ref")}
    clicked = support.valid_selection_refs(chart, visible_refs)

    options = [n.get("ref") for n in gnodes if n.get("ref")]
    label_of = {n.get("ref"): f"{n.get('kind')} ｜ {(n.get('label') or '')[:28]} ｜ {n.get('ref')}" for n in gnodes}
    chosen = st.selectbox(
        "选择节点（键盘可访问入口，与图点选共用详情）",
        ["—"] + options,
        format_func=lambda r: label_of.get(r, r) if r != "—" else "（未选择）",
        key=f"{kind}_pick",
    )
    selected_ref = clicked[0] if clicked else (chosen if chosen != "—" else None)
    if clicked and chosen != "—" and chosen not in clicked:
        st.caption("（图上刚点选了新节点，优先展示点选结果。）")
    if not selected_ref:
        st.info("点选图中的节点，或在上方下拉框搜索一个节点以查看详情。点击事件的前端渲染属浏览器视觉验收项。")
        return
    node = next((n for n in gnodes if n.get("ref") == selected_ref), None)
    prefix, _, identity = str(selected_ref).partition(":")
    st.markdown(f"**选中节点**：`{selected_ref}`")
    if node:
        st.json({k: v for k, v in node.items() if k != "ref"})
    if prefix == "need":
        detail = support.need_detail(dataset, identity)
        if detail:
            _show_need_detail(detail, str((dataset.get("report") or {}).get("run", {}).get("mode") or ""))
        else:
            st.info("该 need 节点未落在 needs.csv（不可展示/证据断裂），故不进入排行榜与详情正文。")
    elif prefix == "kw":
        _show_keyword_detail(dataset, identity)
    else:
        st.caption("该节点类型无独立详情面板，上方属性即为批次文件里记录的全部信息。")


def _render_export_preview(paths, run_id: str, snapshot) -> None:
    st.markdown("**批次内容预览（只读固定产物，按文本展示，不当作可执行/可跳转内容）**")
    try:
        dataset = support.load_batch_dataset(paths, run_id, snapshot)
    except services.AppError as exc:
        st.error(f"预览读取失败：{exc}")
        return
    report = dataset["report"]
    with st.expander("report.json 关键摘要"):
        st.json(
            {
                "meta_subset": {k: report.get("meta", {}).get(k) for k in ("run_id", "analysis_version", "extractor_version", "protocol_version")},
                "run_subset": {k: report.get("run", {}).get(k) for k in ("status", "outcome", "pause_reason", "synthetic_fixture")},
                "graph": report.get("graph", {}).get("merged_summary"),
                "exclusions": report.get("analysis", {}).get("exclusions"),
                "limitations_count": len(report.get("limitations", [])),
            }
        )
    with st.expander(f"needs.csv（{len(dataset['needs'])} 行，取前 20）"):
        cols = ("need_id", "canonical_text", "statement_kind", "evidence_status", "score", "effective_weight_total", "provisional", "evidence_rows")
        st.dataframe(pd.DataFrame(dataset["needs"][:20])[list(cols)], hide_index=True, use_container_width=True) if dataset["needs"] else st.info("（本批次 needs.csv 无行）")
    with st.expander(f"evidence.csv（{len(dataset['evidence'])} 行，取前 20）"):
        if dataset["evidence"]:
            ev_cols = ("evidence_id", "quote", "source_field", "locatable", "synthetic", "note_id", "keyword_normalized")
            st.dataframe(pd.DataFrame(dataset["evidence"][:20])[list(ev_cols)], hide_index=True, use_container_width=True)
        else:
            st.info("（本批次 evidence.csv 无行）")
    st.caption(
        _provenance_note(str((report.get("run") or {}).get("mode") or ""))
        + "预览按文本展示，不渲染成可跳转链接。"
    )


st.set_page_config(page_title="小红书长尾需求发现（离线看板）", layout="wide")
st.title("小红书长尾需求发现引擎 — 离线看板")
st.caption(
    "看板只读所选 run 的固定批次。fixture run 全部数据为合成 fixture：note_id / access_url / "
    "作者引用均为合成值，不代表真实平台内容，也不是可访问的真实链接。live run 来自本机已配置的 "
    "xhs-search CLI，只有搜索卡片标题（title_only）：正文与作者公开笔记不支持，点赞数是卡片点赞"
    "而非搜索量。两种模式都不接模型、不下载权重；live 也不读凭据、不代做登录。"
)

config_path = st.sidebar.text_input("配置文件（定位数据根 run.data_dir）", "config/default.yaml")
page = st.sidebar.radio(
    "页面", ["创建任务", "运行看板", "关键词发现图", "需求图 / 证据详情", "导出", "人工标注"], index=0
)

if page == "人工标注":
    from xhs_discovery.ui.annotations import render

    render()
    st.stop()

if not Path(config_path).is_file():
    st.error(f"配置文件不存在：{config_path}（相对当前工作目录）")
    st.stop()

try:
    repo, paths, settings, conn = support.open_store_conn(config_path, Path.cwd())
except Exception as exc:  # noqa: BLE001 - 坏配置/坏数据根要显示原因而不是白屏
    st.error(f"读取数据根失败：{exc}")
    st.stop()


def stop_and_close() -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - 关闭失败不掩盖页面错误
        pass


try:
    runs = services.list_runs(repo)
    run_ids = [r["id"] for r in runs]

    # ------------------------------------------------------------ 创建任务
    if page == "创建任务":
        st.subheader("创建任务")
        seed = st.text_input("种子原文（保留歧义，不改写）", settings.run.seed_raw)
        mode_options = ["fixture", "live"]
        mode = st.selectbox(
            "模式", mode_options, index=mode_options.index(settings.run.mode) if settings.run.mode in mode_options else 0,
            help="fixture=合成数据集，离线；live=本机已配置的 xhs-search CLI，"
            "必须把工具路径、profile、锁目录与地域口径配置完备，缺任一项都不会被当作可用（拒绝启动）。",
        )
        live_fields: dict = {}
        if mode == "live":
            section = settings.sources.xhs_search
            st.markdown("**live 来源配置**（全部写入派生配置；绝对路径固定「已测的那份工具」）")
            l1, l2 = st.columns(2)
            node_path = l1.text_input("node 可执行文件绝对路径", section.node_path)
            cli_path = l2.text_input("xhs-search.mjs 绝对路径", section.cli_path or "")
            l3, l4 = st.columns(2)
            profile = l3.text_input("profile 名", section.profile or "default")
            browser = l4.selectbox("browser", ["msedge", "chrome", "chromium"],
                                   index=["msedge", "chrome", "chromium"].index(section.browser) if section.browser in ("msedge", "chrome", "chromium") else 0)
            l5, l6 = st.columns(2)
            lock_dir = l5.text_input("同 profile 互斥目录（跨数据根共享，不得在 run.data_dir 内）", section.lock_dir or "")
            locations = l6.text_input("地域口径 locations（逗号分隔，必填）", ",".join(section.locations))
            l7, l8, l9 = st.columns(3)
            op_timeout = l7.number_input("单操作超时（秒）", 5, 900, int(section.timeout_seconds))
            total_timeout = l8.number_input("本进程总超时（秒）", 10, 3600, int(section.total_process_timeout_seconds))
            scrolls = l9.number_input("搜索滚动次数", 0, 20, int(section.search_scrolls))
            l10, l11 = st.columns(2)
            min_interval = l10.number_input("两次外部访问最小间隔（秒）", 1.0, 600.0, float(section.min_interval_seconds), step=0.5)
            ambiguous = l11.text_input("歧义种子清单（逗号分隔，留空=保守规则不标歧义）", ",".join(section.ambiguous_seeds))
            live_fields = {
                "node_path": node_path,
                "cli_path": cli_path,
                "profile": profile,
                "browser": browser,
                "lock_dir": lock_dir,
                "locations": [item.strip() for item in locations.split(",") if item.strip()],
                "ambiguous_seeds": [item.strip() for item in ambiguous.split(",") if item.strip()],
                "timeout_seconds": int(op_timeout),
                "total_process_timeout_seconds": int(total_timeout),
                "min_interval_seconds": float(min_interval),
                "search_scrolls": int(scrolls),
            }
            readiness = support.live_readiness(settings, xhs_search=live_fields)
            if readiness["ready"]:
                st.success("live 配置完备：工具绝对路径、共享锁目录与地域口径都已配齐（能力边界见下）。")
            else:
                st.error("live 配置不完备，缺任何一项都不算可用（点创建也会按配置口径拒绝）：\n"
                         f"{readiness['notes'][0]}")
            for note in readiness["notes"][1:] if not readiness["ready"] else readiness["notes"]:
                st.markdown(f"- {note}")
            st.markdown(
                "**live 分析侧边界（创建前必须知道）**\n\n"
                "- 评分基于标题样本的抽样口径，样本代表性有限，不等同于全量需求强度；\n"
                "- 未知发布日期/作者按 missing 保留，「没采到」不等于「没有需求」；\n"
                "- 撞到登录/验证码/选择器/页面/schema 问题时 worker 会**暂停并等人处置**，"
                "不会自动重发；\n\n来源侧的能力边界与互斥口径见下方「live 就绪说明」。"
            )
        st.markdown("**分项预算**（不存在隐含无限）")
        c1, c2, c3 = st.columns(3)
        suggest = c1.number_input("联想尝试 suggest_attempts", 0, 500, int(settings.budget.suggest_attempts))
        search = c2.number_input("搜索尝试 search_attempts", 0, 500, int(settings.budget.search_attempts))
        max_posts = c3.number_input("存储上限 max_posts_stored", 0, 2000, int(settings.budget.max_posts_stored))
        calibration = st.number_input(
            "覆盖校准预算 calibration_suggest_attempts", 0, 500,
            int(settings.budget.calibration_suggest_attempts),
        )
        st.markdown("**采样配置**")
        d1, d2, d3 = st.columns(3)
        high = d1.number_input("高价值档位 high_value_posts", 0, 20, int(settings.sampling.high_value_posts))
        medium = d2.number_input("中价值档位 medium_value_posts", 0, 20, int(settings.sampling.medium_value_posts))
        low = d3.number_input("低价值档位 low_value_posts", 0, 2, int(settings.sampling.low_value_posts))

        st.markdown("**模型开关**（P0 一律关闭：打开会被明确拒绝）")
        llm = st.checkbox("启用 LLM（llm_enabled）", bool(settings.models.llm_enabled))
        emb = st.checkbox("启用 embedding（embedding_enabled）", bool(settings.models.embedding_enabled))

        run_id_input = st.text_input("run_id（留空自动生成）", "")

        if llm or emb:
            st.warning("P0 不接模型：本轮不下载权重、不调用外部 API。规则基线可独立运行，模型开关必须保持关闭。")

        if st.button("创建并后台启动 worker", type="primary"):
            try:
                derived = support.derive_settings(
                    settings, mode=mode, seed=seed,
                    budget={
                        "suggest_attempts": int(suggest), "search_attempts": int(search),
                        "max_posts_stored": int(max_posts),
                        "calibration_suggest_attempts": int(calibration),
                    },
                    sampling={
                        "high_value_posts": int(high), "medium_value_posts": int(medium),
                        "low_value_posts": int(low),
                    },
                    llm_enabled=llm, embedding_enabled=emb,
                    xhs_search=live_fields or None,
                )
            except ConfigError as exc:
                st.error(f"配置非法（未启动）：\n{exc}")
                st.stop()
            except services.UnsupportedCapabilityError as exc:
                st.error(f"能力被明确拒绝（未启动）：{exc}")
                st.stop()

            stamp = services.utc_now()
            new_run_id = run_id_input.strip() or services.generate_run_id(derived, stamp)
            try:
                new_run_id = services.validate_run_id(new_run_id)
            except services.UsageError as exc:
                st.error(str(exc))
                st.stop()
            if repo.get_run(new_run_id) is not None:
                st.error(f"run {new_run_id!r} 已存在：不允许用同一个 id 覆盖历史轨迹")
                st.stop()
            config_file = support.write_derived_config(derived, paths, new_run_id)
            launched = st.session_state.setdefault("launched", set())
            command = [
                sys.executable, "-m", "xhs_discovery", "run", "--mode", derived.run.mode,
                "--seed", seed or derived.run.seed_raw, "--config", config_file, "--run", new_run_id,
            ]
            outcome = support.plan_worker_launch(
                conn, paths, new_run_id, command, session_launched=launched, cwd=Path.cwd()
            )
            if not outcome["launched"]:
                prefix = "未重复启动" if outcome["reason"] in ("session", "occupied") else "后台启动失败（进程未创建）"
                st.warning(f"{prefix}：{outcome['detail']}")
            else:
                record = outcome["record"]
                st.success(
                    f"启动请求已记录：run `{new_run_id}` 后台进程已创建（pid={record['pid']}，无可见控制台）。"
                    "注意：这只证明进程创建成功，worker 是否已持锁开始运行，以运行看板的"
                    "进程锁/租约判定为准；刷新页面不会重复启动，也不会终止后台任务。"
                )
                st.code(" ".join(command), language="text")
                with st.expander("启动登记与日志落点"):
                    st.json({k: record[k] for k in ("pid", "requested_at", "stdout_log", "stderr_log")})

    # ------------------------------------------------------------ 运行看板
    elif page == "运行看板":
        st.subheader("运行看板")
        if not run_ids:
            st.info("该数据根还没有任何 run：到「创建任务」页新建一个 fixture 任务。")
        else:
            run_id = st.selectbox("选择 run", run_ids, index=0)
            dash = support.read_dashboard(repo, conn, paths, run_id)
            if "error" in dash:
                st.error(dash["error"])
            else:
                status_badge = {"running": "🟢", "paused": "🟡", "queued": "⚪", "completed": "✅", "failed": "🔴"}
                st.markdown(
                    f"**状态：{status_badge.get(dash['status'], '')} {dash['status']}** ｜ "
                    f"mode={dash['mode']} ｜ seed=`{dash['seed_raw']}` ｜ "
                    f"outcome={dash['outcome'] or '—'} ｜ pause_reason={dash['pause_reason'] or '—'} ｜ "
                    f"budget_revision={dash['budget_revision']}"
                )
                if dash["worker_occupied"]:
                    st.info(f"当前有活跃 worker 持锁（后台进程）：{dash['worker_detail']}")

                stop = dash.get("stop_note")
                if stop:
                    note = stop["detail"].get("note") or "（事件未附详细停止说明）"
                    st.markdown(f"**最近停止/暂停事件**：`{stop['kind']}` @ {stop['at']}")
                    st.write(f"原始停止说明：{note}")
                    signals = stop["detail"].get("signals")
                    if isinstance(signals, dict):
                        with st.expander("停止信号原始计数（signals）"):
                            st.json(signals)
                    if dash["status"] == "paused":
                        st.caption("这是暂停不是收敛（converged）；追加额度或恢复后可继续。若为模拟认证暂停，请检查本地 fixture/环境，不要登录真实平台。")

                counts = dash["counts"]
                k1, k2, k3, k4, k5 = st.columns(5)
                k1.metric("关键词", counts["keywords"])
                k2.metric("去重帖子", counts["posts"])
                k3.metric("有效问题/需求", (dash["analysis"] or {}).get("displayable_needs", "无完成批次"))
                k4.metric("失败操作", counts["failures"])
                k5.metric("未结操作", counts["unfinished"])
                if dash["analysis"]:
                    a = dash["analysis"]
                    st.caption(
                        f"最新完成批次 {a['snapshot']}（analysis_version={a['analysis_version']}）："
                        f"需求总数 {a['needs_total']}、问题 {a['problems']}、证据 {a['evidence']}。"
                        "看板读取的是该固定批次产物，不覆盖正在重算的库表。"
                    )

                st.markdown("**预算：已用 / 预留 / 上限（剩余）**")
                budget_rows = [
                    {
                        "键": row["key"], "已用": row["used"], "预留": row["reserved"],
                        "上限": row["limit"], "剩余": row["remaining"],
                        "耗尽": "是" if row["exhausted"] else "",
                    }
                    for row in dash["budget"]
                ]
                st.dataframe(pd.DataFrame(budget_rows), hide_index=True, use_container_width=True)

                st.markdown("**关键词状态分布**")
                st.json(counts["keywords_by_status"])

                st.markdown("**控制（作用于同一 run）**")
                cc1, cc2, cc3 = st.columns(3)
                # 面板开关存在 session_state 里、并按 run 隔离：Streamlit 的 button 只在被点
                # 的那一次 rerun 返回 True，用 `if button("追加预算"): ... if button("确认追加")`
                # 这种嵌套写法，确认事件永远落在"外层已为 False"的下一次 rerun 里，永不执行。
                panel = st.session_state.setdefault("control_panel", {"run_id": None, "mode": None})

                if cc1.button("暂停", disabled=dash["status"] not in ("queued", "running")):
                    try:
                        result = services.pause_run(config_path, Path.cwd(), run_id, note="UI 暂停请求")
                        st.success(f"暂停请求已写入：{result['note']}")
                    except services.AppError as exc:
                        st.error(str(exc))

                if cc2.button("追加预算", disabled=dash["status"] not in ("queued", "running", "paused")):
                    panel["run_id"], panel["mode"] = run_id, "extend"
                if cc3.button("恢复（后台 worker）", disabled=dash["status"] != "paused"):
                    panel["run_id"], panel["mode"] = run_id, "resume"

                # 面板按 run 隔离：切到别的 run 时这里为 False，确认按钮不会替别的 run 提交任务
                if panel["run_id"] == run_id and panel["mode"] == "extend":
                    add_s = cc2.number_input("追加联想", 0, 500, 4, key=f"ext_s_{run_id}")
                    add_se = cc2.number_input("追加搜索", 0, 500, 0, key=f"ext_se_{run_id}")
                    if cc2.button("确认追加"):
                        try:
                            result = services.extend_run(
                                config_path, Path.cwd(), run_id,
                                suggest_budget=add_s or None, search_budget=add_se or None,
                                note="UI 追加额度",
                            )
                            panel["run_id"], panel["mode"] = None, None
                            st.success(f"追加请求已写入（只增不清零）：{result['note']}")
                        except services.AppError as exc:
                            st.error(str(exc))

                if panel["run_id"] == run_id and panel["mode"] == "resume":
                    max_slots = cc3.number_input(
                        "本次最多槽位（0=不限）", 0, 500, 0, key=f"resume_slots_{run_id}"
                    )
                    if cc3.button("确认恢复"):
                        derived_file = _resolve_run_config(repo, paths, run_id)
                        command = [
                            sys.executable, "-m", "xhs_discovery", "resume",
                            "--run", run_id, "--config", derived_file,
                        ]
                        if max_slots:
                            command += ["--max-slots", str(int(max_slots))]
                        outcome = support.plan_worker_launch(
                            conn, paths, run_id, command,
                            session_launched=st.session_state.setdefault("launched", set()),
                            cwd=Path.cwd(),
                        )
                        if not outcome["launched"]:
                            st.warning(f"未启动恢复 worker：{outcome['detail']}")
                        else:
                            panel["run_id"], panel["mode"] = None, None
                            record = outcome["record"]
                            st.success(
                                f"恢复启动请求已记录：`{run_id}` 后台进程已创建（pid={record['pid']}，无可见控制台）。"
                                "worker 是否持锁运行以本页进程锁/租约判定为准。"
                            )
                            st.code(" ".join(command), language="text")

                launch = support.worker_launch_status(paths, run_id)
                with st.expander(f"后台启动登记（{launch['state']}）"):
                    st.write(launch["detail"])
                    if launch.get("record"):
                        st.json(
                            {
                                k: launch["record"].get(k)
                                for k in ("run_id", "pid", "requested_at", "stdout_log", "stderr_log", "create_error")
                            }
                        )
                    excerpt = support.launch_log_excerpt(paths, run_id)
                    if excerpt:
                        st.markdown("**子进程日志尾部**")
                        st.code(excerpt, language="text")

                st.markdown("**最近事件**")
                st.dataframe(
                    pd.DataFrame([{"at": e["at"], "kind": e["kind"]} for e in dash["recent_events"]]),
                    hide_index=True, use_container_width=True,
                )

    # ------------------------------------------------------------ 关键词发现图
    elif page == "关键词发现图":
        _graph_page("discovery", repo, paths, run_ids, config_path)

    # ------------------------------------------------------------ 需求图 / 证据详情
    elif page == "需求图 / 证据详情":
        _graph_page("demand", repo, paths, run_ids, config_path)

    # ------------------------------------------------------------ 导出
    else:
        st.subheader("导出（读所选已完成批次，可下载并反读）")
        if not run_ids:
            st.info("还没有 run。")
        else:
            run_id = st.selectbox("选择 run", run_ids, key="export_run")
            batches = [b for b in services.completed_batches(repo, run_id) if b["verification_ok"]]
            if not batches:
                st.warning("该 run 没有已完成的分析批次：先创建/恢复跑完探索→分析→导出。")
            else:
                snapshots = [b["snapshot"] for b in batches]
                snapshot = st.selectbox("批次（snapshot）", snapshots, index=len(snapshots) - 1)
                try:
                    info = support.verified_batch_files(repo, paths, run_id, snapshot)
                except services.AppError as exc:
                    st.error(f"批次完整性校验未通过：{exc}")
                    st.stop()
                st.write(
                    f"verification.mode=`{info['verification']['mode']}` ｜ "
                    f"analysis_version=`{info['analysis_version']}` ｜ "
                    f"analysis_time_verification_ok={info['verification'].get('analysis_time_verification_ok')}"
                )
                for item in info["files"]:
                    filename = item["filename"]
                    data = Path(item["abs_path"]).read_bytes()
                    st.download_button(
                        f"下载 {filename}（{item['sha256'][:12]}…）",
                        data=data, file_name=filename, key=f"dl_{run_id}_{snapshot}_{filename}",
                    )
                _render_export_preview(paths, run_id, snapshot)
                out_dir = st.text_input("可选：复制到目录（相对当前工作目录）", "")
                if st.button("复制该批次产物") and out_dir:
                    try:
                        result = services.export_batch(config_path, Path.cwd(), run_id, snapshot=snapshot, out=out_dir)
                        st.success(f"已复制到：{result['copied_to']}")
                    except services.AppError as exc:
                        st.error(str(exc))
finally:
    stop_and_close()
