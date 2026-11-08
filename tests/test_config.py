"""P0-03：配置必须显式、可复现，非法值报错要带字段路径。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from xhs_discovery.config import (
    ConfigError,
    Settings,
    apply_overrides,
    load_domain_terms,
    load_intent_rules,
    load_settings,
    with_seed,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = ROOT / "config" / "default.yaml"
DEMO_YAML = ROOT / "config" / "offline-demo.yaml"
TINY_YAML = ROOT / "config" / "tests-convergence-tiny.yaml"
INTENT_YAML = ROOT / "config" / "intent_rules.yaml"
TERMS_TXT = ROOT / "config" / "domain_terms.txt"


def write_yaml(tmp_path: Path, data: dict, name: str = "cfg.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def baseline() -> dict:
    return yaml.safe_load(DEFAULT_YAML.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [DEFAULT_YAML, DEMO_YAML])
def test_shipped_configs_are_valid(path: Path):
    settings = load_settings(path)
    assert settings.run.mode == "fixture"
    assert settings.budget.body_attempts == 0 and settings.budget.author_attempts == 0
    assert settings.models.llm_enabled is False and settings.models.embedding_enabled is False


@pytest.mark.parametrize(
    "path",
    [
        DEFAULT_YAML,
        DEMO_YAML,
        TINY_YAML,
        ROOT / "config" / "live-smoke.yaml",
    ],
)
def test_first_sample_floor_is_declared_not_implied(path: Path):
    """首采保底决定"低分词到底采不采内容"，必须写在配置里，不能靠代码默认值兜。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "first_sample_posts" in data["sampling"], f"{path.name} 没有显式声明 sampling.first_sample_posts"


@pytest.mark.parametrize("path", [DEFAULT_YAML, DEMO_YAML])
def test_product_configs_keep_the_plan_convergence_thresholds(path: Path):
    """演示配置不得为了展示收敛下调门槛（DEVELOPMENT_PLAN.md 5.3）。"""
    settings = load_settings(path)
    assert settings.stopping.min_successful_suggestion_observations == 50
    assert settings.stopping.convergence_window == 50
    assert settings.stopping.new_problem_cluster_floor == 3
    assert settings.stopping.repeat_rate_ceiling == 0.85
    assert settings.stopping.branch_stall_trials == 15
    assert settings.stopping.require_content_validation is True


def test_demo_budget_cannot_reach_the_convergence_bar_so_it_stops_by_budget():
    """14 次联想预算 < 50 次最低观察：演示必然按预算/队列退出，而不是 converged。"""
    settings = load_settings(DEMO_YAML)
    assert settings.budget.suggest_attempts < settings.stopping.min_successful_suggestion_observations


def test_tiny_convergence_config_is_test_only_and_differs_from_defaults():
    """小窗口配置只用于单元测试收敛判定分支，必须与产品配置有明显差异并保留标记。"""
    settings = load_settings(TINY_YAML)
    header = TINY_YAML.read_text(encoding="utf-8").splitlines()[0]
    assert "仅单元测试" in header and "不是产品演示" in header
    assert settings.stopping.convergence_window == 8
    assert settings.stopping.min_successful_suggestion_observations == 8
    product = load_settings(DEFAULT_YAML)
    assert settings.stopping.model_dump() != product.stopping.model_dump()
    # 其它结构性判据仍走同一套校验：槽位和、权重和、采样单调
    assert settings.scheduler.slot_window == 8
    assert settings.scoring.weights.as_dict() == product.scoring.weights.as_dict()


def test_default_weights_match_the_plan_formula():
    settings = load_settings(DEFAULT_YAML)
    assert settings.scheduler.weights.as_dict() == {
        "R": 0.25, "N": 0.20, "I": 0.20, "E": 0.15, "C": 0.10, "U": 0.10
    }
    assert settings.scoring.weights.as_dict() == {"S": 0.35, "Q": 0.30, "A": 0.20, "F": 0.15}


def test_config_hash_is_deterministic_and_seed_sensitive():
    a = load_settings(DEFAULT_YAML)
    b = load_settings(DEFAULT_YAML)
    assert a.config_hash() == b.config_hash()
    changed = with_seed(a, "上海中评")
    assert changed.config_hash() != a.config_hash()
    assert changed.run.seed_raw == "上海中评"


def test_ambiguous_seed_is_preserved_verbatim():
    settings = with_seed(load_settings(DEFAULT_YAML), "初中中评 ")
    assert settings.run.seed_raw == "初中中评 "
    assert settings.config_hash() != load_settings(DEFAULT_YAML).config_hash()


