"""P1-B-LIVE-WIRING-r2：看板 live 入口的字段、标记与来历（真 AppTest + 本机假 CLI）。

覆盖任务要求 6 的 UI 项，并且只离线：

- 创建页只有选了 live 才渲染来源配置（工具绝对路径 / profile / browser / 共享锁目录 / 地域口径 /
  各超时与滚动），能力边界与就绪判定复用 `ui.support.live_readiness` 那一份口径，页面不另写一套。
- 缺配置的 live **不算可用**：就绪说明先报错，点「创建并后台启动 worker」也按配置口径拒绝，
  库里不留 run、假 CLI 一次都不被调用（不伪装成"空结果跑通了"）。
- 配齐后点创建：派生配置与启动命令都带 `--mode live`，真实后台 worker 用固化的工具路径访问
  本机假 CLI，库里的 run 是非合成（synthetic_fixture=0）。
- 看板/导出的来历说明按 run 模式区分：live 的 note_id 是平台返回值，fixture 仍明确写合成值
  （fixture 侧的既有断言在 `tests/test_cli_e2e.py` 里保持不变）。

数据根、锁目录、计划与调用日志全在系统临时目录里，不碰仓库数据根、不碰平台。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

from test_live_cli_pipeline import (  # noqa: F401 - 复用同一套 live 沙箱与假 CLI
    LiveProject,
    happy_steps,
)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "xhs_discovery" / "ui" / "app.py"

LIVE_TEXT_LABELS = (
    "node 可执行文件绝对路径",
    "xhs-search.mjs 绝对路径",
    "profile 名",
    "同 profile 互斥目录（跨数据根共享，不得在 run.data_dir 内）",
    "地域口径 locations（逗号分隔，必填）",
    "歧义种子清单（逗号分隔，留空=保守规则不标歧义）",
)
LIVE_NUMBER_LABELS = (
    "单操作超时（秒）",
    "本进程总超时（秒）",
    "搜索滚动次数",
    "两次外部访问最小间隔（秒）",
)


def _app(project: LiveProject) -> AppTest:
    """打开看板并把配置指向这份 live 沙箱（页面按 cwd 定位数据根，cwd 已由沙箱设好）。"""
    at = AppTest.from_file(str(APP), default_timeout=180).run()
    assert not at.exception, at.exception
    at.sidebar.text_input[0].set_value(str(project.config_path)).run()
    assert not at.exception, at.exception
    return at


def _mode(at: AppTest):
    return next(s for s in at.selectbox if s.label == "模式")


def _input(at: AppTest, label: str):
    matches = [x for x in at.text_input if x.label == label]
    assert matches, f"页面上没有输入框 {label!r}：{[x.label for x in at.text_input]}"
    return matches[0]


def _button(at: AppTest, label: str):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"页面上没有按钮 {label!r}：{[b.label for b in at.button]}"
    return matches[0]


def _texts(at: AppTest) -> str:
    parts: list[str] = []
    for group in (at.markdown, at.caption, at.error, at.success, at.info, at.warning, at.code):
        for item in group:
            parts.append(str(getattr(item, "value", "")))
    return "\n".join(parts)


def _query(project: LiveProject, sql: str, params: tuple) -> list[dict]:
    conn = sqlite3.connect(project.paths.db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _wait_settled(project: LiveProject, run_id: str, timeout: float = 120.0) -> dict:
    """等真实后台 worker 自己停在终态/可恢复态：页面只负责启动，不替它伪造结果。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _query(project, "SELECT status, mode, synthetic_fixture FROM runs WHERE id = ?", (run_id,))
        if rows and rows[0]["status"] in ("paused", "completed", "failed"):
            return rows[0]
        time.sleep(0.1)
    raise AssertionError(f"{run_id} 未在 {timeout}s 内停下")


# --------------------------------------------------------------------- 创建页：字段与边界


def _live_page_labels(at: AppTest) -> list[str]:
    return [x.label for x in at.text_input] + [n.label for n in at.number_input]


