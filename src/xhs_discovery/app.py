"""P0-E 应用服务：CLI（以及后续 UI）到已验收底层的最小接线层。

这里不实现第二套探索、账本或分析算法：装配全部复用已验收的构造入口——
`prepare_run`/`DiscoveryEngine`（P0-C）、`WorkerControl`/`request_control`（P0-B）、
`FixtureSearchSource`（P0-C）/`CliSearchSource`（P1-A）与 `run_analysis`/`export_run`（P0-D）。

三条纪律：

1. **同一 run 的"探索→分析→导出"在一个 worker 临界区内完成**：`run_guarded` 只在探索
   期间持锁，分析若发生在锁外，一个并发的 resume/extend worker 能改写同 run 的行，
   产出混合批次。本模块统一用 `control.acquire() … finally release()` 罩住整段。
2. **恢复与导出用创建时固化的口径**：run 目录 `source/` 下保存领域词典、意图规则与配置快照的
   字节副本；fixture 另存数据集副本，live 另存"是哪一份工具"（绝对路径 + 主脚本 SHA-256 +
   协议版本 + 采样口径）。进程重启后一律读副本并核对摘要，不悄悄采用可能已被修改或已升级的全局配置。
   副本自身按 SHA-256 复核，缺失或被改动明确报错，不静默降级。
3. **run_id 严格校验**：非法路径段拒绝，不 `_safe_segment` 静默清洗成另一个目录。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from xhs_discovery._version import (
    ANALYSIS_VERSION,
    CODE_VERSION,
    EXTRACTOR_VERSION,
    PROTOCOL_VERSION,
)
from xhs_discovery.config import Settings, apply_overrides, load_settings
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import ControlActionType, RunStatus
from xhs_discovery.models.run import ControlRequest
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import WorkerAlreadyRunning, WorkerControl, request_control

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 65
EXIT_MISSING = 66
EXIT_STATE_CONFLICT = 67
EXIT_UNSUPPORTED = 70

SOURCE_DIRNAME = "source"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PAUSEABLE = (RunStatus.queued.value, RunStatus.running.value)
_WORKABLE = (RunStatus.queued.value, RunStatus.running.value, RunStatus.paused.value)


class AppError(Exception):
    """应用层可预期错误：CLI 按 `exit_code` 映射稳定退出码。"""

    exit_code = EXIT_STATE_CONFLICT


class UsageError(AppError):
    exit_code = EXIT_USAGE


class SourceIntegrityError(AppError):
    exit_code = EXIT_CONFIG


class MissingRunError(AppError):
    exit_code = EXIT_MISSING


class StateConflictError(AppError):
    exit_code = EXIT_STATE_CONFLICT


class UnsupportedCapabilityError(AppError):
    exit_code = EXIT_UNSUPPORTED


# --------------------------------------------------------------------- 基础校验


def validate_run_id(run_id: str) -> str:
    """用户提供的 run_id 必须是合法单段路径名；非法一律拒绝，不清洗成另一个 ID。"""
    if not run_id or not RUN_ID_RE.match(run_id) or ".." in run_id:
        raise UsageError(
            f"非法 run_id：{run_id!r}；只允许字母/数字/下划线/点/连字符，且不得以点开头或包含路径分隔符"
        )
    return run_id


def runtime_paths(settings: Settings, cwd: Path | str) -> RuntimePaths:
    """数据路径只在配置声明的根目录下（`run.data_dir` 相对当前工作目录）。"""
    return RuntimePaths(Path(cwd) / settings.run.data_dir)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


# --------------------------------------------------------------------- 连接与 run 读取


def open_store(config_path: Path | str, cwd: Path | str) -> tuple[Repository, RuntimePaths, Settings]:
    """按 --config 定位数据根并建库/迁移；run 级处理一律用库里的 config_json 快照。"""
    settings = load_settings(Path(config_path))
    paths = runtime_paths(settings, cwd)
    conn = storage_db.connect(paths.db_path)
    return Repository(conn), paths, settings


def stored_settings(repo: Repository, run_id: str) -> Settings:
    run = repo.get_run(run_id)
    if run is None:
        raise MissingRunError(
            f"run {run_id!r} 不在该数据库；确认 --config 的 run.data_dir 指向正确的数据根"
        )
    try:
        return Settings.model_validate(json.loads(str(run["config_json"])))
    except Exception as exc:  # noqa: BLE001 - 快照损坏必须报为口径问题而不是裸 traceback
        raise SourceIntegrityError(f"run {run_id} 的 config_json 快照不可解析：{exc}") from exc


# --------------------------------------------------------------------- 来源快照（创建时固化）


def _copy_into(src: Path, dst: Path) -> str:
    if not src.is_file():
        raise MissingRunError(f"来源文件不存在：{src}")
    digest = sha256_file(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return digest


def _cli_package_identity(cli: Path) -> dict | None:
    """Fingerprint the CLI's own imported code and selectors, never a browser profile."""
    entry = cli.resolve()
    for root in (entry.parent, entry.parent.parent):
        manifest = root / "package.json"
        if not manifest.is_file():
            continue
        package = json.loads(manifest.read_text(encoding="utf-8"))
        if package.get("name") != "xhs-search-cli":
            continue
        files = {manifest, entry}
        for folder in ("bin", "src", "config"):
            files.update(p for p in (root / folder).rglob("*") if p.is_file() and p.suffix in (".mjs", ".js", ".json"))
        spec = root / "docs" / "cli-spec.json"
        if spec.is_file():
            files.add(spec)
        hashes = {}
        for path in sorted(files):
            if not path.resolve().is_relative_to(root):
                raise SourceIntegrityError("CLI package resource resolves outside its installation")
            hashes[path.relative_to(root).as_posix()] = sha256_file(path)
        return {"root": str(root), "version": package.get("version"), "files": hashes}
    return None


