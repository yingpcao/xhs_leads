"""P0-E-UI-r3：真实表单控制的端到端测试（真 AppTest 按钮 + 真应用服务 + 真后台子进程）。

与 `tests/test_ui_support.py`（视图模型层单测）互补，这里覆盖 r3 复核要求的生产用户序列：

- 追加/恢复面板由 session_state 持久化、按 run 隔离：点「追加预算」后确认按钮存在于**同一次
  rerun**，确认一次只写一条控制请求，纯 rerun 不重复；外层瞬时 button 包内层确认 button 的
  旧写法（确认事件永不消费）在这里会直接失败。
- 数值输入跨 rerun 保留，且确认时按用户改过的值落库（不是默认值）。
- 切 run 不串任务：A 的面板不会替 B 提交，B 上看板时 A 的确认按钮不渲染。
- 同一 session 创建的 run 正常退出后可以 追加→恢复→再追加→再恢复。
- 停止说明：真实 fixture run 被大量分析事件挤掉近期列表后，看板仍显示预算停止 note。

数据一律落在系统临时目录的合成 fixture 沙箱里，不碰仓库数据根。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from xhs_discovery import app as services
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.repository import Repository
from xhs_discovery.ui import support

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "xhs_discovery" / "ui" / "app.py"


# --------------------------------------------------------------------- 沙箱与探针工具


def _build_sandbox(target: Path) -> Path:
    (target / "config").mkdir(parents=True, exist_ok=True)
    (target / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
    for rel in ("config/domain_terms.txt", "config/intent_rules.yaml"):
        shutil.copyfile(ROOT / rel, target / rel)
    shutil.copyfile(ROOT / "tests" / "fixtures" / "shanghai_zhongkao.json",
                    target / "tests" / "fixtures" / "shanghai_zhongkao.json")
    demo = (ROOT / "config" / "offline-demo.yaml").read_text(encoding="utf-8")
    tiny = (
        demo.replace("suggest_attempts: 14", "suggest_attempts: 6")
        .replace("search_attempts: 6", "search_attempts: 2")
        .replace("calibration_suggest_attempts: 3", "calibration_suggest_attempts: 1")
    )
    (target / "config" / "default.yaml").write_text(tiny, encoding="utf-8")
    return target


@pytest.fixture
def box(tmp_path, monkeypatch):
    sandbox = _build_sandbox(tmp_path)
    monkeypatch.chdir(sandbox)  # 页面按 Path.cwd() 定位 config/default.yaml 与数据根
    return sandbox


def _query(box: Path, sql: str, params: tuple, one: bool = False):
    with sqlite3.connect(box / "data" / "xhs_discovery.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return (rows[0] if rows else None) if one else rows


def _run_row(box: Path, run_id: str) -> dict | None:
    return _query(box, "SELECT status, budget_revision FROM runs WHERE id = ?", (run_id,), one=True)


def _control_rows(box: Path, run_id: str, action: str = "extend") -> list[dict]:
    return _query(
        box,
        "SELECT action, suggest_budget_delta, search_budget_delta, note, applied_at"
        " FROM control_requests WHERE run_id = ? AND action = ? ORDER BY id",
        (run_id, action),
    )


def _paths(box: Path) -> RuntimePaths:
    return RuntimePaths(box / "data")


def _settle(box: Path, run_id: str, min_revision: int = 1, timeout: float = 90.0) -> dict:
    """等后台 worker 自己停在可恢复的 paused（不改引擎、不伪造状态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _run_row(box, run_id)
        if row and row["status"] == "paused" and int(row["budget_revision"]) >= min_revision:
            return row
        time.sleep(0.05)
    raise AssertionError(f"{run_id} 未在 {timeout}s 内停在 paused(>={min_revision})：{_run_row(box, run_id)}")


