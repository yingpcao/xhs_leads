"""xhs-search CLI 数据源适配器（P1-A：协议/数据源适配，仅 suggest / search 单词调用）。

设计边界（DEVELOPMENT_PLAN.md 第 6 节 P1、CHECKLIST P1-01～04、本机工具预检）：
- 只用 `node <配置的 mjs 路径> suggest|search` 单词调用，`shell=False` + 参数数组，隐藏 Windows 子进程窗口；
  不用 collect/batch 偷合预算，suggest 与 search 分别由引擎按 attempt 计量。
- 真实退出码与 stdout 单 JSON 的 schemaVersion/cliVersion/command/seed/逐词 status 做一致性核验；
  未知协议 / 损坏 JSON / 空 stdout / 额外 stdout 文本 / 退出码矛盾 / 错词响应一律不伪装 completed 空结果。
- 复用 `models.errors` 的 ErrorInfo 分类，不另起并行错误体系（本模块只补协议级新码到既有表）。
- 每 attempt 唯一输出目录、原子落盘、拒绝覆盖既有不同内容；stdout/stderr/工具 out/元数据都可恢复定位，
  走与 `paths.RuntimePaths` + `storage.artifacts` 一致的分层与内容寻址纪律（P1-B 起由引擎按
  `raw_artifacts` 校验哈希/路径归属并登记 operation_id/attempt_id）。
- 同 profile 串行：复用 `storage.worker.OsGuard`（互斥文件永不删除），跨实例/进程保守最小间隔（默认 2.5s，
  可注入测试时钟）；profile_busy 是明确可重试状态，绝不起第二个浏览器。
- CLI `--timeout`（单操作 ms）与项目 `total_process_timeout_seconds`（本进程墙钟上限）相互独立；超时/取消
  只回收自己启动的子进程，不终止用户浏览器或他人 profile，保留可验证 partial / uncertain，不自动重发（重试留引擎）。

本包不打开 live run 入口、不重构已验收引擎、不接模型、不读凭据、不再访问真实平台。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from xhs_discovery._version import CODE_VERSION
from xhs_discovery.models.common import RawArtifactRef, SessionRef, utc_now
from xhs_discovery.models.enums import (
    Capability,
    ErrorCategory,
    OperationType,
    ResultState,
)
from xhs_discovery.models.errors import ErrorInfo, classify_cli_failure
from xhs_discovery.models.source import (
    AuthorRef,
    Capabilities,
    KeywordText,
    OperationContext,
    PerWordOutcome,
    PostCard,
    PostSearchResult,
    SuggestionItem,
    SuggestionResult,
)
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.sources.base import AccessNotStarted, SearchSource
from xhs_discovery.storage.worker import OsGuard
from xhs_discovery.text.normalize import normalize_text

SOURCE_NAME = "xhs-search-cli"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})
EXPECTED_CLI_NAME = "xhs-search"
DEFAULT_MIN_INTERVAL_SECONDS = 2.5
DEFAULT_SEARCH_SCROLLS = 5
CLI_EXIT_TABLE_HINT = (0, 1, 2, 3, 4, 5, 6, 7, 130)
_WORD_STATE = {
    "completed": ResultState.completed,
    "partial": ResultState.partial,
    "failed": ResultState.failed,
    "skipped": ResultState.skipped,
}
_PAUSE_CATEGORIES = (ErrorCategory.auth, ErrorCategory.action_required)


class CliAttemptConflict(RuntimeError):
    """同一 attempt 目录已有不同内容的历史产物：拒绝覆盖，避免历史引用不可复现。"""


# --------------------------------------------------------------------------- 注入点


@dataclass(frozen=True)
class CommandResult:
    """一次子进程调用的原始结果；runner 负责真实读取字节，不在这里解析协议。"""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False


class CommandRunner(Protocol):
    def run(
        self, argv: list[str], *, out_dir: Path, timeout_seconds: float,
        cancel: "threading.Event | None",
    ) -> CommandResult: ...


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class SubprocessRunner:
    """真实本地子进程：argv 数组、shell=False、Windows 隐藏窗口、stdout/stderr 并发读取。

    只控制自己启动的子进程；总超时或取消时 terminate 该子进程（Windows 为硬终止，残留浏览器风险
    在报告中明确，绝不去杀用户浏览器或他人 profile）。stdout/stderr 用管道并发读取，防止大输出阻塞。
    """

    def __init__(self, *, cwd: Path | None = None, popen: Callable | None = None) -> None:
        self.cwd = cwd
        self._popen = popen or subprocess.Popen

    def run(
        self, argv: list[str], *, out_dir: Path, timeout_seconds: float,
        cancel: "threading.Event | None",
    ) -> CommandResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = self._popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=str(self.cwd) if self.cwd else None,
            creationflags=flags,
            shell=False,
        )
        state = {"timed_out": False, "cancelled": False}
        done = threading.Event()

        def watchdog() -> None:
            if cancel is not None:
                if cancel.wait(timeout_seconds):
                    state["cancelled"] = True
                else:
                    state["timed_out"] = True
                _terminate(proc)
                return
            # 无外部取消信号：正常完成由 done 唤醒；否则到总超时才回收本次子进程
            if not done.wait(timeout_seconds):
                state["timed_out"] = True
                _terminate(proc)

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()
        try:
            stdout, stderr = proc.communicate()
        finally:
            done.set()
            watchdog_thread.join(timeout=2.0)
        if proc.poll() is None:  # 兜底：communicate 返回后仍在运行则硬回收本次子进程
            _terminate(proc)
            proc.wait(timeout=5)
        return CommandResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout or b"",
            stderr=stderr or b"",
            timed_out=state["timed_out"],
            cancelled=state["cancelled"],
        )


def _terminate(proc) -> None:
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 - 尽力回收，失败由 communicate/wait 兜底
        pass


# --------------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class CliSourceConfig:
    node_path: str
    cli_path: str
    profile: str
    browser: str
    timeout_seconds: int
    total_process_timeout_seconds: int
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    search_scrolls: int = DEFAULT_SEARCH_SCROLLS

    def __post_init__(self) -> None:
        for field_name in ("node_path", "cli_path", "profile", "browser"):
            if not getattr(self, field_name):
                raise AccessNotStarted(f"sources.xhs_search.{field_name} 未配置，适配器拒绝发起任何外部访问")
        if self.total_process_timeout_seconds <= self.timeout_seconds:
            raise AccessNotStarted(
                "total_process_timeout_seconds 必须大于单操作 timeout_seconds（两者相互独立）"
            )
        if not 1 <= self.timeout_seconds <= 120:
            raise AccessNotStarted("xhs-search 单操作 timeout 必须在 1～120 秒内")
        if self.min_interval_seconds < 1.0:
            raise AccessNotStarted("min_interval_seconds 至少 1.0（保守限频，默认 2.5s）")
        # profile 命名与 CLI 一致：1-64 位字母数字下划线连字符；不猜测、不扫描登录目录
        if not (1 <= len(self.profile) <= 64) or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for ch in self.profile
        ):
            raise AccessNotStarted(f"非法 profile 名：{self.profile!r}")
        if self.browser not in ("msedge", "chrome", "chromium"):
            raise AccessNotStarted(f"非法 browser：{self.browser!r}")


# --------------------------------------------------------------------------- 落盘 sink


class AttemptSink:
    """把 attempt 目录布局、原子写、拒绝覆盖封装成一个可注入接口，供后包直接接线。

    P1-A 只负责把原始 stdout/stderr/工具 out/元数据可靠落盘并返回 RawArtifactRef；
    DB `raw_artifacts` 登记与 operation/attempt 归属由 P1-B 用 storage.artifacts 走同一 attempt 目录。
    """

    def __init__(self, paths: RuntimePaths, base_dir: Path) -> None:
        self.paths = paths
        self.base_dir = base_dir

    def ensure_fresh(self, known_names: tuple[str, ...]) -> None:
        if not self.base_dir.exists():
            return
        for name in known_names:
            if (self.base_dir / name).exists():
                raise CliAttemptConflict(
                    f"attempt 目录已存在历史产物 {name}：拒绝覆盖，重试请使用新的 attempt_no"
                )

    def land(self, filename: str, data: bytes, kind: str) -> RawArtifactRef:
        target = self.base_dir / filename
        digest = hashlib.sha256(data).hexdigest()
        if target.exists():
            existing = target.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise CliAttemptConflict(
                    f"{filename} 已存在不同内容：拒绝覆盖，避免历史引用不可复现"
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".tmp-{filename}.part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return RawArtifactRef(
            path=self.paths.relative(target), sha256=digest, size_bytes=len(data), kind=kind
        )


# --------------------------------------------------------------------------- 解析结果


@dataclass
class _Interpretation:
    state: ResultState
    top_error: ErrorInfo | None
    per_word: list[PerWordOutcome]
    ok_entries: list[dict]
    warnings: list[str]


def _protocol_error(code: str, message: str, *, exit_code: int | None = None,
                   category: ErrorCategory = ErrorCategory.system,
                   detail: dict[str, str] | None = None) -> ErrorInfo:
    return ErrorInfo(
        code=code,
        message=message,
        category=category,
        retryable=False,
        human_action_required=True,
        exit_code=exit_code,
        detail=detail or {},
    )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _word_error(returncode: int, entry: dict) -> ErrorInfo:
    err = entry.get("error")
    message = str((err or {}).get("message") or f"逐词失败（exit {returncode}）")
    code = (err or {}).get("code")
    if not code:
        # 失败但没有 error.code：是"没采到/不确定"，不能当无需求
        return _protocol_error(
            "WORD_FAILED_UNSPECIFIED",
            f"{message}；该词失败但未给出 error.code，按不确定失败处理",
            exit_code=returncode,
        )
    info = classify_cli_failure(returncode, message, error_code=str(code))
    if info.code == "UNKNOWN_CLASSIFICATION":
        detail = dict(info.detail)
        detail["raw_error_code"] = str(code)
        info = info.model_copy(update={"detail": detail})
    return info


def _decode_envelope(stdout: bytes) -> tuple[dict | None, ErrorInfo | None]:
    if not stdout.strip():
        return None, _protocol_error("EMPTY_STDOUT", "stdout 为空：无法判定结果，不伪装 completed")
    text = stdout.decode("utf-8", errors="replace").strip()
    decoder = json.JSONDecoder()
    try:
        obj, end = decoder.raw_decode(text)
    except ValueError as exc:
        return None, _protocol_error("MALFORMED_JSON", f"stdout 不是合法 JSON：{exc}")
    trailing = text[end:].strip()
    if trailing:
        return None, _protocol_error(
            "EXTRA_STDOUT", "stdout 在单个 JSON 对象后还有多余文本：协议被破坏，不静默丢弃",
        )
    if not isinstance(obj, dict):
        return None, _protocol_error("ENVELOPE_NOT_OBJECT", "stdout 顶层 JSON 必须是对象")
    return obj, None


def _validate_envelope(envelope: dict, *, returncode: int, command: str) -> ErrorInfo | None:
    schema = envelope.get("schemaVersion")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        return _protocol_error(
            "SCHEMA_VERSION_MISMATCH",
            f"不支持的 schemaVersion：{schema!r}（本适配器支持 {sorted(SUPPORTED_SCHEMA_VERSIONS)}）",
            exit_code=returncode,
        )
    if not isinstance(envelope.get("cliVersion"), str) or not envelope["cliVersion"].strip():
        return _protocol_error("MISSING_CLI_VERSION", "envelope 缺少 cliVersion", exit_code=returncode)
    if envelope.get("command") != command:
        return _protocol_error(
            "COMMAND_MISMATCH",
            f"envelope.command={envelope.get('command')!r} 与调用命令 {command!r} 不一致",
            exit_code=returncode,
        )
    declared_exit = envelope.get("exitCode")
    if type(declared_exit) is not int or declared_exit != returncode:
        return _protocol_error(
            "EXIT_CODE_MISMATCH",
            f"envelope.exitCode={declared_exit!r} 与真实进程退出码 {returncode} 矛盾",
            exit_code=returncode,
        )
    ok = envelope.get("ok")
    if type(ok) is not bool or ok != (returncode == 0):
        return _protocol_error(
            "OK_FLAG_CONTRADICTION", "ok 与真实退出码矛盾：成功仅对应退出码 0", exit_code=returncode,
        )
    return None


_ITEMS_FIELD = {"suggest": "suggestions", "search": "notes"}
_AGGREGATE_EXITS = frozenset({0, 1, 5, 7})


def _result_rows(envelope: dict, *, returncode: int) -> tuple[list[dict] | None, ErrorInfo | None]:
    """严格结构核验（正常与 timeout/cancel rescue 共用）：data 必须对象、results 必须列表、每项必须对象。

    缺失 / null / 错类型一律显式失败，绝不静默过滤成确认空。
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None, _protocol_error(
            "DATA_NOT_OBJECT", "envelope.data 缺失或不是对象：不能当作确认无结果", exit_code=returncode)
    results = data.get("results")
    if results is None:
        return None, _protocol_error(
            "RESULTS_MISSING", "data.results 缺失：协议未给出逐词结果，绝不能当作确认无建议", exit_code=returncode)
    if not isinstance(results, list):
        return None, _protocol_error(
            "RESULTS_NOT_LIST", "data.results 不是列表：协议被破坏，不做字符串逐字符等静默降级",
            exit_code=returncode)
    for row in results:
        if not isinstance(row, dict):
            return None, _protocol_error(
                "RESULT_ROW_INVALID", "data.results 含非对象条目：无法核验逐词结果", exit_code=returncode)
    return results, None