def _live_tool_block(settings: Settings, cwd: Path) -> dict:
    """固化 live 来源的"是哪一份工具"：绝对路径 + 主脚本字节摘要 + 协议与采样口径。

    live 快照不复制采集脚本本体（脚本依赖同目录的模块与 package.json，抄出来就跑不起），
    只固化路径与摘要，恢复时按摘要核对同一份工具还在不在、有没有被升级覆盖。
    """
    from xhs_discovery.sources.cli import (
        EXPECTED_CLI_NAME,
        SUPPORTED_SCHEMA_VERSIONS,
    )

    section = settings.sources.xhs_search
    node = Path(str(section.node_path))
    cli = Path(str(section.cli_path))
    absent = [str(path) for path in (node, cli) if not path.is_file()]
    if absent:
        raise SourceIntegrityError(
            "run.mode=live 的配置指向不存在的工具文件：" + "、".join(absent)
            + "；已测工具的绝对路径必须由配置显式给出，本包不安装、不下载、不猜路径"
        )
    payload = cli.read_bytes()
    return {
        "cli_name": EXPECTED_CLI_NAME,
        "node_path": str(node),
        "cli_path": str(cli),
        "cli_sha256": sha256_bytes(payload),
        "cli_size_bytes": len(payload),
        "package_identity": _cli_package_identity(cli),
        "profile": str(section.profile),
        "browser": str(section.browser),
        "timeout_seconds": int(section.timeout_seconds),
        "total_process_timeout_seconds": int(section.total_process_timeout_seconds),
        "min_interval_seconds": float(section.min_interval_seconds),
        "search_scrolls": int(section.search_scrolls),
        "lock_dir": str(section.lock_dir),
        "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
    }


def _live_sampling_block(settings: Settings) -> dict:
    """采样口径：一次 live run"取多少、隔多久取、每次取多深"必须与 run 绑定，恢复不换口径。"""
    budget = settings.budget
    return {
        "card_limits": {
            "high_value_posts": settings.sampling.high_value_posts,
            "medium_value_posts": settings.sampling.medium_value_posts,
            "low_value_posts": settings.sampling.low_value_posts,
        },
        "priority_thresholds": {
            "high": settings.sampling.priority_high,
            "medium": settings.sampling.priority_medium,
        },
        "external_budget": {key: getattr(budget, key) for key in budget.external_keys()},
        "max_posts_stored": budget.max_posts_stored,
        "max_runtime_minutes": budget.max_runtime_minutes,
        "retry": {
            "max_retries": settings.retry.max_retries,
            "backoff_base_seconds": settings.retry.backoff_base_seconds,
            "engine_min_interval_seconds": settings.retry.engine_min_interval_seconds,
        },
        "capabilities": "title_only：正文与作者公开笔记均不支持，不消耗外部预算、不伪造正文",
    }


def _live_normalizer_block(settings: Settings) -> dict:
    """地域与歧义口径只能来自显式配置：真实 run 绝不借用 fixture 数据集的 regions/歧义清单。"""
    section = settings.sources.xhs_search
    return {
        "locations": list(section.locations),
        "ambiguous_seeds": list(section.ambiguous_seeds),
        "rule": "locations 由配置显式声明；ambiguous_seeds 为空即保守规则——没有配置证据时不标歧义",
    }


