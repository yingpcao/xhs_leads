"""P0-E-CLI-r1：真实 CLI 子进程 + 临时数据根的端到端验收。

这里只调用 `python -m xhs_discovery`（不 import 内部装配来"替它"跑），因此覆盖的是
用户实际会得到的行为：fixture 全跑通、五类产物可反读、同一 run 可控制、导出读固定批次，
以及各类非法输入的退出码。不依赖平台、登录、模型权重下载。
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from xhs_discovery._version import ANALYSIS_VERSION
from xhs_discovery.analysis.export import COLUMNS_BY_FILENAME
from xhs_discovery.app import EXIT_STATE_CONFLICT
from xhs_discovery.cli import (
    EXIT_CONFIG,
    EXIT_MISSING,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_USAGE,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = ROOT / "config" / "offline-demo.yaml"
EXPORT_NAMES = ("keywords.csv", "needs.csv", "evidence.csv", "graph.graphml", "report.json")

# 持有 worker 锁的脚本：等外部放行后，才在"安全点"消费队列里的控制请求
HOLDER = """
import json, sys, time
from pathlib import Path
from xhs_discovery import app
from xhs_discovery.storage.worker import consume_all_controls

cfg, run_id, ready, release = sys.argv[1:5]
repo, paths, _ = app.open_store(cfg, Path.cwd())
conn, comp = app.build_components(repo, paths, run_id)
comp.control.acquire()
Path(ready).write_text(json.dumps({"acquired": True}), encoding="utf-8")
while not Path(release).exists():
    time.sleep(0.05)
applied = consume_all_controls(conn, run_id, ledger=comp.engine.ledger)
comp.control.release()
print("HOLDER " + json.dumps(applied, ensure_ascii=False, default=str))
"""

# 完整跑一遍并回报子进程里是否引入模型依赖
PROBE = """
import json, sys
from xhs_discovery.cli import main
rc = main(sys.argv[1:])
loaded = [m for m in ("torch", "transformers", "sentence_transformers", "modelscope")
          if m in sys.modules]
print("PROBE " + json.dumps({"rc": rc, "models": loaded}, ensure_ascii=False))
sys.exit(rc)
"""


# 在子进程里执行 UI 页面脚本：不把 streamlit 带进父进程，也不碰用户的运行数据库。
# 首页（创建任务）渲染一次，再切到"运行看板"确认能选到已有 run；两次都不许触发重算。
UI_PAGE_PROBE = """
import json, os, sys
from pathlib import Path
from streamlit.testing.v1 import AppTest

project, entry, run_id = sys.argv[1:4]
os.chdir(project)
script = AppTest.from_file(entry, default_timeout=180)
script.run()
first_texts = " ".join(
    str(item.value) for group in (script.title, script.subheader, script.caption) for item in group
)
if script.radio:
    script.radio[0].set_value("运行看板")
    script.run()