def _wait_worker_released(box: Path, run_id: str, timeout: float = 60.0) -> None:
    """真实放行条件：上一个 worker 既释放 OS 锁/租约（WorkerControl），其**启动进程也确认退出**
    （`last_launch_process_state` ∈ dead/none）——下一轮启动才真的被允许。

    这不是固定 sleep，而是轮询两条真实状态。缺一不可，因为后台启动的门就是这两条：
    - `worker_occupied`（锁/租约）判定以 `WorkerControl` 为准；
    - `plan_worker_launch` 还会扫描其他/同一 run 的启动登记，只要上一个进程仍 live/unknown
      就按"occupied/previous_process"正确拒绝，避免并发两个 worker（同文件的
      `test_pending_child_blocks_another_run_before_worker_lock` 锁住同一产品门）。
    worker 是"先放锁、进程随后退出"，只等锁释放会踩进这段真实退出窗口：下一次点击会被正确
    拒启、对应 run 根本不会创建，表现为 `_settle` 超时拿到 None（独立整套即此形态）。真实用户
    两次操作天然有时间差，AppTest 瞬时点击必须显式等到"进程确认退出"这一半才算合法放行。
    """
    paths = _paths(box)
    conn = storage_db.connect(paths.db_path)
    try:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            occupied, detail = support.worker_occupied(conn, paths, run_id)
            state, state_detail = support.last_launch_process_state(paths, run_id)
            if not occupied and state in ("dead", "none"):
                return
            last = f"occupied={occupied}({detail}) state={state}({state_detail})"
            time.sleep(0.05)
        raise AssertionError(f"worker 未在 {timeout}s 内满足下一次启动的真实放行条件：{last}")
    finally:
        conn.close()


def _button(at: AppTest, label: str):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"页面上没有按钮 {label!r}，当前按钮：{[b.label for b in at.button]}"
    return matches[0]


def _has_button(at: AppTest, label: str) -> bool:
    return any(b.label == label for b in at.button)


def _dashboard_run_select(at: AppTest):
    return next(s for s in at.selectbox if s.label == "选择 run")


def _create_run(at: AppTest, run_id: str) -> None:
    _input(at, "run_id（留空自动生成）").set_value(run_id)
    _button(at, "创建并后台启动 worker").click().run()
    assert not at.exception, at.exception


def _input(at: AppTest, label: str):
    return next(x for x in at.text_input if x.label == label)


def _open_dashboard(at: AppTest, run_id: str | None = None) -> None:
    at.sidebar.radio[0].set_value("运行看板").run()
    assert not at.exception, at.exception
    if run_id is not None:
        _dashboard_run_select(at).set_value(run_id).run()
        assert not at.exception, at.exception


def _number(at: AppTest, label: str):
    matches = [n for n in at.number_input if n.label == label]
    assert matches, f"页面上没有数值输入 {label!r}，当前：{[n.label for n in at.number_input]}"
    return matches[0]


# --------------------------------------------------------------------- C18：确认表单真实生效


def test_confirm_extend_writes_edited_values_exactly_once(box):
    """改过的追加额度确实落库；一次确认=一条请求；后续纯 rerun 不重复。"""
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    _create_run(at, "r3-fields")
    _settle(box, "r3-fields")
    _open_dashboard(at, "r3-fields")

    _button(at, "追加预算").click().run()
    assert not at.exception, at.exception
    assert _has_button(at, "确认追加"), "点「追加预算」后同一次 rerun 就该出现「确认追加」"

    _number(at, "追加联想").set_value(7).run()
    _number(at, "追加搜索").set_value(5).run()
    assert _number(at, "追加联想").value == 7, "数值输入必须跨 rerun 保留"

    assert _control_rows(box, "r3-fields") == []
    _button(at, "确认追加").click().run()
    assert not at.exception, at.exception

    rows = _control_rows(box, "r3-fields")
    assert len(rows) == 1, f"确认一次应恰好写入一条请求，实际 {len(rows)}"
    assert int(rows[0]["suggest_budget_delta"]) == 7 and int(rows[0]["search_budget_delta"]) == 5, \
        "确认时必须使用用户改过的字段值，而不是组件默认值"

    at.run()  # 确认按钮在它被点的那次 rerun 里仍然渲染；关闭要体现在下一次 rerun
    assert not _has_button(at, "确认追加"), "写入成功后面板应关闭"
    at.run()
    assert len(_control_rows(box, "r3-fields")) == 1, "rerun 不得重复写控制请求"