def create_source_snapshot(
    repo: Repository,
    paths: RuntimePaths,
    run_id: str,
    settings: Settings,
    cwd: Path | str,
    *,
    at: datetime | None = None,
) -> dict:
    """把来源字节副本与口径固化到 run 目录，恢复与导出只读这里。

    fixture 固化数据集本身；live 固化"是哪一份工具"（路径/摘要/协议/采样口径）加同一套领域
    词典与意图规则，**不读取也不复制任何 fixture 数据**——真实 run 的分析口径不能来自合成集。
    """
    stamp = at or utc_now()
    cwd = Path(cwd)
    live = settings.run.mode == "live"
    # live 先验工具再固化：来源不可用时不留下一个写到一半的快照（也不会碰到真实 CLI）
    tool = _live_tool_block(settings, cwd) if live else None
    source_dir = paths.run_dir(run_id) / SOURCE_DIRNAME
    source_dir.mkdir(parents=True, exist_ok=True)
    settings_copy = source_dir / "settings_snapshot.json"
    settings_copy.write_text(settings.canonical_json(), encoding="utf-8")
    files = {
        "domain_terms.txt": {
            "origin": str(cwd / settings.nlp.domain_terms_path),
            "sha256": _copy_into(
                cwd / settings.nlp.domain_terms_path, source_dir / "domain_terms.txt"
            ),
        },
        "intent_rules.yaml": {
            "origin": str(cwd / settings.nlp.intent_rules_path),
            "sha256": _copy_into(
                cwd / settings.nlp.intent_rules_path, source_dir / "intent_rules.yaml"
            ),
        },
        "settings_snapshot.json": {
            "origin": "runs.config_json 的同内容副本",
            "sha256": sha256_file(settings_copy),
        },
    }
    meta = {
        "run_id": run_id,
        "created_at": stamp.isoformat(),
        "mode": settings.run.mode,
        "versions": {
            "code": CODE_VERSION,
            "protocol": PROTOCOL_VERSION,
            "analysis": ANALYSIS_VERSION,
            "extractor": EXTRACTOR_VERSION,
        },
        "config_hash": settings.config_hash(),
        "files": files,
    }
    if live:
        meta["fixture"] = None
        meta["tool"] = tool
        meta["sampling"] = _live_sampling_block(settings)
        meta["normalizer"] = _live_normalizer_block(settings)
    else:
        from xhs_discovery.sources.fixture import FixtureDataset

        dataset = FixtureDataset.load(
            cwd / settings.sources.fixture.base_path, settings.sources.fixture.dataset
        )
        name = f"{dataset.name}.json"
        files[name] = {
            "origin": str(dataset.path),
            "sha256": _copy_into(dataset.path, source_dir / name),
        }
        meta["fixture"] = {
            "name": dataset.name,
            "version": dataset.version,
            "digest": dataset.digest,
            "sha256": files[name]["sha256"],
            "ambiguous_seeds": list(dataset.ambiguous_seeds),
            "regions": list(dataset.regions),
        }
    (source_dir / "snapshot.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    repo.record_event(
        run_id,
        "source_snapshot",
        detail={
            "mode": meta["mode"],
            "fixture": meta["fixture"],
            "tool": meta.get("tool"),
            "files": {key: item["sha256"] for key, item in files.items()},
            "versions": meta["versions"],
            "config_hash": meta["config_hash"],
        },
        at=stamp,
    )
    return meta


def _verify_live_tool(meta: dict, run_id: str) -> dict:
    """恢复前核对固化的工具还是那一份：路径消失或被升级覆盖都明确拒绝，不换工具悄悄继续跑。"""
    from xhs_discovery.sources.cli import SUPPORTED_SCHEMA_VERSIONS

    tool = meta.get("tool") or {}
    if not tool:
        raise SourceIntegrityError(f"run {run_id} 的 live 快照缺少 tool 块：无法确定采集用的是哪份工具")
    frozen_schema = [str(v) for v in tool.get("supported_schema_versions") or []]
    if set(frozen_schema) != set(SUPPORTED_SCHEMA_VERSIONS):
        raise SourceIntegrityError(
            f"run {run_id} 固化时支持的 schemaVersion 是 {frozen_schema}，当前代码是 "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}：协议口径已变，历史 run 不按新代码重采"
        )
    for field in ("node_path", "cli_path"):
        path = Path(str(tool.get(field) or ""))
        if not path.is_file():
            raise SourceIntegrityError(
                f"run {run_id} 固化的工具文件不存在：{path}；live 来源必须与创建时同一份工具，"
                "不安装、不下载、不改用其它路径"
            )
    cli = Path(str(tool["cli_path"]))
    digest = sha256_file(cli)
    if digest != str(tool["cli_sha256"]):
        raise SourceIntegrityError(
            f"run {run_id} 固化的 CLI 脚本已被改动：{cli} 现为 {digest[:12]}…，"
            f"固化时是 {str(tool['cli_sha256'])[:12]}…（安装/升级会换掉这份工具），拒绝继续采集"
        )
    if cli.stat().st_size != int(tool["cli_size_bytes"]):
        raise SourceIntegrityError(f"run {run_id} 固化的 CLI 脚本大小与快照不一致：{cli}")
    if tool.get("package_identity") != _cli_package_identity(cli):
        raise SourceIntegrityError(f"run {run_id} 的 CLI package 版本、导入代码或选择器已改变，拒绝继续采集")
    return tool


