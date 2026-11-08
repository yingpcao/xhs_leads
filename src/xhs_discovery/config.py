"""配置模型与加载（P0-03）。

原则：
- 所有阈值都是"待实验的配置项"，因此全部显式出现在配置文件里并带范围校验；
- 非法值必须报出字段路径与原因，不接受静默纠正或未知键；
- 权重之和、槽位之和、采样档位单调性这类结构不变量在加载期就失败。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from xhs_discovery._version import CODE_VERSION
from xhs_discovery.models.enums import EXPANSION_OPERATORS, IntentCode


class ConfigError(ValueError):
    """配置非法：消息中带字段路径，供 CLI 与 UI 直接展示。"""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RunSection(Strict):
    seed_raw: str = Field(description="用户原始种子输入，保留歧义，不自动改写")
    mode: Literal["fixture", "live"] = "fixture"
    random_seed: int = 20261024
    data_dir: str = "data"
    run_label: str | None = None

    @field_validator("seed_raw")
    @classmethod
    def _seed_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("seed_raw 不得为空白；种子原文按用户输入保留")
        return value

    @field_validator("data_dir")
    @classmethod
    def _relative_data_dir(cls, value: str) -> str:
        p = Path(value)
        if p.is_absolute():
            raise ValueError("data_dir 必须是项目内相对路径，禁止写到项目外")
        return value


class BudgetSection(Strict):
    """分项预算：采样预算没有隐含"无限"，未配置的动作默认 0。"""

    suggest_attempts: int = Field(default=300, ge=0)
    search_attempts: int = Field(default=60, ge=0)
    body_attempts: int = Field(default=0, ge=0)
    author_attempts: int = Field(default=0, ge=0)
    calibration_suggest_attempts: int = Field(default=0, ge=0)
    max_posts_stored: int = Field(default=400, ge=0)
    max_runtime_minutes: int = Field(default=120, ge=1)
    model_calls: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    model_cost_usd: float = Field(default=0.0, ge=0.0)

    def external_keys(self) -> tuple[str, ...]:
        return (
            "suggest_attempts",
            "search_attempts",
            "body_attempts",
            "author_attempts",
            "calibration_suggest_attempts",
        )

    @model_validator(mode="after")
    def _check(self) -> "BudgetSection":
        if self.suggest_attempts > 500:
            raise ValueError("budget.suggest_attempts 上限 500（超出需先修订计划的访问假设）")
        if self.max_posts_stored == 0 and self.search_attempts > 0:
            raise ValueError("budget.max_posts_stored=0 时不得配置 search_attempts：卡片无处可存")
        return self


class SamplingSection(Strict):
    high_value_posts: int = Field(default=15, ge=0, le=20)
    medium_value_posts: int = Field(default=5, ge=0, le=20)
    low_value_posts: int = Field(default=0, ge=0, le=2)
    priority_high: float = Field(default=0.60, gt=0.0, lt=1.0)
    priority_medium: float = Field(default=0.35, gt=0.0, lt=1.0)
    branch_min_validation_share: int = Field(default=1, ge=0, le=5)
    first_sample_posts: int = Field(
        default=0,
        ge=0,
        le=20,
        description=(
            "首采保底：还没有任何内容采样记录的词，本轮至少采几篇（0 表示只按分数档位，"
            "未采过内容的词也可能永远采不到）"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> "SamplingSection":
        if not self.high_value_posts >= self.medium_value_posts >= self.low_value_posts:
            raise ValueError(
                "采样档位必须单调：high_value_posts >= medium_value_posts >= low_value_posts"
            )
        if not self.priority_high > self.priority_medium:
            raise ValueError("优先级阈值必须 priority_high > priority_medium")
        if self.first_sample_posts > self.high_value_posts:
            raise ValueError(
                "first_sample_posts 不得超过 high_value_posts："
                "保底是给没采过的词开门，不是比高价值词采得更多"
            )
        return self


class PriorityWeights(Strict):
    r: float = Field(default=0.25, ge=0.0, le=1.0)
    n: float = Field(default=0.20, ge=0.0, le=1.0)
    i: float = Field(default=0.20, ge=0.0, le=1.0)
    e: float = Field(default=0.15, ge=0.0, le=1.0)
    c: float = Field(default=0.10, ge=0.0, le=1.0)
    u: float = Field(default=0.10, ge=0.0, le=1.0)

    def as_dict(self) -> dict[str, float]:
        return {"R": self.r, "N": self.n, "I": self.i, "E": self.e, "C": self.c, "U": self.u}

    @model_validator(mode="after")
    def _sums_to_one(self) -> "PriorityWeights":
        total = sum(self.as_dict().values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scheduler.weights 之和必须为 1.0，当前 {total:.6f}")
        return self


class DemandWeights(Strict):
    s: float = Field(default=0.35, ge=0.0, le=1.0)
    q: float = Field(default=0.30, ge=0.0, le=1.0)
    a: float = Field(default=0.20, ge=0.0, le=1.0)
    f: float = Field(default=0.15, ge=0.0, le=1.0)

    def as_dict(self) -> dict[str, float]:
        return {"S": self.s, "Q": self.q, "A": self.a, "F": self.f}

    @model_validator(mode="after")
    def _sums_to_one(self) -> "DemandWeights":
        total = sum(self.as_dict().values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scoring.weights 之和必须为 1.0，当前 {total:.6f}")
        return self


class SchedulerSection(Strict):
    slot_window: int = Field(default=20, ge=1)
    slots_high_score: int = Field(default=12, ge=0)
    slots_branch_exploration: int = Field(default=5, ge=0)
    slots_random_novelty: int = Field(default=3, ge=0)
    ucb_exploration_coefficient: float = Field(default=1.0, gt=0.0, le=5.0)
    unknown_evidence_prior: float = Field(default=0.5, gt=0.0, lt=1.0)
    cluster_keyword_weight: float = Field(default=0.2, gt=0.0, le=1.0)
    max_depth: int = Field(
        default=5,
        ge=1,
        le=12,
        description="联想纵向扩展的深度上限：父词达到该深度就不再向下追联想",
    )
    max_segment_repeats: int = Field(
        default=2,
        ge=1,
        le=6,
        description="查询词内同一 2~3 字片段允许的最大重复次数，超过即判为退化词、不入队",
    )
    operators: list[str] = Field(default_factory=lambda: list(EXPANSION_OPERATORS))
    weights: PriorityWeights = Field(default_factory=PriorityWeights)

    @model_validator(mode="after")
    def _check(self) -> "SchedulerSection":
        total = self.slots_high_score + self.slots_branch_exploration + self.slots_random_novelty
        if total != self.slot_window:
            raise ValueError(
                f"scheduler 槽位之和 {total} 必须等于 slot_window {self.slot_window}（空桶回流由调度实现处理）"
            )
        unknown = set(self.operators) - set(EXPANSION_OPERATORS)
        if unknown:
            raise ValueError(f"scheduler.operators 含未实现算子：{sorted(unknown)}")
        if not self.operators:
            raise ValueError("scheduler.operators 不得为空")
        return self


class StoppingSection(Strict):
    """收敛必须同时满足最低观察量，失败与验证码不得伪装收敛。"""

    min_successful_suggestion_observations: int = Field(default=50, ge=1)
    convergence_window: int = Field(default=50, ge=1)
    new_problem_cluster_floor: int = Field(default=3, ge=1)
    repeat_rate_ceiling: float = Field(default=0.85, gt=0.0, lt=1.0)
    branch_stall_trials: int = Field(default=15, ge=1)
    require_content_validation: bool = True

    @model_validator(mode="after")
    def _check(self) -> "StoppingSection":
        if self.convergence_window < self.new_problem_cluster_floor:
            raise ValueError(
                "stopping.convergence_window 必须 >= new_problem_cluster_floor，否则窗口内无法判定"
            )
        if self.min_successful_suggestion_observations > 500:
            raise ValueError("stopping.min_successful_suggestion_observations 超过任何可用预算上限")
        return self


class ScoringSection(Strict):
    weights: DemandWeights = Field(default_factory=DemandWeights)
    log_scale: bool = True
    quantile_min_sample: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "ScoringSection":
        if not self.log_scale:
            raise ValueError("P0 评分特征必须先做对数缩放，关闭该假设需先修订计划")
        return self


class NlpSection(Strict):
    char_ngram_min: int = Field(default=2, ge=1, le=6)
    char_ngram_max: int = Field(default=4, ge=1, le=8)
    min_phrase_freq: int = Field(default=3, ge=1)
    min_pmi: float = Field(
        default=1.0,
        ge=0.0,
        description="PMI 互信息阈值（以 log2 计）：低于该值说明两字共现与独立出现无差别，不算短语",
    )
    tfidf_batch_size: int = Field(default=25, ge=1)
    domain_terms_path: str = "config/domain_terms.txt"
    intent_rules_path: str = "config/intent_rules.yaml"

    @model_validator(mode="after")
    def _check(self) -> "NlpSection":
        if self.char_ngram_max < self.char_ngram_min:
            raise ValueError("nlp.char_ngram_max 必须 >= char_ngram_min")
        return self


class RetrySection(Strict):
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_base_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    engine_min_interval_seconds: float = Field(default=3.0, ge=0.0, le=600.0)

    @model_validator(mode="after")
    def _check(self) -> "RetrySection":
        if self.max_retries > 2:
            raise ValueError("retry.max_retries 上限为 2（计划规定有限指数退避，最多两次重试）")
        return self


class RetentionSection(Strict):
    raw_artifact_days: int = Field(default=90, ge=0)
    export_days: int = Field(default=90, ge=0)
    minimize_author_data: bool = True

    @model_validator(mode="after")
    def _check(self) -> "RetentionSection":
        if self.raw_artifact_days > 0 and self.raw_artifact_days < self.export_days:
            raise ValueError(
                "retention.raw_artifact_days 不得小于 export_days，否则导出失去可核查的原始来源"
            )
        return self


class ModelSection(Strict):
    """模型默认关闭：关闭时不导入重型依赖、不下载权重。"""

    embedding_enabled: bool = False
    embedding_model: str | None = None
    embedding_device: Literal["cpu", "cuda"] = "cpu"
    llm_enabled: bool = False
    llm_endpoint_env: str | None = None
    llm_api_key_env: str | None = None
    llm_model: str | None = None
    llm_max_output_tokens: int = Field(default=1200, ge=1, le=8000)
    llm_batch_size: int = Field(default=8, ge=1, le=50)
    llm_strategy_diagnosis_every_searches: int = Field(default=50, ge=10)

    @model_validator(mode="after")
    def _check(self) -> "ModelSection":
        if self.llm_enabled:
            missing = [
                name
                for name, value in (
                    ("llm_endpoint_env", self.llm_endpoint_env),
                    ("llm_api_key_env", self.llm_api_key_env),
                    ("llm_model", self.llm_model),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"models.llm_enabled=true 必须配置环境变量名（不写密钥值）：{missing}"
                )
        if self.embedding_enabled and not self.embedding_model:
            raise ValueError("models.embedding_enabled=true 必须指定 embedding_model")
        return self


class FixtureSection(Strict):
    dataset: str = "shanghai_zhongkao"
    base_path: str = "tests/fixtures"

    @field_validator("base_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError("sources.fixture.base_path 必须是项目内相对路径")
        return value


class XhsSearchSection(Strict):
    """真实 CLI 来源（P1-B 接线）：字段全部显式配置，live 模式缺任何一项都在加载期拒绝。

    `node_path`/`cli_path`/`lock_dir` 在 live 下必须是绝对路径：固定"已测的那份工具"，
    避免 PATH 或全局安装漂移后悄悄换了口径。`lock_dir` 是跨数据根共享的本机 profile 互斥目录，
    不得落在 `run.data_dir` 内（既不能只对本数据根生效，也不能被恢复扫描当成采集产物）。
    """

    node_path: str = "node"
    cli_path: str | None = None
    profile: str | None = None
    browser: str | None = None
    timeout_seconds: int = Field(default=90, ge=5, le=900)
    total_process_timeout_seconds: int = Field(default=180, ge=10, le=3600)
    min_interval_seconds: float = Field(default=2.5, ge=1.0, le=600.0)
    search_scrolls: int = Field(default=5, ge=0, le=20)
    lock_dir: str | None = None
    locations: list[str] = Field(
        default_factory=list,
        description="真实采样的地域口径：必须显式配置，不得借用 fixture 数据伪造",
    )
    ambiguous_seeds: list[str] = Field(
        default_factory=list,
        description="歧义种子清单：同样必须显式配置，缺省即保守地不标歧义",
    )

    @field_validator("locations", "ambiguous_seeds")
    @classmethod
    def _no_blank_items(cls, value: list[str]) -> list[str]:
        if any(not str(item).strip() for item in value):
            raise ValueError("地域/歧义种子清单不得含空白项")
        return value

    @model_validator(mode="after")
    def _check(self) -> "XhsSearchSection":
        if self.total_process_timeout_seconds <= self.timeout_seconds:
            raise ValueError(
                "sources.xhs_search.total_process_timeout_seconds 必须大于单操作 timeout_seconds"
            )
        return self

    def check_live_complete(self, data_dir: str) -> None:
        """live 模式的配置完备性：缺字段/相对路径/锁目录落在数据根内都是"配置不完整"，一律拒绝。

        这替代了 P0 的"live 永远拒绝"：能力已接通，但只有把已测工具的绝对路径、profile、
        browser、跨数据根共享的锁目录都显式配齐，才允许走到真实 CLI。
        """
        missing = [
            name
            for name, value in (
                ("sources.xhs_search.node_path", self.node_path),
                ("sources.xhs_search.cli_path", self.cli_path),
                ("sources.xhs_search.profile", self.profile),
                ("sources.xhs_search.browser", self.browser),
                ("sources.xhs_search.lock_dir", self.lock_dir),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"run.mode=live 必须先配置 {missing}")
        if self.timeout_seconds > 120:
            raise ValueError("sources.xhs_search.timeout_seconds 超过 xhs-search spec 的 120 秒上限")
        relative = [
            name
            for name, value in (
                ("node_path", self.node_path),
                ("cli_path", self.cli_path),
                ("lock_dir", self.lock_dir),
            )
            if not Path(str(value)).is_absolute()
        ]
        if relative:
            raise ValueError(
                "run.mode=live 必须写绝对路径（固定已测工具与共享锁目录，不随 PATH/工作目录漂移）："
                + str([f"sources.xhs_search.{name}" for name in relative])
            )
        inside = Path(str(self.lock_dir)).resolve()
        root = (Path.cwd() / data_dir).resolve()
        if inside == root or root in inside.parents:
            raise ValueError(
                "sources.xhs_search.lock_dir 不得位于 run.data_dir 内：同 profile 互斥要跨数据根共享，"
                "且数据根里的文件会被恢复扫描当作采集产物"
            )
        if not self.locations:
            raise ValueError(
                "run.mode=live 必须显式配置 sources.xhs_search.locations（地域口径）："
                "不得借用 fixture 数据集的 regions 伪造真实采样的地域"
            )
        # ambiguous_seeds 允许留空：空清单就是保守规则——没有显式配置的歧义证据时一律不标歧义，
        # 绝不借用 fixture 数据集的歧义清单替真实采样下结论。


class SourcesSection(Strict):
    fixture: FixtureSection = Field(default_factory=FixtureSection)
    xhs_search: XhsSearchSection = Field(default_factory=XhsSearchSection)


class Settings(Strict):
    version: int = 1
    run: RunSection
    budget: BudgetSection = Field(default_factory=BudgetSection)
    sampling: SamplingSection = Field(default_factory=SamplingSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    stopping: StoppingSection = Field(default_factory=StoppingSection)
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    nlp: NlpSection = Field(default_factory=NlpSection)
    retry: RetrySection = Field(default_factory=RetrySection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
    models: ModelSection = Field(default_factory=ModelSection)
    sources: SourcesSection = Field(default_factory=SourcesSection)

    @model_validator(mode="after")
    def _check_cross_section(self) -> "Settings":
        if self.run.mode == "live":
            self.sources.xhs_search.check_live_complete(self.run.data_dir)
        if self.budget.calibration_suggest_attempts and self.budget.suggest_attempts == 0:
            raise ValueError("覆盖校准抽样需要独立的 suggest 预算池，且不能借用主动探索预算")
        models_on = self.models.llm_enabled or self.models.embedding_enabled
        if models_on and self.budget.model_calls == 0:
            raise ValueError(
                "已启用模型但未配置 budget.model_calls：模型调用必须有显式上限，不存在隐含无限"
            )
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class IntentRule(Strict):
    intent: IntentCode
    patterns: list[str] = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0.0, le=1.0)

    @field_validator("patterns")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v and v.strip()]
        if len(cleaned) != len(value):
            raise ValueError("intent 规则模式不得为空白")
        return cleaned


class IntentRules(Strict):
    version: int = 1
    rules: list[IntentRule] = Field(min_length=1)
    negation_patterns: list[str] = Field(default_factory=list)
    comparison_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _covers_all_intents(self) -> "IntentRules":
        seen = [rule.intent for rule in self.rules]
        duplicated = {i for i in seen if seen.count(i) > 1}
        if duplicated:
            raise ValueError(f"intent 规则重复：{sorted(str(d) for d in duplicated)}")
        missing = set(IntentCode) - set(seen)
        if missing:
            raise ValueError(f"intent 规则缺少类别：{sorted(str(m) for m in missing)}")
        if not self.negation_patterns or not self.comparison_patterns:
            raise ValueError(
                "negation_patterns 与 comparison_patterns 必须配置：否定与比较对象是归并的结构属性"
            )
        return self


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件必须是 UTF-8：{path}（{exc}）") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败：{path} — {exc}") from exc


def _format_validation_error(exc: Exception, prefix: str) -> ConfigError:
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "<root>"
            lines.append(f"  - {prefix}{loc}: {err['msg']}")
        return ConfigError("配置非法：\n" + "\n".join(lines))
    return ConfigError(f"配置非法：{prefix}{exc}")


def load_settings(path: Path) -> Settings:
    """加载并校验配置；未知键与非法值都直接报错，不做默认纠正。"""
    data = _read_yaml(Path(path))
    if data is None:
        raise ConfigError(f"配置文件为空：{path}")
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射，当前是 {type(data).__name__}：{path}")
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise _format_validation_error(exc, "") from exc


def load_intent_rules(path: Path) -> IntentRules:
    data = _read_yaml(Path(path))
    if not isinstance(data, dict):
        raise ConfigError(f"意图规则顶层必须是映射：{path}")
    try:
        return IntentRules.model_validate(data)
    except ValidationError as exc:
        raise _format_validation_error(exc, "intent_rules.") from exc


def load_domain_terms(path: Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"领域词典不存在：{p}")
    terms = [line.strip() for line in p.read_text(encoding="utf-8").splitlines()]
    cleaned = [t for t in terms if t and not t.startswith("#")]
    if len(set(cleaned)) != len(cleaned):
        raise ConfigError(f"领域词典存在重复词条：{p}")
    return cleaned


CONFIG_VERSION_FOR_CODE = 1


def with_seed(settings: Settings, seed_raw: str) -> Settings:
    """替换种子原文并完整重校验（配置里的 seed_raw 是用户原始输入，不做改写）。"""
    return apply_overrides(settings, {"run": {"seed_raw": seed_raw}})


def apply_overrides(settings: Settings, overrides: dict) -> Settings:
    """深合并覆盖后重新校验；UI 与 CLI 的参数只能走这条路，避免半更新状态。"""
    data = json.loads(settings.canonical_json())

    def merge(target: dict, patch: dict) -> None:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = value

    merge(data, overrides)
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise _format_validation_error(exc, "") from exc


__all__ = [
    "ConfigError",
    "Settings",
    "RunSection",
    "BudgetSection",
    "SamplingSection",
    "SchedulerSection",
    "StoppingSection",
    "ScoringSection",
    "NlpSection",
    "RetrySection",
    "RetentionSection",
    "ModelSection",
    "SourcesSection",
    "PriorityWeights",
    "DemandWeights",
    "IntentRules",
    "IntentRule",
    "load_settings",
    "load_intent_rules",
    "load_domain_terms",
    "with_seed",
    "apply_overrides",
    "CODE_VERSION",
    "CONFIG_VERSION_FOR_CODE",
]