def test_unknown_key_is_rejected_with_field_path(tmp_path: Path):
    data = baseline()
    data["budget"]["suggest_attemptz"] = 10
    with pytest.raises(ConfigError) as err:
        load_settings(write_yaml(tmp_path, data))
    assert "budget.suggest_attemptz" in str(err.value)
    assert "Extra" in str(err.value) or "extra" in str(err.value)


def test_section_typo_is_rejected(tmp_path: Path):
    data = baseline()
    data["budgets"] = data.pop("budget")
    with pytest.raises(ConfigError, match="budgets"):
        load_settings(write_yaml(tmp_path, data))


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d["scheduler"]["weights"].__setitem__("r", 0.40), "scheduler.weights 之和"),
        (lambda d: d["scheduler"].__setitem__("slots_random_novelty", 9), "槽位之和"),
        (lambda d: d["sampling"].__setitem__("medium_value_posts", 19), "采样档位必须单调"),
        (lambda d: d["sampling"].__setitem__("priority_medium", 0.9), "priority_high"),
        (lambda d: d["sampling"].__setitem__("first_sample_posts", 16), "first_sample_posts 不得超过"),
        (lambda d: d["budget"].__setitem__("suggest_attempts", 501), "上限 500"),
        (lambda d: d["budget"].__setitem__("max_posts_stored", 0), "无处可存"),
        (lambda d: d["budget"].__setitem__("suggest_attempts", -1), "suggest_attempts"),
        (lambda d: d["stopping"].__setitem__("convergence_window", 2), "convergence_window"),
        (lambda d: d["scoring"]["weights"].__setitem__("s", 0.9), "scoring.weights 之和"),
        (lambda d: d["retry"].__setitem__("max_retries", 3), "上限为 2"),
        (lambda d: d["retention"].__setitem__("export_days", 365), "不得小于 export_days"),
        (lambda d: d["models"].__setitem__("embedding_enabled", True) or d["models"].__setitem__("embedding_model", "qwen3-embedding-0.6b"), "budget.model_calls"),
        (lambda d: d["nlp"].__setitem__("char_ngram_max", 1), "char_ngram_max"),
        (lambda d: d["run"].__setitem__("mode", "live"), "live 必须先配置"),
        (lambda d: d["run"].__setitem__("data_dir", "D:/outside"), "相对路径"),
        (lambda d: d["sources"]["xhs_search"].__setitem__("total_process_timeout_seconds", 10),
         "total_process_timeout_seconds"),
        (lambda d: d["scheduler"].__setitem__("operators", ["telepathy"]), "未实现算子"),
    ],
)
def test_invalid_values_fail_loudly(tmp_path: Path, mutate, needle: str):
    data = baseline()
    mutate(data)
    with pytest.raises(ConfigError) as err:
        load_settings(write_yaml(tmp_path, data))
    assert needle in str(err.value)


def test_live_mode_accepted_when_cli_config_is_complete(tmp_path: Path):
    data = baseline()
    data["run"]["mode"] = "live"
    data["sources"]["xhs_search"].update(
        {
            "node_path": "D:/tools/node.exe",
            "cli_path": "D:/tools/xhs-search.mjs",
            "profile": "client-a",
            "browser": "chrome",
            "lock_dir": "D:/tools/xhs_locks",
            "locations": ["上海"],
        }
    )
    settings = load_settings(write_yaml(tmp_path, data))
    assert settings.run.mode == "live"


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d["sources"]["xhs_search"].__setitem__("cli_path", None), "必须先配置"),
        (lambda d: d["sources"]["xhs_search"].__setitem__("lock_dir", None), "必须先配置"),
        (lambda d: d["sources"]["xhs_search"].__setitem__("node_path", "node"), "绝对路径"),
        (lambda d: d["sources"]["xhs_search"].__setitem__("cli_path", "tools/xhs.mjs"), "绝对路径"),
        (lambda d: d["sources"]["xhs_search"].__setitem__("locations", []), "locations"),
        (lambda d: d["sources"]["xhs_search"].__setitem__("lock_dir", str(Path.cwd() / "data" / "locks")),
         "不得位于 run.data_dir 内"),
    ],
)
def test_live_mode_rejects_each_incomplete_shape(tmp_path: Path, mutate, needle: str):
    """live 的每一类"不完备"都要单独拒绝：缺项、相对路径、锁目录落在数据根内、地域口径缺失。"""
    data = baseline()
    data["run"]["mode"] = "live"
    data["sources"]["xhs_search"].update(
        {
            "node_path": "D:/tools/node.exe",
            "cli_path": "D:/tools/xhs-search.mjs",
            "profile": "client-a",
            "browser": "chrome",
            "lock_dir": "D:/tools/xhs_locks",
            "locations": ["上海"],
        }
    )
    mutate(data)
    with pytest.raises(ConfigError) as err:
        load_settings(write_yaml(tmp_path, data))
    assert needle in str(err.value)