def load_source_snapshot(paths: RuntimePaths, run_id: str) -> tuple[dict, Path]:
    source_dir = paths.run_dir(run_id) / SOURCE_DIRNAME
    snapshot_path = source_dir / "snapshot.json"
    if not snapshot_path.is_file():
        raise SourceIntegrityError(
            f"run {run_id} 缺少 source/snapshot.json：创建时的数据集/规则来源没有固化，"
            "无法按固定口径恢复或分析"
        )
    meta = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for name, item in meta["files"].items():
        target = source_dir / name
        if not target.is_file():
            raise SourceIntegrityError(f"run {run_id} 的来源副本缺失：{target}")
        digest = sha256_file(target)
        if digest != item["sha256"]:
            raise SourceIntegrityError(
                f"run {run_id} 的来源副本被改动：{name} 现为 {digest[:12]}…，"
                f"固化时是 {str(item['sha256'])[:12]}…；拒绝按当前全局配置悄悄换口径"
            )
    return meta, source_dir


# --------------------------------------------------------------------- 组件装配


@dataclass
class RunComponents:
    """一个 run 的全部可复用件：CLI 与后续 UI 都从这里拿，不再各自拼装。"""

    conn: object
    paths: RuntimePaths
    repo: Repository
    run_id: str
    settings: Settings  # 引擎/评分用：config_json 快照（nlp 路径已重定向到 run 副本）
    source: object
    engine: object
    rules: object
    control: WorkerControl

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "mode": self.settings.run.mode}


def build_components(repo: Repository, paths: RuntimePaths, run_id: str):
    """恢复路径唯一装配口：读 run 副本 + 库内快照，绝不信当前全局文件的运气。

    返回 (conn, RunComponents)；conn 由调用方持有并复用。
    """
    from xhs_discovery.analysis.lexicon import DomainLexicon
    from xhs_discovery.analysis.pipeline import AnalysisRules
    from xhs_discovery.config import load_domain_terms, load_intent_rules
    from xhs_discovery.discovery.engine import DiscoveryEngine
    from xhs_discovery.storage.ledger import Ledger
    from xhs_discovery.text.intent import IntentMatcher
    from xhs_discovery.text.normalize import KeywordNormalizer

    conn = repo.conn
    meta, source_dir = load_source_snapshot(paths, run_id)
    snapshot = stored_settings(repo, run_id)
    live = snapshot.run.mode == "live"
    if live:
        # live 来源的歧义/地域口径来自创建时固化的配置块，不来自任何合成数据集
        seeds = tuple(meta["normalizer"]["ambiguous_seeds"])
        regions = tuple(meta["normalizer"]["locations"])
        tool = _verify_live_tool(meta, run_id)
    else:
        seeds = tuple(meta["fixture"]["ambiguous_seeds"])
        regions = tuple(meta["fixture"]["regions"])
        tool = None
    settings = snapshot.model_copy(
        update={
            "nlp": snapshot.nlp.model_copy(
                update={
                    "intent_rules_path": str(source_dir / "intent_rules.yaml"),
                    "domain_terms_path": str(source_dir / "domain_terms.txt"),
                }
            )
        }
    )
    domain_terms = tuple(load_domain_terms(source_dir / "domain_terms.txt"))
    matcher = IntentMatcher(load_intent_rules(source_dir / "intent_rules.yaml"))
    normalizer = KeywordNormalizer(ambiguous_seeds=seeds, locations=regions)
    ledger = Ledger.from_settings(conn, run_id, snapshot)
    control = WorkerControl(conn, paths, run_id)
    engine = DiscoveryEngine(
        paths=paths,
        settings=settings,
        run_id=run_id,
        source=_build_source(
            paths, mode=snapshot.run.mode, meta=meta, source_dir=source_dir, tool=tool
        ),
        repo=repo,
        ledger=ledger,
        control=control,
        domain_terms=domain_terms,
        ambiguous_seeds=seeds,
        locations=regions,
        matcher=matcher,
    )
    rules = AnalysisRules(
        settings,
        lexicon=DomainLexicon(list(load_domain_terms(source_dir / "domain_terms.txt"))),
        normalizer=normalizer,
    )
    return conn, RunComponents(
        conn=conn,
        paths=paths,
        repo=repo,
        run_id=run_id,
        settings=settings,
        source=engine.source,
        engine=engine,
        rules=rules,
        control=control,
    )