def test_resume_panel_is_isolated_per_run(box):
    """A run 打开的追加面板不会渲染到 B run 上，也不会替 B 提交任务。"""
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    _create_run(at, "r3-iso-a")
    _settle(box, "r3-iso-a")
    _wait_worker_released(box, "r3-iso-a")
    _input(at, "run_id（留空自动生成）").set_value("r3-iso-b")
    _button(at, "创建并后台启动 worker").click().run()
    assert not at.exception, at.exception
    # 真正后台启动断言：B 的 worker 进程确实被创建（成功回报+启动登记），而不是被
    # "上一个进程尚未确认退出"之类的串行保护正确拒启后，页面只剩一条 warning。
    blocked = [w.value for w in at.warning if "启动" in w.value and "未" in w.value]
    assert not blocked, f"B 的创建被拒启（应等待真实放行条件后再点击）：{blocked}"
    assert support.read_launch_record(_paths(box), "r3-iso-b") is not None, \
        "B 必须有后台启动登记（真实进程创建，不是 mock）"
    assert any("r3-iso-b" in s.value for s in at.success), [s.value for s in at.success]
    _settle(box, "r3-iso-a")
    _settle(box, "r3-iso-b")

    _open_dashboard(at, "r3-iso-a")
    _button(at, "追加预算").click().run()
    assert _has_button(at, "确认追加")

    _dashboard_run_select(at).set_value("r3-iso-b").run()
    assert not _has_button(at, "确认追加"), "切到另一个 run 不得沿用上一个 run 的确认面板"

    _dashboard_run_select(at).set_value("r3-iso-a").run()
    assert _has_button(at, "确认追加"), "切回原 run 后面板仍属于该 run"
    _button(at, "确认追加").click().run()

    assert len(_control_rows(box, "r3-iso-a")) == 1
    assert _control_rows(box, "r3-iso-b") == [], "确认不得串到另一个 run"


def _paused_panel_ready(at: AppTest, box: Path, run_id: str, labels: tuple[str, ...],
                        timeout: float = 90.0) -> None:
    """轮询真实状态：run 真的停在 paused、上一 worker 已释放互斥、看板已渲染且目标按钮
    可点击。预算耗尽会表现为非 paused 状态/按钮 disabled，这里同样不放行——面板未就绪
    时绝不发起点击（不改生产 UI 代码）。"""
    deadline = time.monotonic() + timeout
    while True:
        row = _run_row(box, run_id)
        if row and row["status"] == "paused":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _wait_worker_released(box, run_id, timeout=remaining)
            _open_dashboard(at, run_id)
            buttons = {b.label: b for b in at.button}
            if all(lb in buttons and not buttons[lb].disabled for lb in labels) and not at.exception:
                return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    raise AssertionError(
        f"{run_id} 未在 {timeout}s 内达到可点击的 paused 面板"
        f"（worker 未释放/面板未就绪/预算耗尽；当前行={_run_row(box, run_id)}）"
    )