options = [str(opt) for box in script.selectbox for opt in (box.options or [])]
dash_texts = " ".join(
    str(item.value) for group in (script.markdown, script.caption) for item in group
)
print("UIPAGE " + json.dumps({
    "exception": [str(getattr(e, "value", e)) for e in script.exception],
    "listed": run_id in options,
    "first_texts": first_texts,
    "dash_texts": dash_texts,
}, ensure_ascii=False))
"""


def _write_config(target: Path, name: str, *, suggest: int, search: int, calibration: int) -> str:
    text = DEMO_YAML.read_text(encoding="utf-8")
    small = (
        text.replace("suggest_attempts: 14", f"suggest_attempts: {suggest}")
        .replace("search_attempts: 6", f"search_attempts: {search}")
        .replace("calibration_suggest_attempts: 3", f"calibration_suggest_attempts: {calibration}")
    )
    (target / "config" / name).write_text(small, encoding="utf-8")
    return f"config/{name}"


def _make_sandbox(target: Path) -> Path:
    """临时项目根：只复制配置与合成数据集，数据根落在 tmp 下，绝不碰用户运行库。"""
    (target / "config").mkdir(parents=True, exist_ok=True)
    (target / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
    for rel in ("config/domain_terms.txt", "config/intent_rules.yaml"):
        shutil.copyfile(ROOT / rel, target / rel)
    shutil.copyfile(
        ROOT / "tests" / "fixtures" / "shanghai_zhongkao.json",
        target / "tests" / "fixtures" / "shanghai_zhongkao.json",
    )
    _write_config(target, "small.yaml", suggest=6, search=3, calibration=1)
    _write_config(target, "tiny.yaml", suggest=4, search=2, calibration=1)
    return target


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory) -> Path:
    return _make_sandbox(tmp_path_factory.mktemp("ecli_sandbox"))


def cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "xhs_discovery", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
    )


def cli_code(cwd: Path, probe: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", probe, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
    )


def db(sandbox: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sandbox / "data" / "xhs_discovery.db")
    conn.row_factory = sqlite3.Row
    return conn


def run_status(sandbox: Path, run_id: str) -> str:
    row = db(sandbox).execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    return str(row["status"]) if row else ""


def batch_dirs(sandbox: Path, run_id: str) -> list[Path]:
    return sorted((sandbox / "data" / "runs" / run_id / "exports").glob("*/*/"))


def succeeded_run_id(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == EXIT_OK, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)["run_id"]


@pytest.fixture(scope="module")
def full_run(sandbox: Path) -> dict:
    """跑一次完整 fixture 任务：run→（同一临界区）分析→导出，并回报子进程模型依赖。"""
    probe = cli_code(
        sandbox, PROBE, "run", "--mode", "fixture", "--seed", "上海中考",
        "--config", "config/small.yaml", "--run", "e2e-full",
    )
    assert probe.returncode == EXIT_OK, f"rc={probe.returncode}\n{probe.stdout}\n{probe.stderr}"
    payload_text, marker, _rest = probe.stdout.rpartition("PROBE ")
    assert marker, f"子进程没有回报模型依赖：{probe.stdout}"
    batch = json.loads(payload_text)
    models = json.loads(_rest)["models"]
    return {"run_id": batch["run_id"], "batch": batch["batch"], "models": models}


# ------------------------------------------------------------------ 全跑通与产物反读


def test_full_run_never_imports_model_libraries(full_run: dict):
    """无模型全流程：不加载 torch/transformers 之类权重依赖，也不因缺模型降级成失败。"""
    assert full_run["models"] == []
    assert full_run["batch"]["verification_ok"] is True


def test_five_artifacts_are_reverse_readable(full_run: dict, sandbox: Path):
    """五类产物齐全且能被独立反读：CSV 表头与导出契约一致，图可解析，报告口径可核对。"""
    dirs = batch_dirs(sandbox, full_run["run_id"])
    assert dirs, "run 目录里没有导出批次"
    latest = dirs[-1]
    present = {p.name for p in latest.iterdir()}
    assert present >= set(EXPORT_NAMES), f"缺少产物：{set(EXPORT_NAMES) - present}"

    for filename, columns in COLUMNS_BY_FILENAME.items():
        with (latest / filename).open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == list(columns), f"{filename} 表头与导出契约不一致"
            rows = list(reader)
        assert rows, f"{filename} 没有可反读的行"
        if filename == "needs.csv":
            assert any(row["canonical_text"] for row in rows), "needs.csv 必须有可展示的需求表述"
        if filename == "evidence.csv":
            assert any(row["locatable"] in ("true", "1") for row in rows), "证据必须可按字符区间定位"

    tree = ElementTree.parse(latest / "graph.graphml")
    namespace = "{http://graphml.graphdrawing.org/xmlns}"
    nodes = tree.getroot().iter(f"{namespace}node")
    edges = list(tree.getroot().iter(f"{namespace}edge"))
    assert len(list(nodes)) > 0 and len(edges) > 0, "图谱必须有节点与边"

    report = json.loads((latest / "report.json").read_text(encoding="utf-8"))
    assert set(report) >= {"meta", "run", "config", "budget", "statistics", "analysis", "graph"}
    assert report["meta"]["analysis_version"] == ANALYSIS_VERSION
    assert report["run"]["id"] == full_run["run_id"]
    assert report["run"]["seed_raw"] == "上海中考", "种子原文不得被改写"
    assert report["run"]["mode"] == "fixture"
    assert report["config"]["snapshot"]["budget"]["suggest_attempts"] == 6, "报告必须用 run 创建时的配置快照"


def test_run_dir_persists_source_snapshot_and_versions(full_run: dict, sandbox: Path):
    """run 目录固化数据集/词典/规则/配置的字节副本与摘要，恢复不靠当前全局文件。"""
    source = sandbox / "data" / "runs" / full_run["run_id"] / "source"
    meta = json.loads((source / "snapshot.json").read_text(encoding="utf-8"))
    assert (source / f"{meta['fixture']['name']}.json").is_file()
    assert (source / "domain_terms.txt").is_file() and (source / "intent_rules.yaml").is_file()
    assert meta["fixture"]["regions"], "数据集地域必须被登记进快照"
    assert meta["versions"]["analysis"] == ANALYSIS_VERSION
    assert meta["config_hash"]

    row = db(sandbox).execute("SELECT config_json FROM runs WHERE id = ?", (full_run["run_id"],)).fetchone()
    stored = json.loads(str(row["config_json"]))
    assert stored["sources"]["fixture"]["dataset"] == meta["fixture"]["name"]
    assert stored["run"]["data_dir"] == "data"
    assert stored["budget"]["suggest_attempts"] == 6, "库里存的必须是本次 run 的配置快照，不是当前全局文件"

    from xhs_discovery import app
    from xhs_discovery.paths import RuntimePaths

    paths = RuntimePaths(sandbox / "data")
    reloaded, _ = app.load_source_snapshot(paths, full_run["run_id"])
    assert reloaded["fixture"]["sha256"] == meta["fixture"]["sha256"]


def test_ledger_queue_and_dataset_regions_reach_analysis(full_run: dict, sandbox: Path):
    """账本/队列可查询，且数据集里的地域规则真的进到分析结果（不是只用当前词典）。"""
    conn = db(sandbox)
    ledger_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM budget_ledger WHERE run_id = ?", (full_run["run_id"],)
    ).fetchone()["c"]
    operations = conn.execute(
        "SELECT COUNT(*) AS c FROM operations WHERE run_id = ?", (full_run["run_id"],)
    ).fetchone()["c"]
    frontier = conn.execute(
        "SELECT COUNT(*) AS c FROM frontier WHERE run_id = ?", (full_run["run_id"],)
    ).fetchone()["c"]
    assert ledger_rows > 0 and operations > 0
    suggest_charge = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(consumed_units), 0) AS units FROM budget_ledger"
        " WHERE run_id = ? AND external_access = 1 AND budget_key = 'suggest_attempts'",
        (full_run["run_id"],),
    ).fetchone()
    assert suggest_charge["c"] > 0, "每次外部访问都要单独记账"
    assert float(suggest_charge["units"]) <= 6, "消耗不得超过 run 创建时快照里的预算上限"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM budget_ledger WHERE run_id = ? AND external_access = 1"
        " AND operation_id IS NULL",
        (full_run["run_id"],),
    ).fetchone()["c"] == 0, "外部访问消耗必须能追溯到具体操作"

    located = conn.execute(
        "SELECT COUNT(*) AS c FROM problems WHERE run_id = ? AND location IS NOT NULL AND location <> ''",
        (full_run["run_id"],),
    ).fetchone()["c"]
    assert located > 0, "数据集地域规则没有进入问题抽取"
    assert frontier > 0, "探索队列必须可查询，恢复时才知道还剩什么"


# ------------------------------------------------------------------ 批次、导出与不重算


def test_export_reads_fixed_batch_and_does_not_recompute(full_run: dict, sandbox: Path):
    """export 只读已登记的固定批次：两次调用给同一批次，不新增分析事件、不覆盖原件。"""
    conn = db(sandbox)
    before = conn.execute(
        "SELECT COUNT(*) AS c FROM run_events WHERE run_id = ? AND kind = 'analysis_export'",
        (full_run["run_id"],),
    ).fetchone()["c"]

    first = cli(sandbox, "export", "--run", full_run["run_id"], "--config", "config/small.yaml")
    assert first.returncode == EXIT_OK, first.stderr
    payload = json.loads(first.stdout)
    assert payload["batch"] == full_run["batch"]["batch"]
    assert payload["verification"]["ok"] is True
    assert payload["verification"]["mode"] == "fixed_snapshot", "导出必须是读固定快照，不是按当前库重算"

    out = sandbox / "delivered"
    second = cli(
        sandbox, "export", "--run", full_run["run_id"], "--config", "config/small.yaml",
        "--out", str(out),
    )
    assert second.returncode == EXIT_OK, second.stderr
    assert {p.name for p in out.iterdir()} == set(EXPORT_NAMES)
    registered = batch_dirs(sandbox, full_run["run_id"])[-1]
    for name in EXPORT_NAMES:
        assert (out / name).read_bytes() == (registered / name).read_bytes()

    after = db(sandbox).execute(
        "SELECT COUNT(*) AS c FROM run_events WHERE run_id = ? AND kind = 'analysis_export'",
        (full_run["run_id"],),
    ).fetchone()["c"]
    assert after == before, "export 触发了重算：它必须只读固定批次"
    assert run_status(sandbox, full_run["run_id"]) in ("paused", "completed", "running")


def test_export_refuses_tampered_registered_artifact(full_run: dict, sandbox: Path):
    """登记产物被改动就不能当作有效导出：内容寻址的哈希校验必须拦住。"""
    registered = batch_dirs(sandbox, full_run["run_id"])[-1] / "keywords.csv"
    original = registered.read_bytes()
    try:
        registered.write_bytes(original + "x,篡改一行\n".encode("utf-8"))
        proc = cli(sandbox, "export", "--run", full_run["run_id"], "--config", "config/small.yaml")
        assert proc.returncode == EXIT_CONFIG
        assert "哈希不符" in proc.stderr or "拒绝" in proc.stderr
    finally:
        registered.write_bytes(original)
    assert cli(sandbox, "export", "--run", full_run["run_id"], "--config", "config/small.yaml").returncode == EXIT_OK


def test_resume_refuses_when_source_snapshot_drifted(full_run: dict, sandbox: Path):
    """来源副本被改动 → 恢复明确拒绝（65），不悄悄按当前全局配置换口径。"""
    source = sandbox / "data" / "runs" / full_run["run_id"] / "source" / "domain_terms.txt"
    original = source.read_bytes()
    try:
        source.write_bytes(original + "\n被改写的地域\n".encode("utf-8"))
        proc = cli(sandbox, "resume", "--run", full_run["run_id"], "--config", "config/small.yaml")
        assert proc.returncode == EXIT_CONFIG, proc.stdout + proc.stderr
        assert "悄悄换口径" in proc.stderr or "副本被改动" in proc.stderr
    finally:
        source.write_bytes(original)


# ------------------------------------------------------------------ 同一 run 的控制


@pytest.fixture(scope="module")
def controllable_run(sandbox: Path) -> str:
    """耗尽到 paused → extend 加量 → resume 只跑一个槽位，留下"有队列、状态 running"的 run。

    文件内后续测试按此顺序使用：先看额度与批次，再做持锁 worker 的互斥与暂停。
    """
    proc = cli(
        sandbox, "run", "--mode", "fixture", "--seed", "上海中考",
        "--config", "config/tiny.yaml", "--run", "e2e-ctl",
    )
    run_id = succeeded_run_id(proc)
    assert run_status(sandbox, run_id) == "paused", "小预算必须停在预算耗尽的可恢复点"

    ext = cli(
        sandbox, "extend", "--run", run_id, "--config", "config/tiny.yaml", "--suggest-budget", "4"
    )
    assert ext.returncode == EXIT_OK, ext.stderr
    res = cli(
        sandbox, "resume", "--run", run_id, "--config", "config/tiny.yaml", "--max-slots", "1"
    )
    succeeded_run_id(res)
    return run_id


def test_extend_adds_quota_without_resetting_and_resume_keeps_one_trace(controllable_run: str, sandbox: Path):
    """extend 只加额度（已花掉的不清零），resume 续跑同一账本并产生新批次而保留旧批次。"""
    conn = db(sandbox)
    suggest_used = conn.execute(
        "SELECT COALESCE(SUM(consumed_units), 0) AS total FROM budget_ledger"
        " WHERE run_id = ? AND budget_key = 'suggest_attempts'",
        (controllable_run,),
    ).fetchone()["total"]
    assert float(suggest_used) > 1, "恢复后必须继续累加同一笔账，而不是重新起账本"

    revisions = [
        json.loads(str(r["added_json"]))
        for r in conn.execute("SELECT added_json FROM budget_revisions WHERE run_id = ?",
                              (controllable_run,)).fetchall()
    ]
    assert revisions and revisions[0]["suggest_attempts"] == 4.0, revisions
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM control_requests WHERE run_id = ? AND action = 'extend'"
        " AND applied_at IS NOT NULL",
        (controllable_run,),
    ).fetchone()["c"] == 1

    batches = conn.execute(
        "SELECT id, detail_json FROM run_events WHERE run_id = ? AND kind = 'analysis_export' ORDER BY id",
        (controllable_run,),
    ).fetchall()
    assert len(batches) >= 2, "新观察必须形成新批次，旧批次结论保留"
    snapshots = [json.loads(str(b["detail_json"]))["snapshot"] for b in batches]
    assert len(set(snapshots)) == len(snapshots), f"批次标识不得重复：{snapshots}"
    assert all(s != ANALYSIS_VERSION for s in snapshots), "批次标识与算法版本是两回事"
    dirs = [d.name for d in batch_dirs(sandbox, controllable_run)]
    assert all(any(s.startswith(tag) for s in dirs) for tag in snapshots)


def test_duplicate_worker_is_refused_and_pause_applies_at_safe_point(controllable_run: str, sandbox: Path):
    """活跃 worker 存在时：第二个 worker 拒绝（67）且不写控制请求；pause 只在安全点生效。"""
    ready = sandbox / "holder.ready"
    release = sandbox / "holder.release"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, "config/tiny.yaml", controllable_run, str(ready), str(release)],
        cwd=sandbox, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        deadline = time.time() + 120
        while not ready.exists():
            if time.time() > deadline or holder.poll() is not None:
                pytest.fail(f"worker 未能持锁：{holder.communicate(timeout=10)[0] if holder.poll() else '超时'}")
            time.sleep(0.05)

        dup = cli(sandbox, "resume", "--run", controllable_run, "--config", "config/tiny.yaml")
        assert dup.returncode == EXIT_STATE_CONFLICT, dup.stdout + dup.stderr
        assert "第二个 worker" in dup.stderr or "活跃 worker" in dup.stderr
        conn = db(sandbox)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM control_requests WHERE run_id = ? AND applied_at IS NULL",
            (controllable_run,),
        ).fetchone()["c"] == 0, "被拒绝的 resume 不该留下待消费请求"

        pause = cli(sandbox, "pause", "--run", controllable_run, "--config", "config/tiny.yaml")
        assert pause.returncode == EXIT_OK, pause.stderr
        status_while_running = run_status(sandbox, controllable_run)
        assert status_while_running == "running", "CLI 不得直接把状态改成 paused"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM control_requests WHERE run_id = ? AND action = 'pause'"
            " AND applied_at IS NULL",
            (controllable_run,),
        ).fetchone()["c"] == 1

        # 导出与活跃 worker 冲突时读固定快照：不重算、不改写批次
        export = cli(sandbox, "export", "--run", controllable_run, "--config", "config/tiny.yaml")
        assert export.returncode == EXIT_OK, export.stderr

        release.touch()
        stdout, _ = holder.communicate(timeout=120)
        applied = json.loads(stdout.splitlines()[-1].split("HOLDER ", 1)[1])
    finally:
        if holder.poll() is None:
            holder.kill()
    actions = [item["action"] for item in applied]
    assert "pause" in actions, f"暂停请求必须由 worker 在安全点消费：{applied}"
    assert all(item["applied_at"] for item in applied)
    assert run_status(sandbox, controllable_run) == "paused"


def test_extend_without_delta_is_usage_error(controllable_run: str, sandbox: Path):
    """extend 不给出正数增量就是用法错误（2），不是把额度重置。"""
    proc = cli(sandbox, "extend", "--run", controllable_run, "--config", "config/tiny.yaml")
    assert proc.returncode == EXIT_USAGE
    assert "追加" in proc.stderr


# ------------------------------------------------------------------ 非法输入与退出码


@pytest.mark.parametrize(
    "args, code, needle",
    [
        (("run", "--mode", "live", "--seed", "上海中考", "--config", "config/small.yaml"),
         EXIT_CONFIG, "live"),
        (("run", "--mode", "fixture", "--seed", "上海中考", "--config", "config/small.yaml",
          "--run", "e2e-full"), EXIT_STATE_CONFLICT, "已存在"),
        (("export", "--run", "../evil", "--config", "config/small.yaml"), EXIT_USAGE, "run_id"),
        (("export", "--run", "a/b", "--config", "config/small.yaml"), EXIT_USAGE, "run_id"),
        (("pause", "--run", "no-such-run", "--config", "config/small.yaml"), EXIT_MISSING, "不存在"),
        (("resume", "--run", "no-such-run", "--config", "config/small.yaml"), EXIT_MISSING, "不存在"),
        (("extend", "--run", "no-such-run", "--config", "config/small.yaml", "--suggest-budget", "1"),
         EXIT_MISSING, "不存在"),
        (("export", "--run", "e2e-full", "--config", "config/small.yaml", "--snapshot", "b99-x"),
         EXIT_MISSING, "批次"),
        (("config-check", "--config", "config/missing.yaml"), EXIT_CONFIG, "不存在"),
    ],
    ids=["live-missing-config", "duplicate-run-id", "traversal-run-id", "slash-run-id", "missing-pause",
         "missing-resume", "missing-extend", "unknown-batch", "missing-config"],
)
def test_cli_error_exit_codes(sandbox: Path, args: tuple[str, ...], code: int, needle: str):
    proc = cli(sandbox, *args)
    assert proc.returncode == code, f"{args} → {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert needle in proc.stderr, proc.stderr


def test_unwritable_data_root_is_refused_with_nonzero_code(tmp_path: Path):
    """数据根无法建库（被占用/无权限一类故障）：明确报错并非零退出，不静默换到别处写。"""
    project = _make_sandbox(tmp_path / "project")
    (project / "data").write_text("我是一个文件，不是目录", encoding="utf-8")
    proc = cli(project, "run", "--mode", "fixture", "--seed", "上海中考",
               "--config", "config/small.yaml", "--run", "blocked-run")
    assert proc.returncode == EXIT_MISSING, proc.stdout + proc.stderr
    assert "数据库" in proc.stderr or "数据文件" in proc.stderr, proc.stderr
    assert not (project / "runs").exists(), "不得把产物偷偷写到数据根之外"


# ------------------------------------------------------------------ UI 入口（不伪装看板）


def test_ui_dry_run_reports_the_real_launch_command(sandbox: Path):
    """`ui` 提供真实 Streamlit 启动命令；看板本体（P0-31～P0-34）已交付五类离线页面。"""
    proc = cli(sandbox, "ui", "--dry-run")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["launched"] is False
    assert payload["command"][:3] == [sys.executable, "-m", "streamlit"]
    entry = Path(payload["entry"])
    assert entry.is_file()
    body = entry.read_text(encoding="utf-8")
    for page in ("创建任务", "运行看板", "关键词发现图", "需求图", "导出"):
        assert page in body, f"看板入口必须交付五类页面：缺少 {page}"
    assert "--server.address" in payload["command"], "默认必须显式绑定回环地址，不暴露局域网/公网"


def test_ui_entry_module_compiles_without_launching_server():
    entry = ROOT / "src" / "xhs_discovery" / "ui" / "app.py"
    proc = subprocess.run([sys.executable, "-m", "py_compile", str(entry)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_ui_page_lists_finished_runs_and_does_not_recompute(tmp_path: Path):
    """UI 页面真跑得起来：首页明确只离线；看板能选到已有 run；两次渲染都不启动 worker、不重算。

    在子进程里用 Streamlit 自带的 AppTest 执行页面脚本：一是避免把 streamlit 导入到
    父进程而破坏 `doctor` 的"轻量导入"契约，二是页面按 cwd 找配置，必须在临时项目根里跑，
    绝不连用户的运行数据库。
    """
    project = _make_sandbox(tmp_path / "ui_project")
    proc = cli(project, "run", "--mode", "fixture", "--seed", "上海中考",
               "--config", "config/small.yaml", "--run", "ui-page-run")
    run_id = succeeded_run_id(proc)
    shutil.copyfile(project / "config" / "small.yaml", project / "config" / "default.yaml")

    def batches() -> int:
        conn = db(project)
        value = conn.execute("SELECT COUNT(*) AS c FROM run_events WHERE kind = 'analysis_export'").fetchone()["c"]
        conn.close()
        return value

    before = batches()
    probe = subprocess.run(
        [sys.executable, "-c", UI_PAGE_PROBE, str(project),
         str(ROOT / "src" / "xhs_discovery" / "ui" / "app.py"), run_id],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=project,
    )
    line = next((l for l in probe.stdout.splitlines() if l.startswith("UIPAGE ")), None)
    assert line, probe.stdout + probe.stderr
    report = json.loads(line[len("UIPAGE ") :])
    assert report["exception"] == [], report["exception"]
    assert report["listed"], f"运行看板必须能选到已有 run：{report}"
    assert "合成" in report["first_texts"] or "fixture" in report["first_texts"].lower(), \
        f"首页要交代数据只来自合成 fixture：{report['first_texts'][:120]}"
    assert batches() == before, "打开页面不得触发任何重算或新批次"