def _build_source(
    paths: RuntimePaths, *, mode: str, meta: dict, source_dir: Path, tool: dict | None
):
    """按 run 的模式装配数据源：fixture 读 run 副本数据集，live 用核对过摘要的那份 CLI。"""
    from xhs_discovery.sources.fixture import FixtureDataset, FixtureSearchSource

    if mode != "live":
        return FixtureSearchSource(
            FixtureDataset.load(source_dir, str(meta["fixture"]["name"]))
        )
    from xhs_discovery.sources.cli import CliSearchSource, CliSourceConfig

    config = CliSourceConfig(
        node_path=str(tool["node_path"]),
        cli_path=str(tool["cli_path"]),
        profile=str(tool["profile"]),
        browser=str(tool["browser"]),
        timeout_seconds=int(tool["timeout_seconds"]),
        total_process_timeout_seconds=int(tool["total_process_timeout_seconds"]),
        min_interval_seconds=float(tool["min_interval_seconds"]),
        search_scrolls=int(tool["search_scrolls"]),
    )
    return CliSearchSource(
        config=config, paths=paths, out_root=paths.root, lock_dir=Path(str(tool["lock_dir"]))
    )


# --------------------------------------------------------------------- 受保护临界区


def analyze_and_export(comp: RunComponents) -> dict:
    """在已持锁的临界区内跑一批完整离线分析并导出；批次标识与算法版本分开。"""
    from xhs_discovery.analysis.pipeline import run_analysis

    stamp = utc_now()
    done = [e for e in comp.repo.events(comp.run_id, kind="analysis_export")]
    batch_tag = f"b{len(done) + 1:02d}-{stamp:%Y%m%dT%H%M%SZ}"
    outcome = run_analysis(
        comp.conn,
        comp.paths,
        comp.run_id,
        comp.settings,
        rules=comp.rules,
        snapshot=batch_tag,
        at=stamp,
    )
    return {
        "batch": batch_tag,
        "analysis_version": outcome.analysis_version,
        "extractor_version": outcome.extractor_version,
        "export_dir": outcome.export.export_dir,
        "files": [item.rel_path for item in outcome.export.files],
        "verification_ok": bool(outcome.export.verification and outcome.export.verification.ok),
        "at": stamp.isoformat(),
    }


def drive_worker(repo: Repository, paths: RuntimePaths, run_id: str, *, max_slots: int | None = None) -> dict:
    """探索→分析→导出的完整临界区：锁不在两段之间放开，杜绝混合批次。"""
    conn, comp = build_components(repo, paths, run_id)
    try:
        comp.control.acquire()
    except WorkerAlreadyRunning as exc:
        raise StateConflictError(f"拒绝启动第二个 worker：{exc}") from exc
    try:
        report = comp.engine.run(max_slots=max_slots)
        batch = analyze_and_export(comp)
    finally:
        comp.control.release()
    return {"run_id": run_id, "engine": report.as_dict(), "batch": batch}


# --------------------------------------------------------------------- 命令入口


LIVE_STANDING_NOTE = (
    "live 只在配置完备时开放：sources.xhs_search 的 node_path/cli_path/profile/browser/lock_dir/"
    "locations 必须显式配齐且工具路径为绝对路径（配置校验负责拒绝缺配置，退出码 65）；"
    "未支持的模型开关仍按 70 拒绝"
)


def reject_unsupported(settings: Settings, mode: str) -> None:
    """能力拒绝：模型仍未接入（70）。live 的"缺配置不可用"由配置校验按 65 处理，不混成一类。"""
    if settings.models.llm_enabled or settings.models.embedding_enabled:
        raise UnsupportedCapabilityError(
            "本项目不接模型：models.llm_enabled/embedding_enabled 必须为 false"
            "（本轮不下载权重、不调用外部 API；规则基线可独立运行）"
        )


def generate_run_id(settings: Settings, stamp: datetime) -> str:
    return f"run-{stamp:%Y%m%dT%H%M%SZ}-{settings.config_hash()[:8]}"


def execute_run(
    config_path: Path | str,
    cwd: Path | str,
    *,
    mode: str,
    seed: str | None = None,
    run_id: str | None = None,
) -> dict:
    """建 run、固化来源、持锁跑探索并在同一临界区完成分析与导出。"""
    settings = load_settings(Path(config_path))
    overrides: dict = {"run": {"mode": mode}}
    if seed:
        overrides["run"]["seed_raw"] = seed
    # 配置完备性在 apply_overrides 的完整重校验里检查：live 缺任何一项都是 ConfigError(65)，
    # 不会先被"缺能力"挡掉，也不会带着半套配置走到真实 CLI。
    settings = apply_overrides(settings, overrides)
    reject_unsupported(settings, mode)

    paths = runtime_paths(settings, cwd)
    conn = storage_db.connect(paths.db_path)
    repo = Repository(conn)
    stamp = utc_now()
    run_id = validate_run_id(run_id or generate_run_id(settings, stamp))
    if repo.get_run(run_id) is not None:
        raise StateConflictError(f"run {run_id!r} 已存在：不允许用同一个 id 覆盖历史轨迹")

    from xhs_discovery.discovery.engine import prepare_run

    prepare_run(repo, settings, run_id=run_id, created_at=stamp)
    try:
        create_source_snapshot(repo, paths, run_id, settings, cwd, at=stamp)
    except BaseException as exc:
        # 来源没固化成功就不能留下一个无法恢复的 running run；删库不做的情况下直接报错，
        # 后续 resume 会因缺 snapshot 被拒绝（SourceIntegrityError），不会假装可恢复。
        # 原因必须原样带出（例如 live 指向的工具文件不存在），否则用户只看到一句"固化失败"。
        raise SourceIntegrityError(
            f"来源快照固化失败：该 run 无法按固定口径恢复，已停止在探索之前（{exc}）"
        ) from exc
    return drive_worker(repo, paths, run_id)