def test_live_mode_allows_empty_ambiguous_seeds_but_never_borrows_fixture_ones(tmp_path: Path):
    """歧义清单留空是保守规则（不标歧义），配置层不会替真实 run 借用 fixture 的歧义集。"""
    data = baseline()
    data["run"]["mode"] = "live"
    data["sources"]["xhs_search"].update(
        {
            "node_path": "D:/tools/node.exe",
            "cli_path": "D:/tools/xhs-search.mjs",
            "profile": "client-a",
            "browser": "chrome",
            "lock_dir": "D:/tools/xhs_locks",
            "locations": ["上海"],
            "ambiguous_seeds": [],
        }
    )
    settings = load_settings(write_yaml(tmp_path, data))
    assert settings.sources.xhs_search.ambiguous_seeds == []


def test_llm_enabled_requires_env_var_names_not_values(tmp_path: Path):
    data = baseline()
    data["models"].update({"llm_enabled": True, "llm_endpoint_env": None})
    data["budget"]["model_calls"] = 10
    with pytest.raises(ConfigError, match="环境变量名"):
        load_settings(write_yaml(tmp_path, data))

    data["models"]["llm_endpoint_env"] = "XHS_LLM_ENDPOINT"
    data["models"]["llm_api_key_env"] = "XHS_LLM_API_KEY"
    data["models"]["llm_model"] = "some-model"
    settings = load_settings(write_yaml(tmp_path, data))
    assert settings.models.llm_enabled is True
    assert "sk-" not in settings.canonical_json()


def test_calibration_pool_needs_its_own_budget(tmp_path: Path):
    data = baseline()
    data["budget"]["suggest_attempts"] = 0
    data["budget"]["calibration_suggest_attempts"] = 20
    with pytest.raises(ConfigError, match="独立的 suggest 预算池"):
        load_settings(write_yaml(tmp_path, data))


def test_empty_and_malformed_files_are_reported(tmp_path: Path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="为空"):
        load_settings(empty)

    seq = tmp_path / "seq.yaml"
    seq.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="顶层必须是映射"):
        load_settings(seq)

    broken = tmp_path / "broken.yaml"
    broken.write_text("run: {seed_raw: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML 解析失败"):
        load_settings(broken)

    with pytest.raises(ConfigError, match="不存在"):
        load_settings(tmp_path / "missing.yaml")


def test_non_utf8_config_is_rejected(tmp_path: Path):
    path = tmp_path / "gbk.yaml"
    path.write_bytes("run:\n  seed_raw: 上海中考\n".encode("gbk"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_settings(path)


def test_apply_overrides_revalidates(tmp_path: Path):
    settings = load_settings(DEFAULT_YAML)
    updated = apply_overrides(settings, {"budget": {"suggest_attempts": 500}})
    assert updated.budget.suggest_attempts == 500
    assert updated.run.seed_raw == settings.run.seed_raw
    with pytest.raises(ConfigError):
        apply_overrides(settings, {"budget": {"suggest_attempts": 9999}})


def test_settings_round_trip_is_stable():
    settings = load_settings(DEFAULT_YAML)
    again = Settings.model_validate(yaml.safe_load(settings.canonical_json()))
    assert again.config_hash() == settings.config_hash()


def test_intent_rules_cover_all_eight_intents():
    rules = load_intent_rules(INTENT_YAML)
    assert len(rules.rules) == 8
    assert rules.negation_patterns and rules.comparison_patterns


def test_intent_rules_reject_missing_category(tmp_path: Path):
    data = yaml.safe_load(INTENT_YAML.read_text(encoding="utf-8"))
    data["rules"].pop(3)
    with pytest.raises(ConfigError, match="缺少类别"):
        load_intent_rules(write_yaml(tmp_path, data))


def test_intent_rules_reject_blank_pattern(tmp_path: Path):
    data = yaml.safe_load(INTENT_YAML.read_text(encoding="utf-8"))
    data["rules"][0]["patterns"] = ["   "]
    with pytest.raises(ConfigError, match="不得为空白"):
        load_intent_rules(write_yaml(tmp_path, data))


def test_domain_terms_load_and_reject_duplicates(tmp_path: Path):
    assert "志愿填报" in load_domain_terms(TERMS_TXT)
    dup = tmp_path / "dup.txt"
    dup.write_text("中考\n中考\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="重复词条"):
        load_domain_terms(dup)
    with pytest.raises(ConfigError, match="不存在"):
        load_domain_terms(tmp_path / "none.txt")


def test_copies_of_baseline_are_not_shared_state():
    data = baseline()
    original = copy.deepcopy(data)
    data["budget"]["suggest_attempts"] = 1
    assert baseline() == original
