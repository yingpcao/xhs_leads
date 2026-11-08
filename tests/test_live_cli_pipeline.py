"""P1-B live 接线自测：真实 `CliSearchSource` 接进同一条 run/账本/worker/分析/导出。

全部离线：数据源是**已验收的真实适配器**，子进程是本机的一份假 CLI
（`tests/fixtures/cli_protocol/fake_pipeline_cli.py`，用 `sys.executable` 起，绝不联网、
不读凭据、不碰真实平台）。这里验证的是"接线"本身：

- 一次 live run 走完 探索→分析→导出→追加额度→恢复，产物与批次都能读回来；
- 数据源交回的原始文件按 operation/attempt 校验哈希与归属后入库，恢复只补本 run 本 attempt 的
  可验证结果，且不重复扣费、不重复入库；
- 快照固化"是哪一份工具"，工具被改动或协议版本换掉时明确拒绝恢复，不悄悄换口径；
- 真实故障（认证/验证/profile 占用/协议漂移/空 stdout/带阻断错误的 partial）一律停在 paused，
  不烧重试、不发布数据、但保留已拿到的证据与原始响应；
- live 的退避真的等时间、重放真的不再起子进程、同 profile 跨数据根互斥真的挡住第二个浏览器。

与 `tests/test_source_cli.py`（协议解释的严格性）和 `tests/test_engine_worker.py`（记账纪律）
分工：那两份不打开 live run，本份不重测协议细节，只测 P1-B 的接线与失败形状。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from xhs_discovery import app
from xhs_discovery.config import load_settings
from xhs_discovery.discovery.engine import REPLAY_NOTE, DiscoveryEngine, prepare_run
from xhs_discovery.models.common import SessionRef, utc_now
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    ErrorCategory,
    PauseReason,
    ResultState,
    RunStatus,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.source import (
    Capabilities,
    OperationContext,
    PostSearchResult,
    SuggestionResult,
)
from xhs_discovery.sources import cli as sources_cli
from xhs_discovery.sources.base import SearchSource
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.artifacts import import_unregistered, recover_run
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import OsGuard, WorkerControl

ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI_SOURCE = ROOT / "tests" / "fixtures" / "cli_protocol" / "fake_pipeline_cli.py"
BASE_CONFIG = ROOT / "config" / "tests-convergence-tiny.yaml"
DOMAIN_TERMS = ("中考", "志愿", "名额分配", "建档线")
SEED = "上海中考"
CHILD = "上海中考分数线"
TITLE_A = "上海中考志愿到底怎么排？求过来人经验"
TITLE_B = "上海中考建档线附近还有书读吗"


def card(note_id: str, title: str | None, *, likes: str | None = "312") -> dict:
    """搜索卡片：id/url 是平台返回值，发布日期与点赞经常缺（live 的真实形状）。"""
    return {
        "id": note_id,
        "title": title,
        "url": f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=SIG{note_id}",
        "author": "过来人家长",
        "authorUrl": "https://www.xiaohongshu.com/user/profile/u-" + note_id,
        "likesText": likes,
        "publishedText": None,
        "coverUrl": None,
    }


def happy_steps() -> list[dict]:
    return [
        {"command": "suggest", "seed": SEED, "returncode": 0, "suggestions": [CHILD]},
        {"command": "suggest", "returncode": 0, "suggestions": [], "explicit_empty": True,
         "stopReason": "empty"},
        {"command": "search", "returncode": 0, "notes": [card("live-a", TITLE_A),
                                                         card("live-b", TITLE_B, likes="1.2万")],
         "stopReason": "limit"},
    ]


class LiveProject:
    """一份隔离的 live 配置 + 本机假 CLI：数据根、锁目录、计划、调用日志都在 tmp 里。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: list[dict], **tweaks) -> None:
        self.cwd = tmp_path / "project"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.lock_dir = tweaks.pop("lock_dir", None) or (tmp_path / "machine-locks")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.fake_cli = self.cwd / "fake_xhs_search.py"
        shutil.copyfile(FAKE_CLI_SOURCE, self.fake_cli)  # 副本归本测试所有：漂移测试要改的就是它
        self.plan_path = tmp_path / "plan.json"
        self.state_path = tmp_path / "call_state.json"
        self.log_path = tmp_path / "calls.jsonl"
        self.write_plan(steps)
        monkeypatch.setenv("XHS_FAKE_PLAN", str(self.plan_path))
        monkeypatch.setenv("XHS_FAKE_STATE", str(self.state_path))
        monkeypatch.setenv("XHS_FAKE_LOG", str(self.log_path))

        data = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
        profile = tweaks.pop("profile", "default")
        run_label = tweaks.pop("run_label", "live-p1b")
        suggest_attempts = tweaks.pop("suggest_attempts", 2)
        search_attempts = tweaks.pop("search_attempts", 2)
        max_posts_stored = tweaks.pop("max_posts_stored", 3)
        card_limit = tweaks.pop("card_limit", 1)
        max_retries = tweaks.pop("max_retries", 1)
        backoff_base_seconds = tweaks.pop("backoff_base_seconds", 0.0)
        operators = tweaks.pop("operators", ["suggest_vertical"])
        min_interval_seconds = tweaks.pop("min_interval_seconds", 1.0)
        search_scrolls = tweaks.pop("search_scrolls", 0)

        data["run"].update({"mode": "live", "seed_raw": SEED, "data_dir": "live-data",
                            "run_label": run_label})
        data["budget"].update({"suggest_attempts": suggest_attempts,
                               "search_attempts": search_attempts,
                               "body_attempts": 0, "author_attempts": 0,
                               "calibration_suggest_attempts": 0,
                               "max_posts_stored": max_posts_stored,
                               "max_runtime_minutes": 5})
        data["sampling"].update({"high_value_posts": card_limit,
                                 "medium_value_posts": card_limit,
                                 "low_value_posts": card_limit})
        data["retry"].update({"max_retries": max_retries,
                              "backoff_base_seconds": backoff_base_seconds,
                              "engine_min_interval_seconds": 0.0})
        data["scheduler"].update({"operators": operators})
        data["nlp"].update({"domain_terms_path": str(ROOT / "config" / "domain_terms.txt"),
                            "intent_rules_path": str(ROOT / "config" / "intent_rules.yaml")})
        data.setdefault("sources", {})["xhs_search"] = {
            "node_path": sys.executable,
            "cli_path": str(self.fake_cli),
            "profile": profile,
            "browser": "msedge",
            "timeout_seconds": 5,
            "total_process_timeout_seconds": 10,
            "min_interval_seconds": min_interval_seconds,
            "search_scrolls": search_scrolls,
            "lock_dir": str(self.lock_dir),
            "locations": ["上海"],
            "ambiguous_seeds": [],
        }
        if tweaks:
            raise AssertionError(f"未知的配置覆盖项：{sorted(tweaks)}")
        self.config_path = tmp_path / "live.yaml"
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                    encoding="utf-8")
        monkeypatch.chdir(self.cwd)
        self.settings = load_settings(self.config_path)
        self.paths = app.runtime_paths(self.settings, self.cwd)
        self.run_id: str | None = None

    # ------------------------------------------------------------------ 计划与调用事实

    def write_plan(self, steps: list[dict]) -> None:
        self.plan_path.write_text(json.dumps({"steps": steps}, ensure_ascii=False), encoding="utf-8")

    def calls(self) -> list[dict]:
        if not self.log_path.is_file():
            return []
        return [json.loads(line) for line in
                self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def call_count(self) -> int:
        return len(self.calls())

    # ------------------------------------------------------------------ 驱动

    def execute(self) -> dict:
        result = app.execute_run(self.config_path, self.cwd, mode="live", seed=SEED)
        self.run_id = str(result["run_id"])
        return result

    def extend(self, **delta) -> dict:
        return app.extend_run(self.config_path, self.cwd, self.run_id, **delta)

    def resume(self, *, max_slots: int | None = None) -> dict:
        return app.resume_run(self.config_path, self.cwd, self.run_id, max_slots=max_slots)

    # ------------------------------------------------------------------ 库读数

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = storage_db.connect(self.paths.db_path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def run_row(self) -> dict:
        return self.query("SELECT * FROM runs WHERE id = ?", (self.run_id,))[0]

    def events(self, kind: str) -> list[dict]:
        rows = self.query("SELECT * FROM run_events WHERE run_id = ? AND kind = ? ORDER BY id",
                          (self.run_id, kind))
        return [{**row, "detail": json.loads(row["detail_json"] or "{}")} for row in rows]

    def attempts(self, operation_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM operation_attempts WHERE operation_id = ? ORDER BY attempt_no",
            (operation_id,),
        )

    def source_dir(self) -> Path:
        return self.paths.run_dir(self.run_id) / "source"

    def snapshot_meta(self) -> dict:
        return json.loads((self.source_dir() / "snapshot.json").read_text(encoding="utf-8"))

    def batches(self) -> list[dict]:
        conn = storage_db.connect(self.paths.db_path)
        try:
            return app.completed_batches(Repository(conn), str(self.run_id))
        finally:
            conn.close()

    def batch_files(self, batch: dict) -> dict[str, Path]:
        directory = self.paths.resolve_within_run(batch["export_dir"], str(self.run_id))
        return {name: directory / name for name in
                ("keywords.csv", "needs.csv", "evidence.csv", "graph.graphml", "report.json")}


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveProject:
    """跑完整一条 live run：探索→分析→导出→追加额度→恢复→再跑完（共 4 次假 CLI 调用）。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(),
                          suggest_attempts=1, search_attempts=1)
    first = project.execute()
    assert first["engine"]["status"] == RunStatus.paused.value, first["engine"]
    project.extend(suggest_budget=1, search_budget=1, note="补一次真实采样")
    second = project.resume()
    project.first_drive = first  # type: ignore[attr-defined]
    project.second_drive = second  # type: ignore[attr-defined]
    return project


# --------------------------------------------------------------------- 完整闭环


def test_live_snapshot_freezes_tool_and_sampling_without_fixture(pipeline: LiveProject) -> None:
    """live 快照固化"是哪一份工具"与采样口径，且不复制任何合成数据：真实 run 的口径不来自 fixture。"""
    meta = pipeline.snapshot_meta()
    assert meta["mode"] == "live"
    assert meta["fixture"] is None, "live 快照不得带 fixture 块"
    assert not (pipeline.source_dir() / "shanghai_zhongkao.json").exists()
    tool = meta["tool"]
    assert tool["cli_path"] == str(pipeline.fake_cli)
    assert tool["node_path"] == sys.executable
    assert tool["profile"] == "default" and tool["browser"] == "msedge"
    assert tool["search_scrolls"] == 0 and tool["timeout_seconds"] == 5
    assert tool["lock_dir"] == str(pipeline.lock_dir)
    assert tool["cli_sha256"] == app.sha256_bytes(pipeline.fake_cli.read_bytes())
    assert meta["sampling"]["capabilities"].startswith("title_only")
    assert meta["normalizer"]["locations"] == ["上海"] and meta["normalizer"]["ambiguous_seeds"] == []
    # 固化的是配置里那一份词典/规则的字节副本，不是 fixture 数据集自带的口径
    names = set(meta["files"])
    assert {"domain_terms.txt", "intent_rules.yaml", "settings_snapshot.json"} <= names
    recorded = pipeline.events("source_snapshot")[0]["detail"]
    assert recorded["mode"] == "live" and recorded["fixture"] is None
    assert recorded["tool"]["cli_sha256"] == tool["cli_sha256"]


def test_live_run_persists_real_not_synthetic_rows(pipeline: LiveProject) -> None:
    """库里与导出里都必须是"非合成"：runs.synthetic_fixture=0、观察/证据 synthetic=0。"""
    run = pipeline.run_row()
    assert run["mode"] == "live" and int(run["synthetic_fixture"]) == 0
    assert pipeline.query("SELECT DISTINCT synthetic FROM suggestion_observations") == [{"synthetic": 0}]
    assert pipeline.query("SELECT DISTINCT synthetic FROM post_observations") == [{"synthetic": 0}]
    assert pipeline.query("SELECT DISTINCT synthetic FROM evidence") == [{"synthetic": 0}]
    row = pipeline.query("SELECT source_name, source_version FROM post_observations LIMIT 1")[0]
    assert row["source_name"] == sources_cli.SOURCE_NAME
    assert row["source_version"] == "xhs-search@2.0.1/schema:1.0", "会话记录的是 CLI 自己报的版本"


def test_live_artifacts_are_attributed_to_operation_and_attempt(pipeline: LiveProject) -> None:
    """数据源交回的每个原始文件都按哈希/归属校验后登记到本 run 本 attempt。"""
    rows = pipeline.query(
        "SELECT rel_path, kind, sha256, size_bytes, operation_id, attempt_id"
        " FROM raw_artifacts WHERE run_id = ? ORDER BY id",
        (pipeline.run_id,),
    )
    cli_rows = [row for row in rows if "/cli/" in row["rel_path"]]
    assert cli_rows, "live attempt 必须有落盘的原始产物"
    for row in cli_rows:
        assert row["rel_path"].startswith(f"runs/{pipeline.run_id}/operations/")
        assert row["operation_id"] is not None and row["attempt_id"] is not None
        target = pipeline.paths.resolve_within_run(row["rel_path"], pipeline.run_id)
        assert target.is_file()
        assert app.sha256_file(target) == row["sha256"]
        assert target.stat().st_size == int(row["size_bytes"])
    kinds = {row["kind"] for row in cli_rows}
    assert {"stdout_json", "stderr_log", "raw_response"} <= kinds
    names = {Path(row["rel_path"]).name for row in cli_rows}
    assert {"cli_stdout.json", "cli_stderr.log", "cli_meta.json", "cli_response.json"} <= names
    # 规范化信封另占一行，绝不用原始 JSON 冒充规范化结果
    envelopes = [row for row in rows if "/cli/" not in row["rel_path"]]
    assert any(Path(row["rel_path"]).name in {"suggest.json", "search.json"} for row in envelopes)
    counts = pipeline.query(
        "SELECT a.raw_artifact_count FROM operation_attempts a"
        " JOIN operations o ON o.id = a.operation_id WHERE o.run_id = ?",
        (pipeline.run_id,),
    )
    assert all(int(row["raw_artifact_count"]) >= 1 for row in counts)


def test_live_export_five_files_and_limits(pipeline: LiveProject) -> None:
    """五文件固定批次能读回来，报告说清 live 局限，且点赞不进任何导出列。"""
    batch = pipeline.second_drive["batch"]
    assert batch["verification_ok"] is True
    files = pipeline.batch_files(batch)
    assert sorted(files) == ["evidence.csv", "graph.graphml", "keywords.csv", "needs.csv",
                             "report.json"]
    assert all(path.is_file() for path in files.values())
    report = json.loads(files["report.json"].read_text(encoding="utf-8"))
    assert report["run"]["mode"] == "live"
    assert report["run"]["synthetic_fixture"] is False
    limitations = " ".join(report["limitations"])
    assert "title_only" in limitations and "xhs-search" in limitations
    assert "全部数据来自合成 fixture" not in limitations, "live 报告不得自称合成 fixture"
    assert "点赞不是搜索量" in limitations
    for name in ("keywords.csv", "needs.csv", "evidence.csv"):
        header = files[name].read_text(encoding="utf-8").splitlines()[0]
        assert "likes" not in header.lower(), f"{name} 泄漏点赞列：{header}"
    # 两批都可读回：旧批次不被新批次覆盖
    batches = pipeline.batches()
    assert len(batches) == 2 and len({item["snapshot"] for item in batches}) == 2
    for item in batches:
        directory = pipeline.paths.root / item["export_dir"]
        assert directory.is_dir()
        older = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        assert older["run"]["id"] == pipeline.run_id
        assert older["exported"]["snapshot"] == item["snapshot"]


def test_live_pause_and_resume_use_the_same_ledger(pipeline: LiveProject) -> None:
    """暂停原因与账本一致；追加额度是追加不是重置；恢复后已花的额度不回滚。"""
    paused_note = pipeline.first_drive["engine"]
    assert paused_note["pause_reason"] == PauseReason.fault.value, paused_note
    assert paused_note["outcome"] is None, "预算未达不是收敛"
    revisions = pipeline.query("SELECT * FROM budget_revisions WHERE run_id = ? ORDER BY id",
                               (pipeline.run_id,))
    assert len(revisions) == 1, revisions
    added = json.loads(revisions[0]["added_json"])
    assert added == {"suggest_attempts": 1, "search_attempts": 1}, "追加的是增量，不是重置成绝对值"
    assert revisions[0]["note"] == "补一次真实采样"
    used = {row["budget_key"]: row for row in pipeline.query(
        "SELECT budget_key, SUM(consumed_units) AS total FROM budget_ledger"
        " WHERE run_id = ? AND external_access = 1 GROUP BY budget_key", (pipeline.run_id,))}
    assert float(used["suggest_attempts"]["total"]) == 2.0
    assert float(used["search_attempts"]["total"]) == 2.0
    assert pipeline.query(
        "SELECT COUNT(*) AS n FROM budget_reservations"
        " WHERE run_id = ? AND consumed_at IS NULL", (pipeline.run_id,)
    )[0]["n"] == 0, "恢复后不得留幽灵预留"
    final = pipeline.run_row()
    assert final["status"] == RunStatus.completed.value, pipeline.second_drive["engine"]
    assert pipeline.call_count() == 4, "两次驱动各一次联想一次搜索，别多敲"


def test_live_search_argv_carries_frozen_scrolls_and_limit(pipeline: LiveProject) -> None:
    """真正发出去的 argv 用的就是固化那套：绝对路径、profile、browser、--scrolls 来自配置。"""
    calls = pipeline.calls()
    assert all(call["interpreter"] == sys.executable
               and call["invoked_as"] == [str(pipeline.fake_cli)] for call in calls), \
        "启动工具的可执行文件与脚本路径必须就是快照里那一份"
    argvs = [call["argv"] for call in calls]
    search = next(argv for argv in argvs if argv[0] == "search")
    assert "--profile" in search and "default" in search
    assert "--browser" in search and "msedge" in search
    assert search[search.index("--scrolls") + 1] == "0"
    assert search[search.index("--timeout") + 1] == "5000"
    suggest = next(argv for argv in argvs if argv[0] == "suggest")
    assert "--scrolls" not in suggest, "suggest 不接受搜索滚动参数"


# --------------------------------------------------------------------- 工具与协议漂移


def run_one_live_slot(project: LiveProject) -> dict:
    """跑一轮完整受保护临界区（探索→分析→导出），返回 drive 结果。"""
    result = app.execute_run(project.config_path, project.cwd, mode="live", seed=SEED)
    project.run_id = str(result["run_id"])
    return result


def test_live_resume_refuses_when_the_frozen_tool_changed(tmp_path, monkeypatch) -> None:
    """安装/升级会换掉那份工具：摘要对不上就拒绝继续，绝不换一份工具悄悄接着采。"""
    project = LiveProject(tmp_path, monkeypatch, [], suggest_attempts=2, search_attempts=2)
    project.write_plan([{"command": "suggest", "returncode": 0, "suggestions": []},
                        {"command": "search", "returncode": 0, "notes": [card("live-a", TITLE_A)]}])
    result = run_one_live_slot(project)
    assert project.call_count() >= 1, result
    project.fake_cli.write_text(
        project.fake_cli.read_text(encoding="utf-8") + "\n# 一次升级改掉了这份脚本\n", encoding="utf-8")
    calls = project.call_count()
    with pytest.raises(app.SourceIntegrityError) as excinfo:
        app.resume_run(project.config_path, project.cwd, project.run_id)
    message = str(excinfo.value)
    assert "已被改动" in message and str(project.fake_cli) in message
    assert project.call_count() == calls, "拒绝恢复不得再多敲一次 CLI"
    # 快照本身也拒绝被当作"当前配置"来重放：指向不存在工具同样拒绝
    project.fake_cli.write_text("raise SystemExit(99)\n", encoding="utf-8")
    with pytest.raises(app.SourceIntegrityError) as second:
        app.resume_run(project.config_path, project.cwd, project.run_id)
    assert "已被改动" in str(second.value)


def test_live_resume_refuses_when_schema_support_changes(tmp_path, monkeypatch) -> None:
    """协议口径漂移同样拒绝：历史 run 不按"新代码支持的 schemaVersion"重采。"""
    project = LiveProject(tmp_path, monkeypatch, [], suggest_attempts=2, search_attempts=2)
    project.write_plan([{"command": "suggest", "returncode": 0, "suggestions": []},
                        {"command": "search", "returncode": 0, "notes": [card("live-a", TITLE_A)]}])
    run_one_live_slot(project)
    calls = project.call_count()
    monkeypatch.setattr(sources_cli, "SUPPORTED_SCHEMA_VERSIONS", frozenset({"1.1"}))
    with pytest.raises(app.SourceIntegrityError) as excinfo:
        app.resume_run(project.config_path, project.cwd, project.run_id)
    assert "schemaVersion" in str(excinfo.value)
    assert project.call_count() == calls


# --------------------------------------------------------------------- 故障形状

FAULTS: dict[str, tuple[dict, PauseReason]] = {
    "auth": ({"command": "suggest", "returncode": 3, "no_rows": True,
              "top_error": {"code": "AUTH_REQUIRED", "message": "需要登录", "hint": "运行 init"}},
             PauseReason.auth_required),
    "action": ({"command": "suggest", "returncode": 4, "no_rows": True,
                "top_error": {"code": "ACTION_REQUIRED", "message": "需要人工验证"}},
               PauseReason.action_required),
    "profile_busy": ({"command": "suggest", "returncode": 6, "no_rows": True,
                      "top_error": {"code": "PROFILE_BUSY", "message": "profile 占用"}},
                     PauseReason.profile_busy),
    "schema_drift": ({"command": "suggest", "returncode": 0, "schemaVersion": "2.0",
                      "suggestions": [CHILD]},
                     PauseReason.fault),
    "empty_stdout": ({"command": "suggest", "returncode": 0, "raw_stdout": ""},
                     PauseReason.fault),
}


@pytest.mark.parametrize("name,step,pause_reason", sorted(
    [(key, value[0], value[1]) for key, value in FAULTS.items()]
))
def test_live_fault_pauses_without_burning_retries(
    tmp_path, monkeypatch, name, step, pause_reason
) -> None:
    """认证/验证/profile 占用/协议漂移/空 stdout：一律停在 paused，不重试、不发布、账照记一笔。"""
    project = LiveProject(tmp_path, monkeypatch, [step], suggest_attempts=1, search_attempts=0,
                          card_limit=0, max_posts_stored=0)
    result = app.execute_run(project.config_path, project.cwd, mode="live", seed=SEED)
    run_id = str(result["run_id"])
    project.run_id = run_id

    assert result["engine"]["status"] == RunStatus.paused.value, result["engine"]
    assert result["engine"]["pause_reason"] == pause_reason.value, result["engine"]
    assert result["engine"]["outcome"] is None, "异常与验证码不得记成收敛"
    operation = project.query(
        "SELECT * FROM operations WHERE run_id = ? AND operation_type = 'suggest'", (run_id,)
    )[0]
    attempts = project.attempts(int(operation["id"]))
    assert len(attempts) == 1, f"{name} 不允许重试"
    assert attempts[0]["result_state"] == ResultState.failed.value
    assert project.call_count() == 1, f"{name} 只发起了一次真实调用"
    # 失败仍登记一条观察（如实的 failed 状态），但一条内容都不发布
    observations = project.query(
        "SELECT state, item_count FROM suggestion_observations WHERE run_id = ?", (run_id,))
    assert [row["state"] for row in observations] == [ResultState.failed.value]
    assert int(observations[0]["item_count"]) == 0
    assert project.query("SELECT COUNT(*) AS n FROM suggestion_items", ()) [0]["n"] == 0, \
        "失败不发布联想条目"
    assert project.query("SELECT COUNT(*) AS n FROM budget_reservations"
                         " WHERE run_id = ? AND consumed_at IS NULL", (run_id,))[0]["n"] == 0
    used = Ledger.from_settings(  # 复用同一份配置读库里的账
        storage_db.connect(project.paths.db_path), run_id, project.settings
    ).used("suggest_attempts")
    assert used == 1.0
    # 原始响应仍然落盘并登记：不能因为失败就丢掉可复核的证据
    assert project.query("SELECT COUNT(*) AS n FROM raw_artifacts WHERE run_id = ?"
                         " AND rel_path LIKE '%/cli/%'", (run_id,))[0]["n"] >= 3


def test_live_partial_with_blocking_error_keeps_evidence_and_pauses(tmp_path, monkeypatch) -> None:
    """partial 带阻断错误（写文件失败）：证据入库、原始响应留档，但整轮停在 paused/fault。"""
    step = {"command": "suggest", "returncode": 1, "status": "completed", "suggestions": [CHILD],
            "top_error": {"code": "OUTPUT_ERROR", "message": "写文件失败"}}
    project = LiveProject(tmp_path, monkeypatch, [step], suggest_attempts=1, search_attempts=0,
                          card_limit=0, max_posts_stored=0)
    result = app.execute_run(project.config_path, project.cwd, mode="live", seed=SEED)
    project.run_id = str(result["run_id"])

    assert result["engine"]["status"] == RunStatus.paused.value
    assert result["engine"]["pause_reason"] == PauseReason.fault.value
    observations = project.query(
        "SELECT state, item_count FROM suggestion_observations WHERE run_id = ?",
        (project.run_id,))
    assert [row["state"] for row in observations] == [ResultState.partial.value]
    assert int(observations[0]["item_count"]) == 1, "partial 的合法条目必须保留，不整批丢弃"
    attempt = project.attempts(
        int(project.query("SELECT id FROM operations WHERE run_id = ?", (project.run_id,))[0]["id"])
    )[0]
    error = json.loads(attempt["error_json"])
    assert error["code"] == "WORD_OUTPUT_ERROR" and error["human_action_required"] is True
    assert project.call_count() == 1, "阻断错误不重试"


# --------------------------------------------------------------------- 退避、重放、恢复


def test_live_retry_waits_the_backoff_for_real(tmp_path, monkeypatch) -> None:
    """live 的重试必须真的等：网络抖动后第二次访问距第一次至少隔了整段退避。"""
    steps = [
        {"command": "suggest", "seed": SEED, "times": 1, "returncode": 5, "status": "failed",
         "word_error": {"code": "NETWORK_ERROR", "message": "网络抖动"}},
        {"command": "suggest", "seed": SEED, "returncode": 0, "suggestions": [CHILD]},
        {"command": "search", "returncode": 0, "notes": [card("live-a", TITLE_A)]},
    ]
    project = LiveProject(tmp_path, monkeypatch, steps, suggest_attempts=2, search_attempts=1,
                          backoff_base_seconds=2.0)
    result = app.execute_run(project.config_path, project.cwd, mode="live", seed=SEED)
    project.run_id = str(result["run_id"])

    operation_id = int(project.query(
        "SELECT id FROM operations WHERE run_id = ? AND operation_type = 'suggest'",
        (project.run_id,))[0]["id"])
    attempts = project.attempts(operation_id)
    assert [row["attempt_no"] for row in attempts] == [1, 2]
    assert [row["result_state"] for row in attempts] == ["failed", "completed"]
    accesses = project.query(
        "SELECT attempt_no FROM budget_ledger WHERE operation_id = ? AND external_access = 1"
        " ORDER BY attempt_no", (operation_id,))
    assert [row["attempt_no"] for row in accesses] == [1, 2], "重试同样占预算"
    event = project.events("retry_backoff")[0]["detail"]
    assert event["planned_seconds"] == 2.0 and event["slept_seconds"] == 2.0
    assert event["mode"] == "live"
    stamps = [call["at"] for call in project.calls()]
    assert stamps[1] - stamps[0] >= 2.0, (
        f"两次访问只隔了 {stamps[1] - stamps[0]:.3f}s：真实模式没有真的等退避"
    )


def test_live_replay_never_touches_the_cli_again(tmp_path, monkeypatch) -> None:
    """已有可靠观察的重放只读库：不再起子进程、不再扣额度、不再多一条观察。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(),
                          suggest_attempts=1, search_attempts=1)
    result = run_one_live_slot(project)
    calls_before = project.call_count()
    assert calls_before == 2, "首槽一次联想一次搜索"

    repo, paths, _ = app.open_store(project.config_path, project.cwd)
    conn, comp = app.build_components(repo, paths, project.run_id)
    keyword_id = int(conn.execute(
        "SELECT keyword_id FROM run_keywords WHERE run_id = ? AND depth = 0",
        (project.run_id,)).fetchone()["keyword_id"])
    ledger = Ledger.from_settings(conn, project.run_id, project.settings)
    ledger_used_before = ledger.used("suggest_attempts")
    observations_before = conn.execute(
        "SELECT COUNT(*) FROM suggestion_observations WHERE run_id = ?", (project.run_id,)
    ).fetchone()[0]

    outcome = comp.engine._observe_suggestions(  # noqa: SLF001 - 直接驱动同一逻辑键
        keyword_id=keyword_id, branch_key="b-root", revisit_seq=0, slot_kind=None,
        sample_kind=CalibrationSampleKind.active_exploration,
    )
    assert outcome.receipt.replayed is True
    assert project.call_count() == calls_before, "重放不得再起子进程"
    assert ledger.used("suggest_attempts") == ledger_used_before, "重放不再扣额度"
    assert conn.execute("SELECT COUNT(*) FROM suggestion_observations WHERE run_id = ?",
                        (project.run_id,)).fetchone()[0] == observations_before
    assert any(REPLAY_NOTE in note for note in outcome.notes)
    conn.close()


def test_live_recovery_imports_cli_artifacts_once_and_never_double_bills(tmp_path, monkeypatch) -> None:
    """崩溃恢复补导入：本 run 的文件按归属登记、重复扫描不新增行，也不再多扣一笔访问。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(),
                          suggest_attempts=2, search_attempts=2)
    result = run_one_live_slot(project)
    run_id = str(project.run_id)
    before = project.query(
        "SELECT COUNT(*) AS n FROM raw_artifacts WHERE run_id = ?", (run_id,))[0]["n"]
    conn = storage_db.connect(project.paths.db_path)
    try:
        rows = conn.execute("SELECT rel_path FROM raw_artifacts WHERE run_id = ?", (run_id,)).fetchall()
        lost = [str(row["rel_path"]) for row in rows]
        assert len(lost) == before
        with storage_db.transaction(conn):
            conn.execute("DELETE FROM raw_artifacts WHERE run_id = ?", (run_id,))
        # 假装还有一份不属于任何 attempt 的散文件：它必须被记孤儿，而不是被认成本次结果
        stray = project.paths.run_dir(run_id) / "operations" / "stray.json"
        stray.write_text('{"note": "不属于任何 attempt"}', encoding="utf-8")
        report = import_unregistered(conn, project.paths, run_id)
        restored = {str(row["rel_path"]) for row in conn.execute(
            "SELECT rel_path FROM raw_artifacts WHERE run_id = ?", (run_id,))}
        assert set(lost) <= restored, "已登记的字节全部补回"
        stray_rel = stray.relative_to(project.paths.root).as_posix()
        assert stray_rel in report.orphaned, "散文件必须记孤儿，不得被认成某次 attempt 的产物"
        assert not [rel for rel in report.orphaned if "/cli/" in rel], \
            "attempt 目录里的产物必须还原归属，不能一律丢给孤儿"
        attributed = conn.execute(
            "SELECT COUNT(*) AS n FROM raw_artifacts WHERE run_id = ? AND operation_id IS NOT NULL"
            " AND rel_path LIKE '%/cli/%'", (run_id,)).fetchone()
        assert int(attributed["n"]) >= 4, "补导入按 attempt 目录还原了 operation/attempt 归属"
        assert import_unregistered(conn, project.paths, run_id).imported_count == 0, "重复扫描不新增行"
        ledger = Ledger.from_settings(conn, run_id, project.settings)
        used_before = ledger.used("suggest_attempts") + ledger.used("search_attempts")
        recovery = recover_run(conn, project.paths, run_id, ledger=ledger)
        assert recovery.imports.imported_count == 0
        assert not recovery.uncertain, "本 run 的 operation 都已有终态 attempt，不需要标 uncertain"
        assert ledger.used("suggest_attempts") + ledger.used("search_attempts") == used_before, "恢复不重复扣费"
    finally:
        conn.close()


# --------------------------------------------------------------------- 跨 run / 能力边界


def test_same_profile_across_data_roots_admits_only_one_browser(tmp_path, monkeypatch) -> None:
    """同 profile 的互斥在机器级锁目录，不在某个数据根里：第二个 run 直接吃 profile_busy，不起浏览器。"""
    shared = tmp_path / "shared-machine-locks"
    sibling = LiveProject(tmp_path / "root-a", monkeypatch, happy_steps(), lock_dir=shared)
    project = LiveProject(tmp_path / "root-b", monkeypatch, happy_steps(), lock_dir=shared)
    # 两个 run 各有数据根，但抢的是同一份 profile 锁：互斥不随 data_dir 隔离而失效
    assert sibling.paths.root != project.paths.root
    assert Path(sibling.settings.sources.xhs_search.lock_dir) == shared
    guard_path = shared / "xhs-cli-profile-default.guard"
    assert not str(guard_path).startswith(str(project.paths.root))

    guard = OsGuard(guard_path)
    assert guard.try_acquire() is True, "另一个 run/进程占着同一个 profile"
    try:
        result = run_one_live_slot(project)
        assert project.call_count() == 0, "profile 被占用时不得起第二个浏览器/子进程"
        assert result["engine"]["status"] == RunStatus.paused.value
        assert result["engine"]["pause_reason"] == PauseReason.profile_busy.value
        assert project.query(
            "SELECT COUNT(*) AS n FROM raw_artifacts WHERE run_id = ? AND rel_path LIKE '%cli_meta.json'",
            (project.run_id,))[0]["n"] >= 1, "本地拒绝也要留下可复核的 attempt 元数据"
        assert project.query(
            "SELECT COUNT(*) AS n FROM budget_reservations WHERE run_id = ? AND consumed_at IS NULL",
            (project.run_id,))[0]["n"] == 0
    finally:
        guard.release()


class StubSource(SearchSource):
    """只用来驱动引擎的失败形状：真实超时/取消必须等满 10s 以上，那部分留给协议测试。"""

    name = "live-stub"
    version = "test"
    synthetic = False

    def __init__(self, error: ErrorInfo, *, sleepless: list[float] | None = None) -> None:
        self.error = error
        self.calls = 0
        self.sleepless = sleepless if sleepless is not None else []

    def capabilities(self) -> Capabilities:
        return Capabilities.title_only()

    def new_session(self) -> SessionRef:
        return SessionRef(session_id="stub-1", collected_at=utc_now(), source_name=self.name,
                          source_version=self.version, synthetic=False)

    def get_suggestions(self, context: OperationContext) -> SuggestionResult:
        self.calls += 1
        return SuggestionResult(state=ResultState.failed, context=context, session=self.new_session(),
                                error=self.error, warnings=["stub"])

    def _search_posts(self, context: OperationContext, *, limit: int,
                      session: SessionRef) -> PostSearchResult:
        return PostSearchResult(state=ResultState.skipped, context=context, session=session,
                                warnings=["stub 不采内容"])


def test_uncertain_process_timeout_is_not_retried_but_still_billed(tmp_path, monkeypatch) -> None:
    """进程级超时：无法确认 CLI 派生的浏览器已退出 → 不重试、照计一笔、停在 paused/network。"""
    project = LiveProject(tmp_path, monkeypatch, [], suggest_attempts=1, search_attempts=0,
                          card_limit=0, max_posts_stored=0)
    settings = load_settings(project.config_path)
    run_id = "run-live-timeout"
    conn = storage_db.connect(project.paths.db_path)
    try:
        repo = Repository(conn)
        prepare_run(repo, settings, run_id=run_id, created_at=utc_now())
        error = ErrorInfo(
            code="CLI_PROCESS_TIMEOUT", message="超过 total_process_timeout_seconds",
            category=ErrorCategory.network, retryable=True, human_action_required=False,
        )
        slept: list[float] = []
        source = StubSource(error)
        engine = DiscoveryEngine(
            paths=project.paths, settings=settings, run_id=run_id, source=source, repo=repo,
            ledger=Ledger.from_settings(conn, run_id, settings),
            control=WorkerControl(conn, project.paths, run_id),
            domain_terms=DOMAIN_TERMS, locations=("上海",), retry_sleeper=slept.append,
        )
        engine.control.acquire()
        try:
            report = engine.run(max_slots=1, calibration=False)
        finally:
            engine.control.release()

        assert source.calls == 1, "不确定浏览器是否还活着就不重试"
        assert slept == [], "不重试就不该退避等待"
        operation = conn.execute("SELECT id FROM operations WHERE run_id = ?", (run_id,)).fetchone()
        attempts = conn.execute("SELECT COUNT(*) FROM operation_attempts WHERE operation_id = ?",
                                (int(operation["id"]),)).fetchone()
        assert int(attempts[0]) == 1
        assert engine.ledger.used("suggest_attempts") == 1.0, "已经发生过的访问照计"
        assert conn.execute("SELECT COUNT(*) FROM budget_reservations"
                            " WHERE consumed_at IS NULL").fetchone()[0] == 0
        assert report.termination.status is RunStatus.paused
        assert report.termination.pause_reason is PauseReason.network
        assert project.call_count() == 0
    finally:
        conn.close()


def test_live_run_does_not_borrow_fixture_normalizer(tmp_path, monkeypatch) -> None:
    """live 的地域/歧义口径来自配置；fixture 数据集的 regions/歧义清单一律不借用。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(),
                          suggest_attempts=1, search_attempts=0, card_limit=0, max_posts_stored=0)
    result = app.execute_run(project.config_path, project.cwd, mode="live", seed=SEED)
    project.run_id = str(result["run_id"])
    meta = project.snapshot_meta()
    assert meta["normalizer"]["ambiguous_seeds"] == []
    assert "上海" in meta["normalizer"]["locations"]
    dataset = json.loads((ROOT / "tests" / "fixtures" / "shanghai_zhongkao.json")
                         .read_text(encoding="utf-8"))
    assert set(meta["normalizer"]["ambiguous_seeds"]) & set(dataset["ambiguous_seeds"]) == set()
    assert int(project.run_row()["synthetic_fixture"]) == 0
