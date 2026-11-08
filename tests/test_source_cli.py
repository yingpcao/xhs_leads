"""P1-A CLI 适配器离线协议测试。

- 主体用注入 runner/时钟覆盖 spec 全部退出码、逐词 auth/action、超时/取消、中文/空/损坏/错词/版本漂移、
  空 vs 不确定空、partial 保数据、限频、attempt 目录拒绝覆盖、profile 串行；全部离线，不联网不读凭据。
- 另有真实本地假 CLI 子进程端到端（sys.executable + tests/fixtures/cli_protocol/fake_cli.py），
  覆盖大输出不阻塞、总超时只回收本次子进程、shell 字符仍是单参数、真实 suggest/search 闭环。
- 一个可选测试读取本机真实预检响应（干净 checkout 缺文件时自动跳过），不作为普通测试的依赖。

所有构造响应来自 tests/fixtures/cli_protocol/scenarios.json，明确标注为测试构造（非真实采集）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from xhs_discovery.models.common import RawArtifactRef
from xhs_discovery.models.enums import OperationType, ResultState
from xhs_discovery.models.source import KeywordText, OperationContext
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.sources.base import AccessNotStarted
from xhs_discovery.sources.cli import (
    CliAttemptConflict,
    CliSearchSource,
    CliSourceConfig,
    CommandResult,
    SubprocessRunner,
    from_settings,
)
from xhs_discovery.storage.worker import OsGuard
from xhs_discovery.text.normalize import normalize_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cli_protocol"
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
SEED = SCENARIOS["requested_seed"]
FAKE_CLI = str(FIXTURES / "fake_cli.py")


class ScenarioRunner:
    def __init__(self, scenario: dict) -> None:
        self.scenario = scenario
        self.calls: list[list[str]] = []

    def run(self, argv, *, out_dir, timeout_seconds, cancel):
        self.calls.append(list(argv))
        out_dir.mkdir(parents=True, exist_ok=True)
        # 模拟工具自身在 --out 写产物
        (out_dir / "response.json").write_text(self.scenario["stdout"], encoding="utf-8")
        return CommandResult(
            returncode=self.scenario["returncode"],
            stdout=self.scenario["stdout"].encode("utf-8"),
            stderr=self.scenario["stderr"].encode("utf-8"),
            timed_out=self.scenario.get("timed_out", False),
            cancelled=self.scenario.get("cancelled", False),
        )


class ClockStub:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.value += seconds


def make_ctx(command: str, *, attempt: int = 1, run: str = "r1", kwid: int | None = 1,
             seed: str = SEED) -> OperationContext:
    op = OperationType.suggest if command == "suggest" else OperationType.search
    return OperationContext(
        run_id=run, keyword_id=kwid, keyword=KeywordText(raw=seed, normalized=normalize_text(seed)),
        operation_type=op, revisit_seq=0, attempt_no=attempt,
        allow_pre_registry=kwid is None,
    )


def build_source(tmp_path, scenario, *, clock=None, lock_dir=None, runner=None):
    root = tmp_path / "root"
    paths = RuntimePaths(root)
    config = CliSourceConfig(
        node_path="node", cli_path="bin.mjs", profile="default", browser="msedge",
        timeout_seconds=90, total_process_timeout_seconds=180,
    )
    return CliSearchSource(
        config=config, paths=paths, out_root=root,
        runner=runner or ScenarioRunner(scenario),
        clock=clock or ClockStub(),
        lock_dir=lock_dir or (tmp_path / "locks"),
    )


def invoke(source, command, ctx):
    if command == "suggest":
        return source.get_suggestions(ctx)
    return source.search_posts(ctx, limit=30)


# --------------------------------------------------------------------------- 全矩阵


@pytest.mark.parametrize("scenario", SCENARIOS["scenarios"], ids=[s["name"] for s in SCENARIOS["scenarios"]])
def test_protocol_matrix(scenario, tmp_path):
    source = build_source(tmp_path, scenario)
    result = invoke(source, scenario["command"], make_ctx(scenario["command"]))
    exp = scenario["expect"]

    assert result.state is ResultState(exp["state"])

    if exp.get("error_code"):
        assert result.error is not None and result.error.code == exp["error_code"]
    elif exp["state"] in ("failed",):
        assert result.error is not None  # failed 必带 error，不能空表
    elif exp["state"] == "completed":
        assert result.error is None

    if exp.get("category") is not None and result.error is not None:
        assert result.error.category.value == exp["category"]
    if exp.get("human") is not None and result.error is not None:
        assert result.error.human_action_required is exp["human"]
    if exp.get("retryable") is not None and result.error is not None:
        assert result.error.retryable is exp["retryable"]

    if exp["items"] == "empty":
        assert result.items == []
    elif exp["items"] == "some":
        assert len(result.items) >= exp.get("min_items", 1)

    # 真实来源、非 synthetic、带落盘产物
    assert result.session.synthetic is False
    assert result.raw_artifacts and all(isinstance(a, RawArtifactRef) for a in result.raw_artifacts)
    if exp["state"] == "partial":
        assert result.per_word  # partial 必须能定位逐词


# --------------------------------------------------------------------------- 具体语义


def test_empty_is_not_uncertain_empty(tmp_path):
    ok = next(s for s in SCENARIOS["scenarios"] if s["name"] == "suggest_empty_confirmed")
    unc = next(s for s in SCENARIOS["scenarios"] if s["name"] == "search_uncertain_empty")
    confirmed = invoke(build_source(tmp_path / "a", ok), "suggest", make_ctx("suggest"))
    uncertain = invoke(build_source(tmp_path / "b", unc), "search", make_ctx("search"))
    assert confirmed.state is ResultState.completed and confirmed.error is None
    assert uncertain.state is ResultState.failed and uncertain.error is not None
    assert uncertain.items == []


def test_likes_not_parsed_and_missing_kept(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "search_ok")
    result = invoke(build_source(tmp_path, scenario), "search", make_ctx("search"))
    card = result.items[0]
    assert card.likes_text == "1.2万"  # 原样文本，不当需求量
    assert card.access_url == scenario["stdout"] is None or card.access_url.startswith("https://")
    assert card.published_at_text == "10-07"
    missing = next(c for c in result.items if c.note_id == "n2")
    assert missing.title_missing is True
    assert missing.published_at_missing is True
    assert missing.likes_text is None
    assert missing.author.kind == "unknown"


def test_url_and_provenance_preserved(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "search_ok")
    result = invoke(build_source(tmp_path, scenario), "search", make_ctx("search"))
    assert "xsec_token=" in result.items[0].access_url  # 完整签名 URL 保留
    assert result.session.source_version == "xhs-search@2.0.1/schema:1.0"
    assert result.session.collected_at.tzinfo is not None


def test_raw_artifacts_recoverable_on_disk(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "search_ok")
    source = build_source(tmp_path, scenario)
    result = invoke(source, "search", make_ctx("search"))
    kinds = {a.kind for a in result.raw_artifacts}
    assert {"stdout_json", "stderr_log", "raw_response"} <= kinds
    import hashlib
    root = tmp_path / "root"
    for artifact in result.raw_artifacts:
        target = root / Path(*artifact.path.split("/"))
        assert target.is_file()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == artifact.sha256


def test_attempt_dir_rejects_overwrite(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "suggest_ok")
    source = build_source(tmp_path, scenario)
    invoke(source, "suggest", make_ctx("suggest", attempt=7))  # 首次落盘
    with pytest.raises(CliAttemptConflict):
        invoke(source, "suggest", make_ctx("suggest", attempt=7))  # 同 attempt 目录拒绝覆盖
    # 新 attempt_no 可正常写入
    assert invoke(source, "suggest", make_ctx("suggest", attempt=8)).state is ResultState.completed


def test_min_interval_across_instances(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "suggest_ok")
    clock = ClockStub(500.0)
    lock = tmp_path / "locks"
    a = build_source(tmp_path / "x", scenario, clock=clock, lock_dir=lock)
    b = build_source(tmp_path / "y", scenario, clock=clock, lock_dir=lock)
    invoke(a, "suggest", make_ctx("suggest", run="ra", attempt=1))  # 首次无间隔
    clock.value += 1.0  # 距上次仅 1s < 2.5s
    invoke(b, "suggest", make_ctx("suggest", run="rb", attempt=1))
    assert any(s >= 1.5 for s in clock.slept)  # 第二实例被同 profile 限频拖住


def test_profile_busy_serializes_two_instances(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "suggest_ok")
    lock = tmp_path / "locks"
    a = build_source(tmp_path / "a", scenario, lock_dir=lock)
    held = OsGuard(a._lock_path())
    assert held.try_acquire() is True
    busy = invoke(a, "suggest", make_ctx("suggest", attempt=3))
    assert busy.state is ResultState.failed
    assert busy.error.category.value == "profile_busy"
    assert busy.error.retryable is True and busy.items == []
    held.release()


def test_config_requires_profile_and_browser_no_access():
    with pytest.raises(AccessNotStarted):
        CliSourceConfig(node_path="node", cli_path="bin.mjs", profile="", browser="msedge",
                        timeout_seconds=90, total_process_timeout_seconds=180)
    with pytest.raises(AccessNotStarted):
        CliSourceConfig(node_path="node", cli_path="bin.mjs", profile="default", browser="",
                        timeout_seconds=90, total_process_timeout_seconds=180)
    with pytest.raises(AccessNotStarted):  # total 必须 > 单操作 timeout，两者独立
        CliSourceConfig(node_path="node", cli_path="bin.mjs", profile="default", browser="msedge",
                        timeout_seconds=180, total_process_timeout_seconds=90)


def test_pre_registry_context_no_keyword_id(tmp_path):
    scenario = next(s for s in SCENARIOS["scenarios"] if s["name"] == "suggest_ok")
    source = build_source(tmp_path, scenario)
    ctx = make_ctx("suggest", kwid=None)
    # allow_pre_registry 身份走规范化文本，attempt 目录用哈希，仍能落盘
    assert source.get_suggestions(ctx).state is ResultState.completed


def test_from_settings_wiring(tmp_path):
    from xhs_discovery.config import Settings

    root = tmp_path / "root"
    paths = RuntimePaths(root)
    # 未配置 node/cli/profile/browser 时适配器拒绝任何外部访问（本包 live 仍关闭）
    unconfigured = Settings.model_validate(
        {"run": {"seed_raw": SEED, "mode": "fixture"}}
    )
    with pytest.raises(AccessNotStarted):
        from_settings(paths, unconfigured)
    configured = Settings.model_validate({
        "run": {"seed_raw": SEED, "mode": "fixture"},
        "sources": {"xhs_search": {
            "node_path": "node", "cli_path": "bin.mjs", "profile": "default",
            "browser": "msedge",
        }},
    })
    src = from_settings(paths, configured, clock=ClockStub(), lock_dir=tmp_path / "locks")
    assert src.config.profile == "default" and src.config.browser == "msedge"


# --------------------------------------------------------------------------- 真实子进程端到端


def _real_source(tmp_path, monkeypatch, case, *, total_timeout=180, timeout=90):
    monkeypatch.setenv("XHS_FAKE_CASE", case)
    root = tmp_path / "root"
    config = CliSourceConfig(
        node_path=sys.executable, cli_path=FAKE_CLI, profile="default", browser="msedge",
        timeout_seconds=timeout, total_process_timeout_seconds=total_timeout,
    )
    return CliSearchSource(
        config=config, paths=RuntimePaths(root), out_root=root,
        runner=SubprocessRunner(cwd=root), lock_dir=tmp_path / "locks",
    )


def test_real_subprocess_suggest_e2e(tmp_path, monkeypatch):
    source = _real_source(tmp_path, monkeypatch, "suggest_ok")
    result = source.get_suggestions(make_ctx("suggest"))
    assert result.state is ResultState.completed
    assert [i.text for i in result.items] == ["上海中考分数组成", "上海中考总分750分", "上海中考体育"]
    assert result.session.synthetic is False


def test_real_subprocess_search_e2e(tmp_path, monkeypatch):
    source = _real_source(tmp_path, monkeypatch, "search_ok")
    result = source.search_posts(make_ctx("search", attempt=2), limit=30)
    assert result.state is ResultState.completed
    assert len(result.items) == 2
    assert "xsec_token=" in result.items[0].access_url


def test_real_subprocess_big_output_no_deadlock(tmp_path, monkeypatch):
    source = _real_source(tmp_path, monkeypatch, "big")
    result = source.get_suggestions(make_ctx("suggest", attempt=3))
    assert result.state is ResultState.completed
    assert len(result.items) == 2000  # 大 stdout+大 stderr 并发读取仍完整


def test_real_subprocess_total_timeout_reaps_child(tmp_path, monkeypatch):
    source = _real_source(tmp_path, monkeypatch, "sleep_forever", total_timeout=2, timeout=1)
    result = source.get_suggestions(make_ctx("suggest", attempt=4))
    assert result.state is ResultState.failed
    assert result.error.code == "CLI_PROCESS_TIMEOUT"
    assert result.error.retryable is True


def test_real_subprocess_arg_is_single_param(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_FAKE_CASE", "echo_argv")
    tricky = 'a b"c $d ; rm -rf /'
    runner = SubprocessRunner()
    result = runner.run([sys.executable, FAKE_CLI, "suggest", "--seed", tricky, "--profile", "default"],
                        out_dir=tmp_path, timeout_seconds=30, cancel=None)
    payload = json.loads(result.stdout.decode("utf-8"))
    assert tricky in payload["data"]["argv"]  # 空格/引号/shell 字符仍是一个参数
    assert "--seed" in payload["data"]["argv"]


def test_real_subprocess_two_process_profile_busy(tmp_path):
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    guard_path = lock_dir / f"xhs-cli-profile-default.guard"
    ready = tmp_path / "ready.txt"
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time;sys.path.insert(0,'src');"
         "from pathlib import Path;from xhs_discovery.storage.worker import OsGuard;"
         f"g=OsGuard(Path(r'{guard_path}'));assert g.try_acquire();"
         f"open(r'{ready}','w').write('1');time.sleep(5);g.release()"],
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            import time as _t
            _t.sleep(0.05)
        assert ready.exists(), "子进程未能在限定时间内取得 profile 互斥"
        config = CliSourceConfig(
            node_path=sys.executable, cli_path=FAKE_CLI, profile="default", browser="msedge",
            timeout_seconds=90, total_process_timeout_seconds=180,
        )
        source = CliSearchSource(config=config, paths=RuntimePaths(root), out_root=root,
                                 runner=SubprocessRunner(cwd=root), lock_dir=lock_dir)
        busy = source.get_suggestions(make_ctx("suggest", attempt=1))
        assert busy.state is ResultState.failed and busy.error.category.value == "profile_busy"
    finally:
        child.terminate()
        child.wait(timeout=5)


# --------------------------------------------------------------------------- 真实预检（可选）


PREFLIGHT = Path("docs/verification/xhs-tool-check-20261028")


@pytest.mark.skipif(not (PREFLIGHT / "suggest-stdout.json").exists(),
                    reason="本机真实预检文件不存在（干净 checkout 跳过）")
def test_reads_real_preflight_suggest_if_present(tmp_path, monkeypatch):
    raw = (PREFLIGHT / "suggest-stdout.json").read_bytes()
    scenario = {"command": "suggest", "returncode": 0, "stdout": raw.decode("utf-8"), "stderr": "",
                "timed_out": False, "cancelled": False, "expect": {}}
    source = build_source(tmp_path, scenario)
    result = source.get_suggestions(make_ctx("suggest"))
    assert result.state is ResultState.completed
    assert len(result.items) == 8  # 真实预检种子"上海中考"返回 8 条建议


# --------------------------------------------------------------------------- P1-A-r2 协议严格核验反例
#
# 覆盖 Codex 原始复现失败项及相邻排列：缺失/null/错类型 results、错词/多词/重复词、
# 字符串 suggestions 逐字符、AUTH/ACTION 顶层与逐词优先、timeout 与 cancel 下信号优先、
# rescue 只保留严格核验的本词 partial。全部离线注入 runner，不触平台。


def _base(name):
    return next(s for s in SCENARIOS["scenarios"] if s["name"] == name)


def _scenario(name, mutate, **flags):
    """取基场景、按 mutate 改 envelope、返回可注入的 scenario dict（不改磁盘 fixture）。"""
    import copy
    base = _base(name)
    doc = copy.deepcopy(json.loads(base["stdout"]))
    mutate(doc)
    sc = dict(base)
    sc["stdout"] = json.dumps(doc, ensure_ascii=False)
    sc.update(flags)
    return sc


def _run(tmp_path, scenario):
    return invoke(build_source(tmp_path, scenario), scenario["command"], make_ctx(scenario["command"]))


def _assert_protocol_failure(result, code):
    assert result.state is ResultState.failed, result.state
    assert result.items == []  # 协议故障不得发布 items，也不得逐字符当建议
    assert result.error is not None and result.error.code == code, result.error
    return result.error


# --- C-19 原始四例（独立反例）---


def test_r2_missing_results_not_confirmed_empty(tmp_path):
    err = _assert_protocol_failure(
        _run(tmp_path, _scenario("suggest_ok", lambda d: d.__setitem__("data", {}))), "RESULTS_MISSING")
    assert err.human_action_required is True


def test_r2_completed_entry_auth_wins_over_items(tmp_path):
    def m(d):
        d["data"]["results"][0]["error"] = {"code": "AUTH_REQUIRED", "message": "login required"}
    err = _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "WORD_AUTH_REQUIRED")
    assert err.category.value == "auth" and err.human_action_required is True


def test_r2_top_level_auth_wins_over_completed(tmp_path):
    def m(d):
        d["error"] = {"code": "AUTH_REQUIRED", "message": "login required"}
    err = _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "WORD_AUTH_REQUIRED")
    assert err.human_action_required is True


def test_r2_timeout_wrong_seed_not_rescued(tmp_path):
    def m(d):
        d["data"]["results"][0]["seed"] = "完全不同的种子"
    sc = _scenario("suggest_ok", m, timed_out=True, returncode=-15)
    err = _assert_protocol_failure(_run(tmp_path, sc), "CLI_PROCESS_TIMEOUT")
    assert err.retryable is True and err.human_action_required is False


# --- 结构缺失 / null / 错类型 ---


@pytest.mark.parametrize("data", [{}, None, [], "x"], ids=["empty-dict", "null", "list", "str"])
def test_r2_data_not_object_or_missing_results(tmp_path, data):
    def m(d):
        d["data"] = data
    sc = _scenario("suggest_ok", m)
    if isinstance(data, dict):  # {} 有 data 但无 results 键
        code = "RESULTS_MISSING"
    else:
        code = "DATA_NOT_OBJECT"
    _assert_protocol_failure(_run(tmp_path / str(code), sc), code)


def test_r2_results_wrong_type(tmp_path):
    for i, (bad, code) in enumerate([(None, "RESULTS_MISSING"), ("abc", "RESULTS_NOT_LIST"), ({"k": 1}, "RESULTS_NOT_LIST")]):
        def m(d, bad=bad):
            d["data"]["results"] = bad
        _assert_protocol_failure(_run(tmp_path / f"c{i}", _scenario("suggest_ok", m)), code)


def test_r2_results_row_not_object(tmp_path):
    def m(d):
        d["data"]["results"] = ["not-a-dict"]
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "RESULT_ROW_INVALID")


def test_r2_suggestions_string_is_not_char_split(tmp_path):
    def m(d):
        d["data"]["results"][0]["suggestions"] = "上海"  # 旧实现会拆成 '上','海'
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "RESULT_ITEMS_NOT_LIST")


def test_r2_suggestions_item_wrong_type(tmp_path):
    def m(d):
        d["data"]["results"][0]["suggestions"] = [1, 2]
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "SUGGESTION_ITEM_TYPE")


def test_r2_completed_missing_items_field(tmp_path):
    def m(d):
        del d["data"]["results"][0]["suggestions"]
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "RESULT_ITEMS_MISSING")


def test_r2_search_notes_wrong_type(tmp_path):
    def m(d):
        d["data"]["results"][0]["notes"] = "not a list"
    _assert_protocol_failure(_run(tmp_path, _scenario("search_ok", m)), "RESULT_ITEMS_NOT_LIST")


def test_r2_search_note_row_wrong_type(tmp_path):
    def m(d):
        d["data"]["results"][0]["notes"] = ["just-a-string"]
    _assert_protocol_failure(_run(tmp_path, _scenario("search_ok", m)), "NOTE_ITEM_TYPE")


def test_r2_word_status_invalid(tmp_path):
    def m(d):
        d["data"]["results"][0]["status"] = "weird"
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "WORD_STATUS_INVALID")


# --- seed / 多词 / 重复词 ---


def test_r2_seed_missing(tmp_path):
    def m(d):
        del d["data"]["results"][0]["seed"]
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "SEED_INVALID")


def test_r2_duplicate_word_rows(tmp_path):
    def m(d):
        d["data"]["results"].append(d["data"]["results"][0])  # 同词重复
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "DUPLICATE_WORD_RESULT")


def test_r2_multi_word_rows(tmp_path):
    import copy
    def m(d):
        other = copy.deepcopy(d["data"]["results"][0])
        other["seed"] = "别的词"
        d["data"]["results"].append(other)  # 本词 + 他词
    _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "SEED_MISMATCH")


# --- AUTH/ACTION 排列：逐词 / 顶层，ACTION / 非 AUTH 矛盾 ---


def test_r2_completed_entry_action_wins(tmp_path):
    def m(d):
        d["data"]["results"][0]["error"] = {"code": "ACTION_REQUIRED", "message": "verify"}
    err = _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "WORD_ACTION_REQUIRED")
    assert err.category.value == "action_required" and err.human_action_required is True


def test_r2_top_level_action_wins(tmp_path):
    def m(d):
        d["error"] = {"code": "ACTION_REQUIRED", "message": "verify"}
    err = _assert_protocol_failure(_run(tmp_path, _scenario("search_ok", m)), "WORD_ACTION_REQUIRED")
    assert err.human_action_required is True


def test_r2_top_level_nonauth_error_exit0_not_completed(tmp_path):
    def m(d):
        d["error"] = {"code": "INTERNAL_ERROR", "message": "boom"}
    err = _assert_protocol_failure(_run(tmp_path, _scenario("suggest_ok", m)), "WORD_INTERNAL_ERROR")
    assert err.human_action_required is True  # 顶层错误/ok/exit 矛盾必须显式，不得 completed


# --- timeout / cancel 下信号优先与 rescue 严格性 ---


def test_r2_cancel_entry_auth_not_masked_by_cancel(tmp_path):
    def m(d):
        d["data"]["results"][0]["error"] = {"code": "AUTH_REQUIRED", "message": "login"}
    sc = _scenario("suggest_ok", m, cancelled=True, returncode=130)
    err = _assert_protocol_failure(_run(tmp_path, sc), "WORD_AUTH_REQUIRED")
    assert err.human_action_required is True


def test_r2_timeout_entry_action_not_masked_by_timeout(tmp_path):
    def m(d):
        d["data"]["results"][0]["error"] = {"code": "ACTION_REQUIRED", "message": "verify"}
    sc = _scenario("suggest_ok", m, timed_out=True, returncode=-15)
    err = _assert_protocol_failure(_run(tmp_path, sc), "WORD_ACTION_REQUIRED")
    assert err.human_action_required is True


def test_r2_cancel_wrong_seed_not_rescued(tmp_path):
    def m(d):
        d["data"]["results"][0]["seed"] = "别的词"
    sc = _scenario("suggest_ok", m, cancelled=True, returncode=130)
    err = _assert_protocol_failure(_run(tmp_path, sc), "CLI_CANCELLED")
    assert err.category.value == "cancelled"


def test_r2_cancel_bad_structure_not_rescued(tmp_path):
    def m(d):
        d["data"]["results"] = []  # rescue 不得把空/缺结构当合法 partial
    sc = _scenario("suggest_ok", m, cancelled=True, returncode=130)
    _assert_protocol_failure(_run(tmp_path, sc), "CLI_CANCELLED")


def test_r2_rescue_keeps_only_verified_this_word_partial(tmp_path):
    """合法本词 partial 在超时下仍被保留（不误伤既有 rescue 行为）。"""
    sc = _scenario("suggest_ok", lambda d: None, timed_out=True, returncode=-15)
    result = _run(tmp_path, sc)
    assert result.state is ResultState.partial
    assert result.error is not None and result.error.code == "CLI_PROCESS_TIMEOUT"
    assert len(result.items) == 3  # 严格核验通过的本词 suggestions 才进入
    assert result.per_word