def test_extend_and_resume_twice_in_one_session(box):
    """同 session 创建的 run 正常退出后：追加→恢复→再追加→再恢复 都要真的跑起来。

    每次点击前轮询真实 paused 状态、等待 worker 互斥释放、确认面板按钮已渲染且可点击，
    并断言预算 revision 已就位——不靠固定 sleep，也不改生产 UI。"""
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    _create_run(at, "r3-rounds")
    _settle(box, "r3-rounds", 1)

    for round_index, min_revision in ((1, 2), (2, 3)):
        _paused_panel_ready(at, box, "r3-rounds", ("追加预算", "恢复（后台 worker）"))
        row = _run_row(box, "r3-rounds")
        assert row["status"] == "paused" and int(row["budget_revision"]) >= round_index, \
            "追加前：paused 真实状态与上一轮预算 revision 必须已生效"

        _button(at, "追加预算").click().run()
        assert not at.exception, at.exception
        assert _has_button(at, "确认追加"), "点「追加预算」后确认面板必须已出现"
        _button(at, "确认追加").click().run()
        assert not at.exception, at.exception
        assert len(_control_rows(box, "r3-rounds")) == round_index
        assert _run_row(box, "r3-rounds")["status"] == "paused"

        _paused_panel_ready(at, box, "r3-rounds", ("恢复（后台 worker）",))
        _button(at, "恢复（后台 worker）").click().run()
        assert _has_button(at, "确认恢复"), "点「恢复（后台 worker）」后应能确认"
        _button(at, "确认恢复").click().run()
        assert not at.exception, at.exception
        at.run()
        assert not _has_button(at, "确认恢复"), "确认恢复成功后面板应关闭"
        _settle(box, "r3-rounds", min_revision)
        _wait_worker_released(box, "r3-rounds")
        assert not _has_button(at, "确认追加"), "恢复后面板不应残留可重复提交的确认按钮"

    applied = _query(
        box,
        "SELECT COUNT(*) AS c FROM control_requests WHERE run_id = ? AND action = 'extend'"
        " AND applied_at IS NOT NULL",
        ("r3-rounds",), one=True,
    )
    assert int(applied["c"]) == 2, "两轮追加都应被 worker 在安全点消费（真链路，不是只写请求）"


# --------------------------------------------------------------------- 停止说明不被分析事件挤掉


def test_pending_child_blocks_another_run_before_worker_lock(box):
    """Two pages must not launch different runs during the child's pre-lock startup gap."""
    paths = _paths(box)
    conn = storage_db.connect(paths.db_path)
    Repository(conn)
    try:
        first = support.plan_worker_launch(
            conn, paths, "pending-a", [sys.executable, "-c", "import time; time.sleep(2)"],
            session_launched=set(), cwd=box)
        assert first["launched"]
        second = support.plan_worker_launch(
            conn, paths, "pending-b", [sys.executable, "-c", "pass"],
            session_launched=set(), cwd=box)
        assert not second["launched"] and second["reason"] == "occupied"
        assert support.read_launch_record(paths, "pending-b") is None
        deadline = time.monotonic() + 10
        while support.last_launch_process_state(paths, "pending-a")[0] == "live":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        third = support.plan_worker_launch(
            conn, paths, "pending-b", [sys.executable, "-c", "pass"],
            session_launched=set(), cwd=box)
        assert third["launched"]
    finally:
        conn.close()


def test_stop_note_survives_flood_of_analysis_events(box):
    """真实 fixture run 停在预算上：写入大量分析事件后，看板仍要显示原始停止说明。"""
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    _create_run(at, "r3-stopnote")
    _settle(box, "r3-stopnote")
    _open_dashboard(at, "r3-stopnote")
    assert _has_button(at, "追加预算"), "paused 的 run 应可追加额度"

    paths = _paths(box)
    conn = storage_db.connect(paths.db_path)
    try:
        repo = Repository(conn, migrate=False)
        before = support.read_dashboard(repo, conn, paths, "r3-stopnote")
        assert before["status"] == "paused"
        assert before["stop_note"] is not None, "看板必须给出最近停止/暂停事件"
        note = before["stop_note"]["detail"].get("note")
        assert note, f"停止事件必须带原始说明：{before['stop_note']}"
        assert before["stop_note"]["kind"] in support.STOP_EVENT_KINDS

        for index in range(400):
            repo.record_event("r3-stopnote", "phrase_skipped",
                              detail={"reason": "ngram_too_long", "phrase": f"合成短语{index}"})

        after = support.read_dashboard(repo, conn, paths, "r3-stopnote")
        assert after["stop_note"]["kind"] == before["stop_note"]["kind"]
        assert after["stop_note"]["detail"].get("note") == note
        assert after["recent_events"] and all(
            e["kind"] not in support.STOP_EVENT_KINDS for e in after["recent_events"]
        ), "本用例必须处于「停止事件已被近期列表挤掉」的场景"
        # 展示口径不变：paused 仍是 paused，不改引擎、不把预算暂停说成收敛
        assert after["status"] == "paused" and after["outcome"] is None
    finally:
        conn.close()
