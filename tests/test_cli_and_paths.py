"""P0-02：启动入口、退出码与产物路径布局。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from xhs_discovery.cli import (
    EXIT_CONFIG,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_USAGE,
    main,
)
from xhs_discovery.models.enums import OperationType, ResultState
from xhs_discovery.paths import RuntimePaths

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = ROOT / "config" / "default.yaml"
DEMO_YAML = ROOT / "config" / "offline-demo.yaml"
BROKEN_YAML = ROOT / "tests" / "fixtures" / "invalid_config.yaml"


def test_version_flag_and_help_exit_codes(capsys):
    assert main(["--version"]) == EXIT_OK
    assert "xhs-discovery" in capsys.readouterr().out
    assert main([]) == EXIT_USAGE


def test_doctor_reports_interpreter_and_confirms_no_model_deps():
    # UI test collection legitimately imports Streamlit in the pytest process.
    # Check the actual CLI startup boundary in a fresh interpreter instead.
    proc = subprocess.run(
        [sys.executable, "-m", "xhs_discovery", "doctor", "--config", str(DEMO_YAML)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == EXIT_OK, proc.stderr
    out = proc.stdout
    assert sys.executable in out
    assert "core imports" in out and "none" in out
    assert "VALID" in out
    assert "llm=False" in out


def test_doctor_without_config_still_succeeds(capsys):
    assert main(["doctor"]) == EXIT_OK
    assert "platform" in capsys.readouterr().out


def test_contracts_selfcheck_rejects_illegal_samples(capsys):
    assert main(["contracts"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "rejected=3/3" in out
    assert "post_body=unsupported" in out


def test_config_check_prints_hash_and_seed_verbatim(capsys):
    assert main(["config-check", "--config", str(DEFAULT_YAML)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "VALID" in out
    assert "seed_raw='上海中考'" in out


def test_config_check_with_ambiguous_seed_is_not_rewritten(capsys):
    assert main(["config-check", "--config", str(DEFAULT_YAML), "--seed", "上海中评"]) == EXIT_OK
    assert "seed_raw='上海中评'" in capsys.readouterr().out


def test_invalid_config_returns_config_exit_code(capsys):
    code = main(["config-check", "--config", str(BROKEN_YAML)])
    assert code == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "配置错误" in err
    assert "scheduler.weights 之和" in err


def test_missing_config_file_reports_path(capsys):
    assert main(["config-check", "--config", str(ROOT / "nope.yaml")]) == EXIT_CONFIG
    assert "不存在" in capsys.readouterr().err


def test_live_mode_without_complete_config_is_refused_as_config_error(capsys):
    """live 已接线，但缺配置就是不可用：按配置口径拒绝（65），绝不带着半套配置去碰真实 CLI。"""
    code = main(["run", "--mode", "live", "--seed", "上海中考", "--config", str(DEMO_YAML)])
    assert code == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "live" in err and "sources.xhs_search" in err


def test_live_mode_with_complete_config_is_not_refused_as_missing_capability(
    tmp_path: Path, capsys, monkeypatch
):
    """配置完备的 live 不再按"缺能力"拒绝：走到工具存在性检查（本机没有已测工具时按 65 停下）。"""
    import yaml

    data = yaml.safe_load(DEMO_YAML.read_text(encoding="utf-8"))
    data["run"]["mode"] = "live"
    data["run"]["data_dir"] = "live_root"
    data["sources"]["xhs_search"] = {
        "node_path": str(tmp_path / "tools" / "node.exe"),
        "cli_path": str(tmp_path / "tools" / "xhs-search.mjs"),
        "profile": "default",
        "browser": "msedge",
        "lock_dir": str(tmp_path / "shared_locks"),
        "locations": ["上海"],
    }
    config = tmp_path / "live_complete.yaml"
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert main(["config-check", "--config", str(config)]) == EXIT_OK  # 完备 live 配置本身合法
    monkeypatch.chdir(tmp_path)  # 隔离数据根：不往仓库 data/ 写任何 run
    code = main(["run", "--mode", "live", "--seed", "上海中考", "--config", str(config)])
    assert code == EXIT_CONFIG  # 不是 70：能力已接，停在"配置指向的工具文件不存在"
    err = capsys.readouterr().err
    assert "不接模型" not in err
    assert "工具文件" in err


def test_enabled_model_switch_is_refused_in_p0(tmp_path: Path, capsys):
    """模型开关开着但本项目不接模型：明确拒绝（70），不下载权重、不调外部 API。"""
    text = DEMO_YAML.read_text(encoding="utf-8")
    enabled = (
        text.replace("  embedding_enabled: false", "  embedding_enabled: true\n  embedding_model: bge-m3")
        .replace("  model_calls: 0", "  model_calls: 5")
    )
    config = tmp_path / "models_on.yaml"
    config.write_text(enabled, encoding="utf-8")
    assert main(["config-check", "--config", str(config)]) == EXIT_OK  # 配置本身合法
    assert main(["run", "--mode", "fixture", "--seed", "上海中考", "--config", str(config)]) == (
        EXIT_NOT_IMPLEMENTED
    )
    assert "模型" in capsys.readouterr().err


def test_control_commands_still_reject_illegal_run_id(capsys):
    """命令接线后仍要拒绝非法 run_id：不清洗成别人的目录。"""
    for bad in ("../escape", "runs/a", "a/b", ".hidden"):
        assert main(["pause", "--run", bad, "--config", str(DEMO_YAML)]) == EXIT_USAGE
    assert "run_id" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["pause"], ["resume"], ["extend"], ["export"]])
def test_run_scoped_commands_require_arguments(argv):
    """缺 --run 属用法错误：argparse 以 SystemExit(2) 表现，不静默默认某个 run。"""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == EXIT_USAGE


def test_run_rejects_live_mode_with_incomplete_config(tmp_path: Path):
    """进程入口同样按配置口径拒绝 live 缺配置（65），不再当作"未实现能力"（70）。"""
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    shutil.copyfile(DEMO_YAML, project / "config" / "offline-demo.yaml")
    result = subprocess.run(
        [sys.executable, "-m", "xhs_discovery", "run", "--mode", "live", "--seed", "上海中考",
         "--config", "config/offline-demo.yaml"],
        capture_output=True, text=True, cwd=project,
    )
    assert result.returncode == EXIT_CONFIG, result.stdout + result.stderr
    assert "live" in result.stderr


def test_module_entry_point_is_invocable():
    result = subprocess.run(
        [sys.executable, "-m", "xhs_discovery", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "protocol" in result.stdout


def test_attempt_directories_are_isolated_per_operation(tmp_path: Path):
    paths = RuntimePaths(root=tmp_path)
    a1 = paths.attempt_dir("run-a", 7, OperationType.suggest, 0, 1)
    a2 = paths.attempt_dir("run-a", 7, OperationType.suggest, 0, 2)
    a3 = paths.attempt_dir("run-a", 7, OperationType.suggest, 1, 1)
    b1 = paths.attempt_dir("run-b", 7, OperationType.suggest, 0, 1)
    assert len({a1, a2, a3, b1}) == 4
    assert paths.db_path == tmp_path / "xhs_discovery.db"
    assert a1.relative_to(tmp_path).parts[0] == "runs"


def test_artifact_relative_paths_round_trip(tmp_path: Path):
    paths = RuntimePaths(root=tmp_path)
    target = paths.attempt_dir("run-a", 7, OperationType.search, 0, 1) / "stdout.json"
    rel = paths.relative(target)
    assert rel.startswith("runs/run-a/") and "\\" not in rel
    assert paths.resolve_artifact(rel) == target
    for bad in ("/etc/passwd", "runs/../../secret", "C:/Windows/x"):
        with pytest.raises(ValueError):
            paths.resolve_artifact(bad)


@pytest.mark.parametrize("value", ["runs/../../secret", r"runs\run-a\..\..\secret",
                                   r"..\run-a\stdout.json", "C:/runs/a.json",
                                   r"\\host\share\a.json", "/runs/a.json"])
def test_resolve_artifact_rejects_windows_and_posix_escape_forms(tmp_path: Path, value):
    """字符串检查 + 解析后归属检查：Windows 分隔符不能绕过 Posix 规则。"""
    paths = RuntimePaths(root=tmp_path)
    with pytest.raises(ValueError):
        paths.resolve_artifact(value)


def test_resolve_artifact_can_confine_to_a_single_run_dir(tmp_path: Path):
    """按调用方给定的 run 目录进一步收紧，避免别的 run 的产物被当作本次结果导入。"""
    paths = RuntimePaths(root=tmp_path)
    mine = paths.attempt_dir("run-a", 7, OperationType.suggest, 0, 1) / "stdout.json"
    theirs = paths.attempt_dir("run-b", 7, OperationType.suggest, 0, 1) / "stdout.json"
    assert paths.resolve_artifact(paths.relative(mine), under=paths.run_dir("run-a")) == mine
    with pytest.raises(ValueError, match="不在"):
        paths.resolve_artifact(paths.relative(theirs), under=paths.run_dir("run-a"))


def test_relative_helper_refuses_paths_outside_the_root(tmp_path: Path):
    paths = RuntimePaths(root=tmp_path / "data")
    with pytest.raises(ValueError):
        paths.relative(tmp_path / "elsewhere" / "stdout.json")


def test_run_id_sanitising_rejects_traversal(tmp_path: Path):
    paths = RuntimePaths(root=tmp_path)
    with pytest.raises(ValueError):
        paths.run_dir("../escape")


def test_export_of_contracts_as_json_for_docs(tmp_path: Path):
    """契约清单可导出，供 README 与验收报告引用实际字段。"""
    from xhs_discovery.models.enums import Capability, StatementKind

    payload = {
        "result_states": [s.value for s in ResultState],
        "capabilities": [c.value for c in Capability],
        "statement_kinds": [s.value for s in StatementKind],
    }
    (tmp_path / "contracts.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = json.loads((tmp_path / "contracts.json").read_text(encoding="utf-8"))
    assert loaded["result_states"] == ["completed", "partial", "failed", "skipped", "unsupported"]
    assert loaded["capabilities"] == ["suggestions", "search_cards", "post_body", "author_posts"]
    assert len(loaded["statement_kinds"]) == 4
