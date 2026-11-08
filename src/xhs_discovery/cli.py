"""命令行入口。

P0-A 交付 `doctor`/`config-check`/`contracts`；P0-E（P0-30）把 `run`/`pause`/`resume`/
`extend`/`export`/`ui` 接到已验收的存储、fixture 源、探索 engine、离线分析与五类导出。
应用装配一律在 `xhs_discovery.app`（CLI 与后续 UI 共用），本模块只做参数解析、
稳定退出码与人类可读输出；不在这里实现第二套探索/账本/分析算法。

退出码约定（写入 README 与验收报告）：
0 正常；2 用法/非法参数（含非法 run_id）；65 配置或来源快照口径漂移（含 live 配置不完备）；
66 run/批次不存在；67 状态冲突（重复 worker、已终结任务、重复 run_id）；70 未支持能力
（模型开关、未安装的启动依赖）——缺能力一律明确拒绝，不提供假装可用的实现。
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from importlib import metadata
from pathlib import Path

from xhs_discovery._version import (
    ANALYSIS_VERSION,
    CODE_VERSION,
    EXTRACTOR_VERSION,
    PROTOCOL_VERSION,
)
from xhs_discovery.config import ConfigError, load_settings, with_seed
from xhs_discovery.models.enums import Capability
from xhs_discovery.sources.base import SearchSource  # noqa: F401  (契约可用性检查)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 65
EXIT_MISSING = 66  # run/批次/数据输入不可用（与 app.MissingRunError 同一码，本地定义以保持 CLI 轻导入）
EXIT_NOT_IMPLEMENTED = 70  # 未支持能力拒绝（P0-E 起等价于 app.UnsupportedCapabilityError）

CORE_IMPORTS = (
    "xhs_discovery.config",
    "xhs_discovery.models",
    "xhs_discovery.sources.base",
    "xhs_discovery.paths",
)
HEAVY_MODULES = ("jieba", "sklearn", "streamlit", "torch", "transformers", "sentence_transformers")
OPTIONAL_PACKAGES = ("pydantic", "PyYAML", "networkx", "streamlit", "jieba", "scikit-learn", "pytest")
DEFAULT_CONFIG = "config/default.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xhs-discovery",
        description="小红书长尾需求发现引擎（P0 离线闭环）",
    )
    parser.add_argument("--version", action="store_true", help="打印代码与协议版本")
    sub = parser.add_subparsers(dest="command")

    p_doctor = sub.add_parser("doctor", help="检查解释器、依赖与核心契约可导入性")
    p_doctor.add_argument("--config", default=None, help="同时校验配置文件")

    p_check = sub.add_parser("config-check", help="校验配置并打印规范化哈希")
    p_check.add_argument("--config", required=True)
    p_check.add_argument("--seed", default=None, help="覆盖种子原文（不改写、不做歧义替换）")

    p_run = sub.add_parser(
        "run",
        help="建立并运行一个任务：探索→分析→导出在同一 worker 临界区内完成",
    )
    p_run.add_argument("--mode", choices=["fixture", "live"], required=True,
                       help="fixture=合成数据集（离线）；live=已配置的本地 CLI（配置不完备一律拒绝，退出码 65）")
    p_run.add_argument("--seed", required=True, help="种子原文（保留歧义，不改写）")
    p_run.add_argument("--config", required=True, help="配置 YAML（含预算、采样与数据根 run.data_dir）")
    p_run.add_argument("--run", dest="run_id", default=None,
                       help="可选：指定 run_id（只允许字母/数字/._-，非法即拒绝而不是清洗）")

    p_pause = sub.add_parser("pause", help="写入暂停请求；worker 在安全点消费")
    p_pause.add_argument("--run", dest="run_id", required=True)
    p_pause.add_argument("--config", default=DEFAULT_CONFIG, help="用于定位 run.data_dir 数据根")

    p_resume = sub.add_parser(
        "resume",
        help="恢复已有 run：写恢复请求并以前台 worker 继续账本与队列（已有活跃 worker 时拒绝，退出码 67）",
    )
    p_resume.add_argument("--run", dest="run_id", required=True)
    p_resume.add_argument("--config", default=DEFAULT_CONFIG)
    p_resume.add_argument("--max-slots", type=int, default=None,
                          help="本次调用最多处理的槽位数（调试/演示用；不改变预算与收敛判定）")

    p_extend = sub.add_parser(
        "extend",
        help="追加探索额度：只增不减，绝不重置已花掉的量；worker 在安全点写入 budget_revisions",
    )
    p_extend.add_argument("--run", dest="run_id", required=True)
    p_extend.add_argument("--config", default=DEFAULT_CONFIG)
    p_extend.add_argument("--suggest-budget", type=int, default=None,
                          help="追加的联想尝试次数（正整数）")
    p_extend.add_argument("--search-budget", type=int, default=None,
                          help="追加的搜索尝试次数（正整数）")

    p_export = sub.add_parser(
        "export",
        help="读取已有 run 的已完成分析批次，校验并输出五类产物路径（不触发重算）",
    )
    p_export.add_argument("--run", dest="run_id", required=True)
    p_export.add_argument("--config", default=DEFAULT_CONFIG)
    p_export.add_argument("--snapshot", default=None,
                          help="指定批次标识（缺省=最新已完成批次；旧批次只做哈希校验，不按当前库重验）")
    p_export.add_argument("--out", default=None,
                          help="把该批次的五类产物复制到指定目录（run 目录内的登记原件不动）")

    p_ui = sub.add_parser(
        "ui",
        help="启动 Streamlit 入口（看板实现属 P0-E-UI 包；当前提供真实启动命令与只读 run 列表）",
    )
    p_ui.add_argument("--dry-run", action="store_true", help="只打印将要执行的启动命令，不启动服务")

    sub.add_parser(
        "contracts", help="自检数据源/结果状态/证据契约是否可构造与可拒绝"
    )
    return parser


def _pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "未安装"


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"python            : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"executable        : {sys.executable}")
    print(f"platform          : {platform.system()} {platform.release()}")
    print(f"code/protocol     : {CODE_VERSION}/{PROTOCOL_VERSION}")
    print(f"analysis/extractor: {ANALYSIS_VERSION}/{EXTRACTOR_VERSION}")
    for name in OPTIONAL_PACKAGES:
        print(f"dep {name:<20}: {_pkg_version(name)}")

    import importlib

    for module_name in CORE_IMPORTS:
        importlib.import_module(module_name)
    loaded_heavy = [m for m in HEAVY_MODULES if m in sys.modules]
    print(f"core imports      : OK（模型/重型依赖未导入：{loaded_heavy or 'none'}）")

    if args.config:
        settings = load_settings(args.config)
        print(f"config            : VALID {args.config} hash={settings.config_hash()[:12]}")
        print(f"mode/seed         : {settings.run.mode} / {settings.run.seed_raw}")
        print(
            "budget            : "
            f"suggest={settings.budget.suggest_attempts} search={settings.budget.search_attempts} "
            f"body={settings.budget.body_attempts} author={settings.budget.author_attempts} "
            f"calibration={settings.budget.calibration_suggest_attempts}"
        )
        print(f"models            : llm={settings.models.llm_enabled} embedding={settings.models.embedding_enabled}")
    return EXIT_OK


def cmd_config_check(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    if args.seed:
        settings = with_seed(settings, args.seed)
    print(f"VALID config={args.config} hash={settings.config_hash()}")
    print(f"seed_raw={settings.run.seed_raw!r} mode={settings.run.mode}")
    return EXIT_OK


def cmd_contracts(_args: argparse.Namespace) -> int:
    """自检 P0-04 契约：合法样例可构造，越界样例必须被拒绝。"""
    from datetime import datetime, timezone

    from pydantic import ValidationError

    from xhs_discovery.models import (
        Capabilities,
        KeywordText,
        OperationContext,
        ResultState,
        SessionRef,
        SuggestionItem,
        SuggestionResult,
        error_from_exit_code,
    )
    from xhs_discovery.models.enums import OperationType

    now = datetime.now(timezone.utc)
    ctx = OperationContext(
        run_id="selfcheck",
        keyword_id=1,
        keyword=KeywordText(raw="上海中考", normalized="上海中考"),
        operation_type=OperationType.suggest,
        revisit_seq=0,
        attempt_no=1,
    )
    session = SessionRef(
        session_id="selfcheck-1",
        collected_at=now,
        source_name="contracts-selfcheck",
        source_version=CODE_VERSION,
        synthetic=True,
    )
    ok = SuggestionResult(
        state=ResultState.completed,
        context=ctx,
        session=session,
        items=[SuggestionItem(text="上海中考 志愿填报", rank=1)],
    )
    rejected = 0
    checks = 3
    try:
        SuggestionResult(
            state=ResultState.failed,
            context=ctx,
            session=session,
            items=[SuggestionItem(text="非法：失败仍带数据", rank=1)],
        )
    except ValidationError:
        rejected += 1
    try:
        SuggestionResult(state=ResultState.completed, context=ctx, session=session, error=None)
        SuggestionResult(
            state=ResultState.completed,
            context=ctx,
            session=session,
            error=error_from_exit_code(3, "样例"),
        )
    except ValidationError:
        rejected += 1
    try:
        # 未取得 keyword_id 又没声明预入库的上下文不得进入调度与预算扣减
        OperationContext(
            run_id="selfcheck",
            keyword=KeywordText(raw="上海中考", normalized="上海中考"),
            operation_type=OperationType.suggest,
            revisit_seq=0,
            attempt_no=1,
        )
    except ValidationError:
        rejected += 1

    caps = Capabilities.title_only()
    print(f"contracts          : valid={ok.state.value} rejected={rejected}/{checks}")
    print(
        "capabilities       : "
        + " ".join(
            f"{cap.value}={caps.state_of(cap).value}"
            for cap in (
                Capability.suggestions,
                Capability.search_cards,
                Capability.post_body,
                Capability.author_posts,
            )
        )
    )
    if rejected != checks:
        print("契约自检未拒绝非法样例", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK


# ---------------------------------------------------------------- P0-E 接线子命令


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_run(args: argparse.Namespace) -> int:
    from xhs_discovery import app

    _emit(app.execute_run(args.config, Path.cwd(), mode=args.mode, seed=args.seed, run_id=args.run_id))
    return EXIT_OK


def cmd_pause(args: argparse.Namespace) -> int:
    from xhs_discovery import app

    _emit(app.pause_run(args.config, Path.cwd(), args.run_id))
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    from xhs_discovery import app

    _emit(app.resume_run(args.config, Path.cwd(), args.run_id, max_slots=args.max_slots))
    return EXIT_OK


def cmd_extend(args: argparse.Namespace) -> int:
    from xhs_discovery import app

    _emit(
        app.extend_run(
            args.config,
            Path.cwd(),
            args.run_id,
            suggest_budget=args.suggest_budget,
            search_budget=args.search_budget,
        )
    )
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    from xhs_discovery import app

    _emit(app.export_batch(args.config, Path.cwd(), args.run_id, snapshot=args.snapshot, out=args.out))
    return EXIT_OK


def cmd_ui(args: argparse.Namespace) -> int:
    from xhs_discovery import app

    result = app.ui_command(dry_run=args.dry_run)
    _emit(result)
    return int(result.get("exit_code", 0))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"xhs-discovery {CODE_VERSION} protocol {PROTOCOL_VERSION}")
        return EXIT_OK

    handlers = {
        "doctor": cmd_doctor,
        "config-check": cmd_config_check,
        "contracts": cmd_contracts,
        "run": cmd_run,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "extend": cmd_extend,
        "export": cmd_export,
        "ui": cmd_ui,
    }
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE

    from xhs_discovery import app

    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"配置错误：\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except app.AppError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        # 数据根不可写/无权限：明确报错并非零退出，不静默换路径继续跑
        print(f"无法访问数据文件或目录：{exc}", file=sys.stderr)
        return EXIT_MISSING
    except sqlite3.Error as exc:
        print(f"无法打开或写入运行数据库（数据根被占用或无权限）：{exc}", file=sys.stderr)
        return EXIT_MISSING