def test_live_fields_and_boundaries_only_appear_in_live_mode(tmp_path, monkeypatch):
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=1, search_attempts=0)
    at = _app(project)

    # 这份沙箱配置本身就是 live：先切回 fixture，确认 live 字段是"选了才给"，不是常驻
    _mode(at).set_value("fixture").run()
    assert not at.exception, at.exception
    labels = _live_page_labels(at)
    for label in (*LIVE_TEXT_LABELS, *LIVE_NUMBER_LABELS):
        assert label not in labels, f"fixture 模式不该渲染 {label}"
    # 全站说明两种模式都提一句（这是必要的），但来源配置与就绪口径只在 live 下出现
    assert "同 profile 互斥" not in _texts(at), "没选 live 就不要摆 live 就绪说明"

    _mode(at).set_value("live").run()
    assert not at.exception, at.exception
    labels = _live_page_labels(at)
    for label in (*LIVE_TEXT_LABELS, *LIVE_NUMBER_LABELS):
        assert label in labels, f"live 缺少配置项输入：{label}"
    assert "browser" in [s.label for s in at.selectbox], "browser 必须可选（默认 msedge）"
    texts = _texts(at)
    # 能力边界与就绪口径来自 ui.support.live_readiness（页面不另写一套判断）
    for phrase in ("title_only", "点赞", "不读凭据", "同 profile 互斥"):
        assert phrase in texts, f"live 边界说明缺少 {phrase!r}"
    assert "live 配置完备" in texts, texts[:600]

    _mode(at).set_value("fixture").run()
    assert "node 可执行文件绝对路径" not in [x.label for x in at.text_input]


def test_incomplete_live_config_is_refused_as_config_not_capability(tmp_path, monkeypatch):
    """锁目录留空=配置不完备：就绪说明先报错，点创建也不建 run、不碰 CLI。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=1, search_attempts=0)
    at = _app(project)
    _mode(at).set_value("live").run()
    _input(at, "同 profile 互斥目录（跨数据根共享，不得在 run.data_dir 内）").set_value("").run()
    assert not at.exception, at.exception
    assert "不完备" in _texts(at)

    _input(at, "run_id（留空自动生成）").set_value("ui-live-incomplete").run()
    _button(at, "创建并后台启动 worker").click().run()
    assert not at.exception, at.exception
    assert "配置非法（未启动）" in _texts(at)
    assert _query(project, "SELECT id FROM runs WHERE id = ?", ("ui-live-incomplete",)) == []
    assert project.call_count() == 0, "缺配置的 live 绝不能走到真实工具"


# --------------------------------------------------------------------- 创建页：真启动与来历


def test_complete_live_config_creates_non_synthetic_run(tmp_path, monkeypatch):
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=1, search_attempts=0)
    at = _app(project)
    _mode(at).set_value("live").run()
    _input(at, "run_id（留空自动生成）").set_value("ui-live-run").run()
    _button(at, "创建并后台启动 worker").click().run()
    assert not at.exception, at.exception

    command = _texts(at)
    assert "--mode live" in command, "启动命令必须带上用户选的 live，而不是配置的默认模式"
    row = _wait_settled(project, "ui-live-run")
    assert row["mode"] == "live" and int(row["synthetic_fixture"]) == 0, row
    assert project.call_count() >= 1, "worker 应真的用上固化的本机工具（这里是假 CLI）"
    call = project.calls()[0]
    assert call["interpreter"] == sys.executable
    assert call["invoked_as"] == [str(project.fake_cli)]

    derived = _query(project, "SELECT config_json FROM runs WHERE id = ?", ("ui-live-run",))
    payload = json.loads(derived[0]["config_json"])
    assert payload["run"]["mode"] == "live"
    assert payload["sources"]["xhs_search"]["lock_dir"] == str(project.lock_dir)


def test_dashboard_and_export_label_live_provenance(tmp_path, monkeypatch):
    """同一份说明在 live 批次里写"平台返回值"，不再一律扣在合成 fixture 名下。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=1, search_attempts=1)
    run_id = project.execute()["run_id"]  # 探索→分析→导出一条完整 live 批次（离线假 CLI）
    at = _app(project)
    at.sidebar.radio[0].set_value("导出").run()
    assert not at.exception, at.exception
    next(s for s in at.selectbox if s.label == "选择 run").set_value(run_id).run()
    assert not at.exception, at.exception
    snapshots = [s for s in at.selectbox if s.label.startswith("批次")]
    assert snapshots, "live run 完成后导出页必须能选到批次"
    snapshots[0].set_value(snapshots[0].options[-1]).run()
    assert not at.exception, at.exception
    texts = _texts(at)
    assert "本机 CLI 从平台返回的值" in texts, texts[:800]
    assert "本批次 note_id / access_url 均为合成" not in texts
    assert "合成数据集离线运行" not in texts, "live 批次不得按 fixture 口径说明来历"
    assert "verification.mode=`fixed_snapshot`" in texts, "导出页仍走固定批次完整性校验"