def pause_run(
    config_path: Path | str, cwd: Path | str, run_id: str, *, note: str | None = None
) -> dict:
    """先校验 run_id 再开库：非法路径段不能顺手在别处建库/建目录。"""
    run_id = validate_run_id(run_id)
    repo, _, _ = open_store(config_path, cwd)
    return request_pause(repo, run_id, note=note)


def request_pause(repo: Repository, run_id: str, *, note: str | None = None) -> dict:
    run = repo.get_run(validate_run_id(run_id))
    if run is None:
        raise MissingRunError(f"run {run_id!r} 不存在；确认 --config 的 run.data_dir 指向正确的数据根")
    if str(run["status"]) not in _PAUSEABLE:
        raise StateConflictError(f"run {run_id} 状态为 {run['status']}，无可暂停的运行中任务")
    request_id = request_control(
        repo.conn,
        ControlRequest(
            run_id=run_id, action=ControlActionType.pause, requested_at=utc_now(), note=note
        ),
    )
    return {
        "request_id": request_id,
        "run_id": run_id,
        "note": "暂停请求已写入，worker 在下一个安全点消费；run 状态由 worker 更新，CLI 不直接篡改",
    }


def extend_run(
    config_path: Path | str,
    cwd: Path | str,
    run_id: str,
    *,
    suggest_budget: int | None = None,
    search_budget: int | None = None,
    note: str | None = None,
) -> dict:
    run_id = validate_run_id(run_id)
    if not suggest_budget and not search_budget:
        raise UsageError(
            "extend 必须给出 --suggest-budget 或 --search-budget 的正整数追加量"
            "（追加额度，不是重置总量）"
        )
    repo, _, _ = open_store(config_path, cwd)
    return request_extend(
        repo, run_id, suggest_budget=suggest_budget, search_budget=search_budget, note=note
    )


def request_extend(
    repo: Repository,
    run_id: str,
    *,
    suggest_budget: int | None = None,
    search_budget: int | None = None,
    note: str | None = None,
) -> dict:
    run = repo.get_run(validate_run_id(run_id))
    if run is None:
        raise MissingRunError(f"run {run_id!r} 不存在；确认 --config 的 run.data_dir 指向正确的数据根")
    if str(run["status"]) not in _WORKABLE:
        raise StateConflictError(
            f"run {run_id} 状态为 {run['status']}：已终结的任务不追加额度，"
            "请新建 run 以保持实验口径可解释"
        )
    if not suggest_budget and not search_budget:
        raise UsageError("extend 必须给出 --suggest-budget 或 --search-budget 的正整数追加量（追加额度，不是重置总量）")
    request_id = request_control(
        repo.conn,
        ControlRequest(
            run_id=run_id,
            action=ControlActionType.extend,
            requested_at=utc_now(),
            suggest_budget_delta=suggest_budget or None,
            search_budget_delta=search_budget or None,
            note=note,
        ),
    )
    return {
        "request_id": request_id,
        "run_id": run_id,
        "note": "extend 是追加额度：worker 在安全点把它写进 budget_revisions，已花掉的不清零",
    }


def resume_run(
    config_path: Path | str,
    cwd: Path | str,
    run_id: str,
    *,
    max_slots: int | None = None,
) -> dict:
    run_id = validate_run_id(run_id)
    repo, paths, _ = open_store(config_path, cwd)
    run = repo.get_run(run_id)
    if run is None:
        raise MissingRunError(f"run {run_id!r} 不存在；确认 --config 的 run.data_dir 指向正确的数据根")
    if str(run["status"]) not in _WORKABLE:
        raise StateConflictError(
            f"run {run_id} 状态为 {run['status']}：completed 是终态，"
            "历史 run 不可恢复（converged/cancelled 之后要重做得开新 run）"
        )
    # 已有活跃 worker 时连"恢复请求"都不写：否则请求会被那个 worker 在安全点消费，
    # 等于隔空替别人做了暂停/继续的决定。真正的互斥仍由 drive_worker 的 acquire 负责。
    occupied = WorkerControl(repo.conn, paths, run_id).status().occupied
    if occupied:
        raise StateConflictError(
            f"run {run_id} 已有活跃 worker：拒绝启动第二个 worker，也不写入会被它消费的控制请求"
        )
    request_id = request_control(
        repo.conn,
        ControlRequest(run_id=run_id, action=ControlActionType.resume, requested_at=utc_now()),
    )
    result = drive_worker(repo, paths, run_id, max_slots=max_slots)
    result["resume_request_id"] = request_id
    return result