def _match_single_word(rows: list[dict], expected_seed: str, *, returncode: int) -> tuple[dict | None, ErrorInfo | None]:
    """单词调用必须恰好一条 seed 等于本词；缺 seed / 错词 / 多词 / 重复词都显式失败，不静默过滤。"""
    if not rows:
        return None, _protocol_error(
            "WORD_RESULT_MISSING", f"单词调用未返回本词 {expected_seed!r} 的任何逐词结果：不能当作确认无建议",
            exit_code=returncode)
    if any(not isinstance(r.get("seed"), str) for r in rows):
        return None, _protocol_error(
            "SEED_INVALID", "逐词结果缺少合法字符串 seed", exit_code=returncode)
    if any(r["seed"] != expected_seed for r in rows):
        return None, _protocol_error(
            "SEED_MISMATCH",
            f"CLI 返回的种子词与请求 {expected_seed!r} 不一致：错词/多词响应不得当作结果",
            exit_code=returncode)
    if len(rows) != 1:
        return None, _protocol_error(
            "DUPLICATE_WORD_RESULT", f"单词调用收到 {len(rows)} 条同词结果：重复词不得静默过滤",
            exit_code=returncode)
    return rows[0], None


def _validate_row_fields(row: dict, command: str, *, returncode: int) -> ErrorInfo | None:
    """核验本条逐词的必需字段：status 合法、warnings/error 类型合法、本操作 items 字段真实合法（列表，
    suggest 逐条字符串 / search 逐条对象）。合法 completed 显式 items=[] 仍被接受为确认空。"""
    status = row.get("status")
    if not isinstance(status, str) or status not in _WORD_STATE:
        return _protocol_error("WORD_STATUS_INVALID", f"逐词 status 缺失或非法：{status!r}", exit_code=returncode)
    warnings = row.get("warnings")
    if warnings is not None and not isinstance(warnings, list):
        return _protocol_error("WARNINGS_NOT_LIST", "逐词 warnings 不是列表", exit_code=returncode)
    err = row.get("error")
    if err is not None and not isinstance(err, dict):
        return _protocol_error("ENTRY_ERROR_INVALID", "逐词 error 必须为 null 或对象", exit_code=returncode)
    if status == "completed" and err is not None:
        return _protocol_error("WORD_STATUS_ERROR_CONTRADICTION", "completed 条目同时带 error，拒绝发布数据", exit_code=returncode)
    items_field = _ITEMS_FIELD.get(command)
    if items_field is not None:
        present = items_field in row
        value = row.get(items_field)
        if status in ("completed", "partial"):
            if not present:
                return _protocol_error(
                    "RESULT_ITEMS_MISSING", f"{command} 的 {status} 结果缺少 {items_field} 字段：无法区分空与未采到",
                    exit_code=returncode)
            if not isinstance(value, list):
                return _protocol_error(
                    "RESULT_ITEMS_NOT_LIST",
                    f"{command} 的 {items_field} 不是列表：不得逐字符当作建议或静默丢弃", exit_code=returncode)
            if command == "suggest" and any(not isinstance(s, str) for s in value):
                return _protocol_error("SUGGESTION_ITEM_TYPE", "suggestions 含非字符串条目", exit_code=returncode)
            if command == "search" and any(not isinstance(n, dict) for n in value):
                return _protocol_error("NOTE_ITEM_TYPE", "notes 含非对象条目", exit_code=returncode)
        elif present and value is not None and not isinstance(value, list):
            return _protocol_error("RESULT_ITEMS_NOT_LIST", f"{items_field} 不是列表", exit_code=returncode)
    return None