# --------------------------------------------------------------------- 批次读取与导出


def completed_batches(repo: Repository, run_id: str) -> list[dict]:
    """已完成（反读校验通过）的分析批次，按时间先后；UI 默认选最后一批及其 analysis_version。"""
    batches = []
    for row in repo.events(run_id, kind="analysis_export"):
        detail = json.loads(row["detail_json"] or "{}")
        verification = detail.get("verification") or {}
        batches.append(
            {
                "event_id": int(row["id"]),
                "at": row["at"],
                "analysis_version": detail.get("analysis_version"),
                "extractor_version": detail.get("extractor_version"),
                "snapshot": detail.get("snapshot"),
                "export_dir": detail.get("export_dir"),
                "files": detail.get("files") or [],
                "verification": verification,
                "verification_ok": bool(verification.get("ok")),
            }
        )
        batches[-1]["file_paths"] = [
            {"filename": item["filename"], "rel_path": item["rel_path"], "sha256": item["sha256"]}
            for item in (detail.get("files") or [])
        ]
    return batches


def pick_batch(batches: list[dict], snapshot: str | None) -> dict:
    done = [batch for batch in batches if batch["verification_ok"]]
    if not done:
        raise MissingRunError(
            "该 run 没有已完成的分析批次（在线初步归并不算导出批次）；"
            "先 run/resume 跑完探索→分析→导出的受保护临界区"
        )
    if snapshot is None:
        return done[-1]
    for batch in done:
        if batch["snapshot"] == snapshot:
            return batch
    raise MissingRunError(
        f"批次 {snapshot!r} 不存在或未通过反读校验；已完成批次："
        + ", ".join(str(batch["snapshot"]) for batch in done)
    )


def snapshot_self_check(batch: dict, landed: list[dict], run_id: str) -> dict:
    """导出=读固定批次：磁盘哈希 = 事件登记哈希 = report.json 内嵌哈希。

    不按当前库重跑 `verify_export`：那项"统计与库同步"检查会把库里新增的任何事实
    （包括一次暂停请求写的 run_event）算作"报告已过期"，于是历史批次明明没被改动，
    却永远读不出来。批次写盘时已在受保护临界区内通过完整反读校验，这里只验完整性。
    """
    from xhs_discovery.analysis.export import EXPORT_FILENAMES

    problems: list[str] = []
    recorded = {str(item["filename"]) for item in landed}
    missing = [name for name in EXPORT_FILENAMES if name not in recorded]
    if missing:
        problems.append(f"批次登记缺少产物：{missing}")

    report = next((item for item in landed if item["filename"] == "report.json"), None)
    checks = 1 + len(landed)
    if report is not None:
        payload = json.loads(Path(report["abs_path"]).read_text(encoding="utf-8"))
        exported = payload.get("exported") or {}
        if str(exported.get("snapshot")) != str(batch["snapshot"]):
            problems.append(
                f"report.json 的批次标识 {exported.get('snapshot')!r} ≠ 登记 {batch['snapshot']!r}"
            )
        if str((payload.get("meta") or {}).get("analysis_version")) != str(batch["analysis_version"]):
            problems.append("report.json 的分析版本与登记批次不一致")
        if str((payload.get("run") or {}).get("id")) != run_id:
            problems.append("report.json 属于另一个 run，不得当作本 run 的导出")
        checks = 2 + len(landed)
        for item in exported.get("files") or []:
            name = str(item.get("filename"))
            digest = str(item.get("sha256"))
            target = next((row for row in landed if row["filename"] == name), None)
            if target is None:
                problems.append(f"report.json 登记了 {name}，批次事件里没有")
            elif digest != str(target["sha256"]):
                problems.append(f"{name} 的 report 内嵌哈希与登记哈希不一致")
    if problems:
        raise SourceIntegrityError("固定批次自检未通过：" + "；".join(problems[:5]))
    return {
        "ok": True,
        "mode": "fixed_snapshot",
        "checks": checks,
        "analysis_time_verification_ok": bool(batch["verification"].get("ok"))
        if batch["verification"]
        else None,
        "problems": [],
    }


def export_batch(
    config_path: Path | str,
    cwd: Path | str,
    run_id: str,
    *,
    snapshot: str | None = None,
    out: Path | str | None = None,
) -> dict:
    """读取已有 run 的固定批次：校验磁盘产物与登记哈希，可复制到 --out；绝不触发重算。"""
    from xhs_discovery.analysis.export import EXPORT_FILENAMES

    run_id = validate_run_id(run_id)
    repo, paths, _ = open_store(config_path, cwd)
    if repo.get_run(run_id) is None:
        raise MissingRunError(f"run {run_id!r} 不存在；确认 --config 的 run.data_dir 指向正确的数据根")
    batches = completed_batches(repo, run_id)
    batch = pick_batch(batches, snapshot)
    landed: list[dict] = []
    for item in batch["file_paths"]:
        target = paths.resolve_within_run(str(item["rel_path"]), run_id)
        if not target.is_file():
            raise SourceIntegrityError(
                f"批次 {batch['snapshot']} 的登记产物在磁盘上缺失：{target}；历史产物不得伪装可读"
            )
        digest = sha256_file(target)
        if digest != str(item["sha256"]):
            raise SourceIntegrityError(
                f"{item['filename']} 内容与登记哈希不符（文件 {digest[:12]}… ≠ 登记 "
                f"{str(item['sha256'])[:12]}…）：产物被改动，拒绝当作有效导出"
            )
        landed.append({**item, "abs_path": str(target)})
    verification = snapshot_self_check(batch, landed, run_id)
    delivered = None
    if out is not None:
        delivered = str(Path(cwd) / str(out)) if not Path(str(out)).is_absolute() else str(out)
        Path(delivered).mkdir(parents=True, exist_ok=True)
        for item in landed:
            if item["filename"] in EXPORT_FILENAMES:
                shutil.copyfile(item["abs_path"], Path(delivered) / item["filename"])
    return {
        "run_id": run_id,
        "batch": batch["snapshot"],
        "analysis_version": batch["analysis_version"],
        "extractor_version": batch["extractor_version"],
        "export_dir": str(paths.root / str(batch["export_dir"])),
        "files": landed,
        "verification": verification,
        "copied_to": delivered,
    }


def list_runs(repo: Repository) -> list[dict]:
    """UI/CLI 只读查询：run 摘要 + 最新已完成批次，不产生副作用、不启动 worker。"""
    rows = repo.conn.execute(
        "SELECT id, seed_raw, mode, status, outcome, created_at, updated_at, analysis_version"
        " FROM runs ORDER BY created_at DESC"
    ).fetchall()
    summary = []
    for row in rows:
        batches = [b for b in completed_batches(repo, str(row["id"])) if b["verification_ok"]]
        latest = batches[-1] if batches else None
        summary.append(
            {
                **dict(row),
                "completed_batches": len(batches),
                "latest_batch": latest["snapshot"] if latest else None,
                "latest_analysis_version": latest["analysis_version"] if latest else None,
            }
        )
    return summary


# --------------------------------------------------------------------- UI 启动入口


def ui_command(
    *,
    dry_run: bool = False,
    extra_args: tuple[str, ...] = (),
) -> dict:
    """提供真实的 Streamlit 启动命令（默认仅绑定 127.0.0.1）。

    P0-E-UI 包交付看板本体后，这里给出的就是可直接运行的启动命令；本地服务默认只绑回环地址，
    不为演示开放局域网/公网。地址类参数以 `extra_args` 覆盖为准（调用方显式给 --server.address 时不重复）。
    """
    if importlib.util.find_spec("streamlit") is None:
        raise UnsupportedCapabilityError(
            "streamlit 未安装：无法提供 ui 启动入口（pip install -e \".[dev]\" 后重试）"
        )
    entry = Path(__file__).resolve().parent / "ui" / "app.py"
    if not entry.is_file():
        raise UnsupportedCapabilityError(
            f"UI 入口 {entry} 尚未交付（属 P0-E-UI 工作包）：不提供伪装可用的启动命令"
        )
    has_address = any("--server.address" in arg or arg.startswith("--server.address=") for arg in extra_args)
    loopback = [] if has_address else ["--server.address", "127.0.0.1"]
    command = [sys.executable, "-m", "streamlit", "run", str(entry), *loopback, *extra_args]
    if dry_run:
        return {"command": command, "entry": str(entry), "launched": False}
    code = subprocess.run(command).returncode
    return {"command": command, "entry": str(entry), "launched": True, "exit_code": code}


__all__ = [
    "AppError",
    "MissingRunError",
    "RunComponents",
    "SourceIntegrityError",
    "StateConflictError",
    "UnsupportedCapabilityError",
    "UsageError",
    "build_components",
    "completed_batches",
    "create_source_snapshot",
    "drive_worker",
    "execute_run",
    "export_batch",
    "extend_run",
    "list_runs",
    "open_store",
    "pick_batch",
    "pause_run",
    "request_extend",
    "request_pause",
    "resume_run",
    "runtime_paths",
    "snapshot_self_check",
    "ui_command",
    "validate_run_id",
]