def _entry_pause_error(entry: dict, *, returncode: int) -> ErrorInfo | None:
    """逐词 AUTH_REQUIRED/ACTION_REQUIRED/LOGIN_TIMEOUT 等信号优先，保留 human_action_required，
    不因 completed / 顶层 0 / timeout / cancel 或其它合法结果被掩盖。"""
    err = entry.get("error")
    if isinstance(err, dict) and err.get("code"):
        info = classify_cli_failure(
            returncode, str(err.get("message") or "逐词认证/验证要求"), error_code=str(err["code"]))
        if info.category in _PAUSE_CATEGORIES:
            return info.model_copy(update={"human_action_required": True, "exit_code": returncode})
    return None


def _find_pause_signal(envelope: dict | None, *, returncode: int) -> ErrorInfo | None:
    """顶层与逐词任一 auth/action 信号都优先显式暂停（不继续访问），无论退出码/超时/取消。"""
    if not isinstance(envelope, dict):
        return None
    top = envelope.get("error")
    if isinstance(top, dict) and top.get("code"):
        info = classify_cli_failure(
            returncode, str(top.get("message") or "进程级认证/验证要求"), error_code=str(top["code"]))
        if info.category in _PAUSE_CATEGORIES:
            return info.model_copy(update={"human_action_required": True, "exit_code": returncode})
    data = envelope.get("data")
    rows = data.get("results") if isinstance(data, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                sig = _entry_pause_error(row, returncode=returncode)
                if sig is not None:
                    return sig
    return None


def _interpret(
    *, result: CommandResult, envelope: dict | None, protocol_error: ErrorInfo | None,
    command: str, seed_raw: str,
) -> _Interpretation:
    # A pause signal applies to every exit/status combination, including partial
    # output and contradictory success flags. Keep its category for the worker.
    pause = _find_pause_signal(envelope, returncode=result.returncode)
    if pause is not None:
        return _Interpretation(ResultState.failed, pause, [], [], [])
    # 取消/超时：认证/验证信号优先显式暂停（不被 timeout/cancel 掩盖），否则只回收严格核验的本词 partial
    if result.cancelled or result.timed_out:
        pause = _find_pause_signal(envelope, returncode=result.returncode)
        if pause is not None:
            return _Interpretation(ResultState.failed, pause, [], [], [])
        if result.cancelled:
            cancelled = _protocol_error(
                "CLI_CANCELLED", "收到取消信号，本次子进程已回收", exit_code=130,
                category=ErrorCategory.cancelled,
            ).model_copy(update={"human_action_required": False})
            return _rescue_partial(result, envelope, command, seed_raw, cancelled)
        timeout = ErrorInfo(
            code="CLI_PROCESS_TIMEOUT",
            message="超过 total_process_timeout_seconds，本次子进程已回收（不确定是否已访问平台）",
            category=ErrorCategory.network,
            retryable=True,
            human_action_required=False,
            exit_code=result.returncode,
        )
        return _rescue_partial(result, envelope, command, seed_raw, timeout)

    if protocol_error is not None:
        return _Interpretation(ResultState.failed, protocol_error, [], [], [])

    mismatch = _validate_envelope(envelope or {}, returncode=result.returncode, command=command)
    if mismatch is not None:
        return _Interpretation(ResultState.failed, mismatch, [], [], [])

    if result.returncode not in CLI_EXIT_TABLE_HINT:
        return _Interpretation(
            ResultState.failed,
            _protocol_error(
                "UNKNOWN_EXIT_CODE", f"未登记的退出码 {result.returncode}", exit_code=result.returncode,
            ),
            [], [], [],
        )

    # 进程级认证/验证优先暂停：即便已有合法条目也不伪装 completed/partial（原始 stdout 已落盘可查，不发布 items）
    if result.returncode in (3, 4):
        hard = classify_cli_failure(result.returncode, "需要登录或人工验证", error_code=_top_error_code(envelope))
        rows, _ = _result_rows(envelope or {}, returncode=result.returncode)
        return _Interpretation(ResultState.failed, hard, [], [], _collect_warnings(rows or []))

    if result.returncode == 6:
        busy = classify_cli_failure(6, "同一 profile 正被其他命令使用", error_code="PROFILE_BUSY")
        return _Interpretation(ResultState.failed, busy, [], [], [])

    if result.returncode == 2:
        usage = classify_cli_failure(2, "参数或输入格式错误", error_code=_top_error_code(envelope) or "INVALID_ARGUMENT")
        return _Interpretation(ResultState.failed, usage, [], [], [])

    # 退出码 0 / 1 / 5 / 7：严格结构/身份核验下聚合
    return _aggregate(result.returncode, envelope or {}, seed_raw, command)


def _top_error_code(envelope: dict | None) -> str | None:
    if not envelope:
        return None
    err = envelope.get("error")
    return str(err["code"]) if isinstance(err, dict) and err.get("code") else None


def _collect_warnings(entries: list[dict]) -> list[str]:
    warnings: list[str] = []
    for entry in entries:
        for item in entry.get("warnings", []) or []:
            warnings.append(str(item))
    return warnings


def _rescue_partial(
    result: CommandResult, envelope: dict | None, command: str, seed_raw: str, error: ErrorInfo,
) -> _Interpretation:
    """超时/取消：只恢复经版本/command/seed/类型完整核验的本词合法 partial；
    错词/其它来源 items 绝不污染本 run，核验不过则记 failed + 原始超时/取消错误。"""
    if envelope is not None:
        declared = _declared_exit(envelope, result)
        if declared == 0 and envelope.get("error") is not None:
            return _Interpretation(ResultState.failed, error, [], [], [])
        if _validate_envelope(envelope, returncode=declared, command=command) is None:
            rows, serr = _result_rows(envelope, returncode=declared)
            if rows is not None:
                row, merr = _match_single_word(rows, seed_raw, returncode=declared)
                if row is not None and _validate_row_fields(row, command, returncode=declared) is None:
                    if _entry_pause_error(row, returncode=declared) is None and _row_publishable(row, command):
                        outcome = _entry_outcome(result.returncode, row, seed_raw)
                        return _Interpretation(
                            ResultState.partial, error, [outcome], [row],
                            _collect_warnings([row]) + ["超时/取消，保留经过严格核验的本词部分结果，不自动重发"],
                        )
    return _Interpretation(ResultState.failed, error, [], [], [])


def _declared_exit(envelope: dict, result: CommandResult) -> int:
    declared = envelope.get("exitCode")
    return declared if isinstance(declared, int) else result.returncode


def _row_publishable(entry: dict, command: str) -> bool:
    """本操作 items 为非空且类型合法（经 _validate_row_fields 保证为列表），且 status 可发布。"""
    if entry.get("status") not in ("completed", "partial"):
        return False
    items_field = _ITEMS_FIELD.get(command)
    value = entry.get(items_field) if items_field else None
    return isinstance(value, list) and len(value) > 0


def _entry_outcome(returncode: int, entry: dict, seed_raw: str) -> PerWordOutcome:
    raw = str(entry.get("seed") or seed_raw)
    keyword = KeywordText(raw=raw, normalized=normalize_text(raw))
    status = str(entry.get("status", "failed"))
    state = _WORD_STATE.get(status, ResultState.failed)
    warnings = [str(w) for w in (entry.get("warnings") or [])]
    if state is ResultState.failed:
        return PerWordOutcome(keyword=keyword, state=state, error=_word_error(returncode, entry),
                              warnings=warnings or ["该词采集失败（保留原始响应于落盘产物）"])
    if state is ResultState.completed:
        # completed 却带 error 是协议矛盾：操作级已在 _aggregate 优先暂停（保留 human 信号），
        # 逐词只如实标状态不伪造 error（per-word completed 不能同时携带 error 的模型不变量）。
        return PerWordOutcome(keyword=keyword, state=state, error=None, warnings=warnings)
    if state is ResultState.skipped:
        return PerWordOutcome(keyword=keyword, state=state, error=None,
                              warnings=warnings or ["该词被跳过"])
    # partial
    err = _word_error(returncode, entry) if entry.get("error") else None
    return PerWordOutcome(keyword=keyword, state=state, error=err,
                          warnings=warnings or ["该词部分成功，检查逐词状态"])


def _aggregate(
    returncode: int, envelope: dict, expected_seed: str, command: str,
) -> _Interpretation:
    rows, serr = _result_rows(envelope, returncode=returncode)
    if serr is not None:
        return _Interpretation(ResultState.failed, serr, [], [], [])

    # 顶层错误 / ok / exit 矛盾（退出 0 但顶层 error 非空）：显式报告，非 AUTH 矛盾也不能 completed
    top = envelope.get("error")
    if returncode == 0 and isinstance(top, dict) and top.get("code"):
        info = classify_cli_failure(returncode, "退出码 0 但顶层 error 非空：协议矛盾", error_code=str(top["code"]))
        info = info.model_copy(update={"human_action_required": True})
        return _Interpretation(ResultState.failed, info, [], [], _collect_warnings(rows))

    row, merr = _match_single_word(rows, expected_seed, returncode=returncode)
    if merr is not None:
        return _Interpretation(ResultState.failed, merr, [], [], _collect_warnings(rows))

    ferr = _validate_row_fields(row, command, returncode=returncode)
    if ferr is not None:
        return _Interpretation(ResultState.failed, ferr, [], [], _collect_warnings([row]))

    # 逐词 auth/action 信号优先：无论 completed / 顶层 0，都显式暂停，不发布 items
    pause = _entry_pause_error(row, returncode=returncode)
    if pause is not None:
        outcome = _entry_outcome(returncode, row, expected_seed)
        return _Interpretation(ResultState.failed, pause, [outcome], [], _collect_warnings([row]))

    outcome = _entry_outcome(returncode, row, expected_seed)
    per_word = [outcome]
    warnings = _collect_warnings([row])
    if outcome.state is ResultState.skipped:
        return _Interpretation(ResultState.skipped, None, per_word, [], warnings or outcome.warnings)
    publishable = _row_publishable(row, command)
    ok_entries = [row] if publishable else []

    if returncode in _AGGREGATE_EXITS:
        if returncode == 0:
            if outcome.state is ResultState.failed:
                return _Interpretation(ResultState.failed, outcome.error, per_word, [], warnings)
            if outcome.state is ResultState.partial:
                return _Interpretation(ResultState.partial, outcome.error, per_word, ok_entries, warnings)
            # completed / skipped：合法确认（含显式 items=[] 确认空）
            return _Interpretation(ResultState.completed, None, per_word, ok_entries, warnings)

        # 退出码 1 / 5 / 7
        if outcome.state is ResultState.failed:
            return _Interpretation(ResultState.failed, outcome.error, per_word, [], warnings)
        if publishable:
            top_err = outcome.error or classify_cli_failure(
                returncode, "部分结果，检查逐词状态", error_code=_top_error_code(envelope) or "PARTIAL_RESULT")
            return _Interpretation(ResultState.partial, top_err, per_word, ok_entries, warnings)
        top_err = outcome.error or classify_cli_failure(
            returncode, "不确定空结果，未采到不能当无需求", error_code=_top_error_code(envelope) or "RESULTS_UNAVAILABLE")
        return _Interpretation(ResultState.failed, top_err, per_word, [], warnings)

    return _Interpretation(
        ResultState.failed,
        _protocol_error(
            "EXIT_STATE_CONTRADICTION",
            f"退出码 {returncode} 与逐词聚合不相容：不伪装 completed", exit_code=returncode),
        per_word, [], warnings,
    )


# --------------------------------------------------------------------------- 适配器


class CliSearchSource(SearchSource):
    """title_only 真实 CLI 数据源：suggest / search 单词调用，synthetic=False。"""

    name = SOURCE_NAME
    version = CODE_VERSION
    synthetic = False

    def __init__(
        self,
        *,
        config: CliSourceConfig,
        paths: RuntimePaths,
        out_root: Path,
        runner: CommandRunner | None = None,
        clock: Clock | None = None,
        lock_dir: Path | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.out_root = Path(out_root)
        self.runner = runner or SubprocessRunner()
        self.clock = clock or RealClock()
        # 间隔状态按 profile 共享（跨实例/进程不可绕开），存于 lock_dir；无 runner 注入时也用真实时钟
        self._interval_dir = Path(lock_dir) if lock_dir is not None else (self.out_root / ".locks")
        self._sleeper = sleeper
        self._bound: OperationContext | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ SearchSource 契约

    def capabilities(self) -> Capabilities:
        return Capabilities.title_only()

    def bind(self, context: OperationContext) -> OperationContext:
        self._bound = context
        return context

    def _baseline_session_version(self) -> str:
        """new_session 的基线来源版本（结果信封会被 _refine_session 精炼，见 session_matches_source）。"""
        return f"{EXPECTED_CLI_NAME}@schema<=max({sorted(SUPPORTED_SCHEMA_VERSIONS)})"

    def session_matches_source(self, session: SessionRef, raw_responses=()) -> bool:
        """恢复核验：来源版本由**同一 attempt 登记的原始响应字节**派生，绝不按格式放行。

        落盘信封自述的 `xhs-search@<版本>/schema:<版本>` 可以是任何格式合法的文本；唯一可信的
        基准是本 attempt 登记的 `cli_stdout.json`（引擎已按登记 sha/size 逐字节核对）里工具
        **实际自报**的 cliVersion/schemaVersion，用与采集时完全同一式的 `_session_version_from_envelope`
        重新派生，逐字相等才认。没有登记任何原始响应（如 profile_busy 根本没发出请求）时，
        唯一合法形态是基线串。快照已核对工具字节与包身份，但真实安装版本只存在于响应证据里；
        恢复不重跑 CLI，也不从被核验的规范化产物反推基准。
        """
        if session.source_name != self.name:
            return False
        if not raw_responses:
            return session.source_version == self._baseline_session_version()
        expected = {
            _session_version_from_envelope(_decode_envelope(body)[0]) for body in raw_responses
        }
        return session.source_version in expected

    def new_session(self) -> SessionRef:
        return SessionRef(
            session_id=f"cli-{os.getpid()}-{self.clock.now():.6f}"[:64],
            collected_at=utc_now(),
            source_name=self.name,
            source_version=self._baseline_session_version(),
            synthetic=False,
        )

    def get_suggestions(self, context: OperationContext) -> SuggestionResult:
        self.bind(context)
        session = self.new_session()
        outcome = self._call("suggest", context, session)
        items = _suggestion_items(outcome.ok_entries)
        return SuggestionResult(
            state=outcome.state,
            context=context,
            session=outcome.session,
            items=items,
            per_word=outcome.per_word,
            error=outcome.top_error,
            warnings=outcome.warnings,
            raw_artifacts=outcome.artifacts,
        )

    def _search_posts(
        self, context: OperationContext, *, limit: int, session: SessionRef
    ) -> PostSearchResult:
        outcome = self._call("search", context, session, limit=limit)
        items = _post_cards(outcome.ok_entries, context.keyword)
        return PostSearchResult(
            state=outcome.state,
            context=context,
            session=outcome.session,
            items=items,
            per_word=outcome.per_word,
            error=outcome.top_error,
            warnings=outcome.warnings,
            raw_artifacts=outcome.artifacts,
        )

    # ------------------------------------------------------------ 调用编排

    def _attempt_sink(self, context: OperationContext) -> AttemptSink:
        run_dir = self.paths.run_dir(context.run_id)
        keyword_segment = (
            f"k{context.keyword_id}"
            if context.keyword_id is not None
            else "k" + hashlib.sha256(str(context.keyword_identity).encode("utf-8")).hexdigest()[:12]
        )
        base = (
            run_dir
            / "operations"
            / keyword_segment
            / context.operation_type.value
            / f"r{context.revisit_seq}"
            / f"a{context.attempt_no}"
            / "cli"
        )
        return AttemptSink(self.paths, base)

    def _build_argv(self, command: str, context: OperationContext, out_dir: Path,
                    limit: int | None) -> list[str]:
        argv = [
            self.config.node_path,
            self.config.cli_path,
            command,
            "--seed",
            context.keyword.raw,
            "--profile",
            self.config.profile,
            "--browser",
            self.config.browser,
            "--timeout",
            str(self.config.timeout_seconds * 1000),
            "--out",
            str(out_dir),
        ]
        if command == "search":
            argv += ["--limit", str(limit), "--scrolls", str(self.config.search_scrolls)]
        return argv

    def _call(self, command: str, context: OperationContext, session: SessionRef,
              *, limit: int | None = None) -> "_Outcome":
        sink = self._attempt_sink(context)
        sink.ensure_fresh(("cli_stdout.json", "cli_meta.json"))
        guard = OsGuard(self._lock_path())
        if not guard.try_acquire():
            # profile_busy：明确可重试状态，绝不起第二个浏览器，不发子进程
            busy = classify_cli_failure(6, "同 profile 已被本适配器串行锁占用", error_code="PROFILE_BUSY")
            busy = busy.model_copy(update={"detail": {**busy.detail, "external_access": "not_started"}})
            return _Outcome(ResultState.failed, busy, [], [], session, [sink.land("cli_meta.json", _meta_bytes(context, command, busy, [], None, 0, False, False, "profile_busy"), "raw_response")])
        try:
            self._throttle()
            result = self.runner.run(
                self._build_argv(command, context, sink.base_dir, limit),
                out_dir=sink.base_dir,
                timeout_seconds=float(self.config.total_process_timeout_seconds),
                cancel=None,
            )
            artifacts = self._land_all(sink, result, context, command)
            envelope, protocol_error = _decode_envelope(result.stdout)
            interpretation = _interpret(
                result=result, envelope=envelope, protocol_error=protocol_error,
                command=command, seed_raw=context.keyword.raw,
            )
            updated_session = _refine_session(session, envelope, interpretation)
            self._stamp_interval()
            return _Outcome(
                interpretation.state, interpretation.top_error, interpretation.per_word,
                interpretation.ok_entries, updated_session, artifacts, interpretation.warnings,
            )
        finally:
            guard.release()

    def _lock_path(self) -> Path:
        self._interval_dir.mkdir(parents=True, exist_ok=True)
        return self._interval_dir / f"xhs-cli-profile-{self.config.profile}.guard"

    def _interval_file(self) -> Path:
        return self._interval_dir / f"xhs-cli-profile-{self.config.profile}.interval"

    def _throttle(self) -> None:
        last = _read_float(self._interval_file())
        if last is None:
            return
        elapsed = self.clock.now() - last
        # Persisted monotonic counters can move backwards after a reboot. Wait
        # one full interval, rather than treating the old uptime as future time.
        if not math.isfinite(elapsed) or elapsed < 0:
            elapsed = 0.0
        remaining = self.config.min_interval_seconds - elapsed
        if remaining > 0:
            (self._sleeper or self.clock.sleep)(remaining)

    def _stamp_interval(self) -> None:
        _write_float(self._interval_file(), self.clock.now())

    def _land_all(self, sink: AttemptSink, result: CommandResult, context: OperationContext,
                  command: str) -> list[RawArtifactRef]:
        refs: list[RawArtifactRef] = []
        refs.append(sink.land("cli_stdout.json", result.stdout, "stdout_json"))
        refs.append(sink.land("cli_stderr.log", result.stderr, "stderr_log"))
        for name in ("response.json", "results.json", "notes.csv", "suggestions.csv"):
            candidate = sink.base_dir / name
            if candidate.is_file():
                data = candidate.read_bytes()
                refs.append(sink.land(f"cli_{name}", data, "raw_response"))
        meta = _meta_bytes(context, command, None, [], result, result.returncode,
                           result.timed_out, result.cancelled, "attempt_meta")
        refs.append(sink.land("cli_meta.json", meta, "raw_response"))
        return refs


@dataclass
class _Outcome:
    state: ResultState
    top_error: ErrorInfo | None
    per_word: list[PerWordOutcome]
    ok_entries: list[dict]
    session: SessionRef
    artifacts: list[RawArtifactRef]
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


def _suggestion_items(entries: list[dict]) -> list[SuggestionItem]:
    items: list[SuggestionItem] = []
    rank = 1
    for entry in entries:
        for text in entry.get("suggestions") or []:
            if str(text):
                items.append(SuggestionItem(text=str(text), rank=rank))
                rank += 1
    return items


def _post_cards(entries: list[dict], keyword: KeywordText) -> list[PostCard]:
    cards: list[PostCard] = []
    seen: set[str] = set()
    rank = 1
    for entry in entries:
        for note in entry.get("notes") or []:
            note_id = str(note.get("id") or "")
            url = str(note.get("url") or "")
            if not note_id or not url.startswith(("http://", "https://")):
                # 缺 id 或非法 URL：不补造、不静默丢弃，交由上层协议核验（见测试）
                raise ValueError("搜索卡片缺少 id 或合法 url，不能伪造或丢弃")
            if note_id in seen:
                continue
            seen.add(note_id)
            title = note.get("title")
            published = note.get("publishedText")
            author_url = note.get("authorUrl")
            likes = note.get("likesText")
            cards.append(
                PostCard(
                    note_id=note_id,
                    access_url=url,
                    title=title,
                    title_missing=title is None,
                    author=AuthorRef(
                        kind="public_ref" if author_url else "unknown",
                        public_ref=author_url or None,
                    ),
                    published_at_text=published,
                    published_at_missing=published is None,
                    likes_text=None if likes is None else str(likes),
                    result_rank=rank,
                    search_keyword=keyword,
                )
            )
            rank += 1
    return cards


def _meta_bytes(context: OperationContext, command: str, error: ErrorInfo | None,
                per_word: list[PerWordOutcome], result: CommandResult | None, returncode: int,
                timed_out: bool, cancelled: bool, note: str) -> bytes:
    payload = {
        "note": note,
        "run_id": context.run_id,
        "keyword_identity": str(context.keyword_identity),
        "operation_type": context.operation_type.value,
        "revisit_seq": context.revisit_seq,
        "attempt_no": context.attempt_no,
        "command": command,
        "cli_name": EXPECTED_CLI_NAME,
        "seed": context.keyword.raw,
        "returncode": returncode,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "error_code": error.code if error else None,
        "error_category": error.category.value if error else None,
        "human_action_required": bool(error and error.human_action_required),
        "per_word": [
            {"keyword": o.keyword.raw, "state": o.state.value,
             "error": o.error.code if o.error else None}
            for o in per_word
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _session_version_from_envelope(envelope: dict | None) -> str:
    """会话来源版本的唯一出处：采集精炼与恢复核验共用，绝不出现第二套拼接。"""
    cli_version = str((envelope or {}).get("cliVersion") or "unknown")
    schema = str((envelope or {}).get("schemaVersion") or "unknown")
    return f"{EXPECTED_CLI_NAME}@{cli_version}/schema:{schema}"


def _refine_session(session: SessionRef, envelope: dict | None,
                    interpretation: _Interpretation) -> SessionRef:
    collected = _parse_iso((envelope or {}).get("startedAt")) or utc_now()
    entry_time = next((_parse_iso(e.get("collectedAt")) for e in interpretation.ok_entries
                       if _parse_iso(e.get("collectedAt"))), None)
    return session.model_copy(
        update={
            "collected_at": entry_time or collected,
            "source_version": _session_version_from_envelope(envelope),
        }
    )


def _read_float(path: Path) -> float | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _write_float(path: Path, value: float) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(repr(value), encoding="utf-8")
    os.replace(tmp, path)


def from_settings(paths: RuntimePaths, settings, *, runner: CommandRunner | None = None,
                  clock: Clock | None = None, out_root: Path | None = None,
                  lock_dir: Path | None = None, min_interval_seconds: float | None = None,
                  search_scrolls: int | None = None) -> CliSearchSource:
    """从 Settings 构造适配器：node/cli/profile/browser/timeout/间隔/滚动/锁目录全部来自配置。

    显式入参（`lock_dir`/`min_interval_seconds`/`search_scrolls`）只用于测试注入；缺省一律取
    `sources.xhs_search` 的配置值——配置里写什么就跑什么，不在代码里留第二套口径。
    """
    section = settings.sources.xhs_search
    config = CliSourceConfig(
        node_path=section.node_path,
        cli_path=section.cli_path,
        profile=section.profile,
        browser=section.browser,
        timeout_seconds=section.timeout_seconds,
        total_process_timeout_seconds=section.total_process_timeout_seconds,
        min_interval_seconds=(min_interval_seconds if min_interval_seconds is not None
                              else section.min_interval_seconds),
        search_scrolls=(search_scrolls if search_scrolls is not None else section.search_scrolls),
    )
    return CliSearchSource(
        config=config,
        paths=paths,
        out_root=out_root if out_root is not None else paths.root,
        runner=runner,
        clock=clock,
        lock_dir=lock_dir if lock_dir is not None else (
            Path(section.lock_dir) if section.lock_dir else None),
    )


__all__ = [
    "CliAttemptConflict",
    "CliSearchSource",
    "CliSourceConfig",
    "CommandResult",
    "CommandRunner",
    "Clock",
    "RealClock",
    "SubprocessRunner",
    "AttemptSink",
    "SOURCE_NAME",
    "SUPPORTED_SCHEMA_VERSIONS",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "from_settings",
]
