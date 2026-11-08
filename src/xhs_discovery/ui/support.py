"""P0-E-UI 支撑层：把 Streamlit 页面与已验收应用服务之间的纯逻辑集中在这里。

设计约束（P0-E-UI-r1 任务）：

- **不复制算法**：装配、预算读数、批次挑选与完整性自检全部调用 `xhs_discovery.app` 的已验收
  函数；本模块只做取数整形、CSV/GraphML 解析与 pydeck 布局计算，是页面的"视图模型"。
- **只读固定批次**：双图/需求详情/排行榜一律从所选已完成批次的产物文件重建（经哈希+内嵌哈希
  校验），绝不把正在被后台 worker 重写的可变 `needs` 表当"上次已完成批次"展示。
- **连接显式关闭**：`open_store` 返回 (repo, paths, settings, conn)，调用方负责在页面 rerun
  结束时 `conn.close()`，避免 Streamlit 每次重跑泄漏一个 SQLite 连接。
- **无证据不入榜、不同重归一化权重不混排**：以 needs.csv 的 `effective_weight_total` 分层，
  小样本保留 provisional 与原始计数。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

import yaml

from xhs_discovery import app as services
from xhs_discovery.config import Settings, apply_overrides, load_settings
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import WorkerControl

# --------------------------------------------------------------------- 连接与关闭


def open_store_conn(config_path: Path | str, cwd: Path | str) -> tuple[Repository, RuntimePaths, Settings, object]:
    """打开配置声明的数据根并建库；返回的 conn 由调用方在 rerun 结束时显式关闭。"""
    settings = load_settings(Path(config_path))
    paths = services.runtime_paths(settings, cwd)
    conn = storage_db.connect(paths.db_path)
    return Repository(conn), paths, settings, conn


# --------------------------------------------------------------------- 创建页：派生配置


def derive_settings(
    base: Settings,
    *,
    mode: str,
    seed: str,
    budget: dict[str, int],
    sampling: dict[str, float],
    llm_enabled: bool,
    embedding_enabled: bool,
    xhs_search: dict | None = None,
) -> Settings:
    """把创建页的表单值合成一份完整重校验后的 Settings；不合法一律抛 ConfigError（带字段路径）。

    live 不是"缺能力"，而是"配置必须完备"：`apply_overrides` 的完整重校验会因
    `sources.xhs_search` 缺项/相对路径/锁目录在数据根内而抛 ConfigError，页面据此明确报错。
    模型开关仍未接入，按 70 类拒绝。
    """
    if llm_enabled or embedding_enabled:
        raise services.UnsupportedCapabilityError(
            "本项目不接模型：models.llm_enabled/embedding_enabled 必须为 false"
            "（不下载权重、不调用外部 API；规则基线可独立运行）"
        )
    overrides: dict = {"run": {"mode": mode}, "budget": dict(budget), "sampling": dict(sampling)}
    if seed:
        overrides["run"]["seed_raw"] = seed
    if xhs_search:
        overrides["sources"] = {"xhs_search": dict(xhs_search)}
    settings = apply_overrides(base, overrides)
    services.reject_unsupported(settings, mode)
    return settings


def live_readiness(settings: Settings, *, xhs_search: dict | None = None) -> dict:
    """创建页的 live 就绪说明：配置是否完备、能力边界在哪，全部在启动前摊开给用户看。

    `xhs_search` 是表单里那份来源配置：先把它叠到基础配置上再判定，页面才不必等到点"创建"
    才知道缺了哪一项；叠加后仍走同一套 `apply_overrides` 重校验，页面不另写判断口径。
    """
    if settings.run.mode != "live" and not xhs_search:
        return {"mode": "fixture", "ready": True, "notes": ["合成数据集离线运行，不访问平台"]}
    from xhs_discovery.config import ConfigError

    if xhs_search is not None:
        try:
            settings = apply_overrides(
                settings,
                {"run": {"mode": "live"}, "sources": {"xhs_search": dict(xhs_search)}},
            )
        except (ValueError, ConfigError) as exc:
            return {"mode": "live", "ready": False, "notes": [str(exc)]}
    if settings.run.mode != "live":
        return {"mode": "fixture", "ready": True, "notes": ["合成数据集离线运行，不访问平台"]}
    try:
        settings.sources.xhs_search.check_live_complete(settings.run.data_dir)
    except (ValueError, ConfigError) as exc:
        return {"mode": "live", "ready": False, "notes": [str(exc)]}
    return {
        "mode": "live",
        "ready": True,
        "notes": [
            "来源为本机已配置的 xhs-search CLI：只取搜索卡片标题（title_only）",
            "正文与作者公开笔记不支持：相应动作不执行、不消耗外部预算，也不会伪造正文",
            "点赞数 likesText 是卡片显示的点赞，不是搜索量；未知日期一律标 missing",
            "登录与 doctor 由使用者在平台侧手工完成：本程序不读凭据、不代做认证",
            "同 profile 互斥跨数据根共享（sources.xhs_search.lock_dir），并行 run 会排队或按 profile_busy 暂停",
        ],
    }


def write_derived_config(settings: Settings, paths: RuntimePaths, run_id: str) -> str:
    """把派生配置写入数据根下的 ui_configs/<run_id>.yaml，供独立 worker 进程读取。"""
    directory = paths.root / "ui_configs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run_id}.yaml"
    target.write_text(
        yaml.safe_dump(settings.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(target)


# --------------------------------------------------------------------- 看板：状态与读数


def worker_occupied(conn: object, paths: RuntimePaths, run_id: str) -> tuple[bool, str]:
    """真实进程互斥判定：只信 OS 锁+租约（WorkerControl），不把 session_state 当互斥。"""
    status = WorkerControl(conn, paths, run_id).status()
    return bool(status.occupied), status.detail


# --------------------------------------------------------------------- 后台启动（可审计）

# 进程内登记簿：同一条 Streamlit 服务进程跨 rerun 保留 Popen 句柄，可回报退出码；
# 服务重启后回退到磁盘启动记录 + OS 锁/pid 探查，不假装还知道子进程状态。
_LAUNCHES: dict[str, dict] = {}

LAUNCH_DIRNAME = "ui_launches"


def _launch_paths(paths: RuntimePaths, run_id: str) -> dict[str, Path]:
    directory = paths.root / LAUNCH_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "dir": directory,
        "record": directory / f"{run_id}.json",
        "stdout": directory / f"{run_id}.out.log",
        "stderr": directory / f"{run_id}.err.log",
    }


def launch_worker(command: list[str], paths: RuntimePaths, run_id: str, cwd: Path | str | None = None) -> dict:
    """无可见控制台的持久后台启动：记录启动请求/PID/日志落点，回报"进程已创建"。

    Popen 返回只证明进程创建成功，**不**证明 worker 已拿到 OS 锁开始运行——锁与运行状态
    一律以 `worker_occupied`（WorkerControl）与日志为准。失败（拒绝启动/子进程报错）会
    写进记录并可由页面读回。
    """
    import subprocess
    import sys
    from datetime import datetime, timezone

    targets = _launch_paths(paths, run_id)
    record: dict = {
        "run_id": run_id,
        "command": list(command),
        "cwd": str(cwd or Path.cwd()),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "stdout_log": str(targets["stdout"]),
        "stderr_log": str(targets["stderr"]),
        "pid": None,
        "create_error": None,
    }
    try:
        out_fh = open(targets["stdout"], "ab")
        err_fh = open(targets["stderr"], "ab")
        popen_kwargs: dict = {"cwd": record["cwd"], "stdout": out_fh, "stderr": err_fh}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(command, **popen_kwargs)  # noqa: S603
        record["pid"] = proc.pid
        _LAUNCHES[run_id] = {"proc": proc, "record_path": targets["record"]}
    except OSError as exc:
        record["create_error"] = f"{type(exc).__name__}: {exc}"
    record["launch_ok"] = record["pid"] is not None
    targets["record"].write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def valid_selection_refs(state: object, visible_refs: set[str]) -> list[str]:
    """点选结果只认当前图节点的合法 ref：未知图层/别的 run 或页面残留的 ref 一律丢弃。

    两个图页面各用各自的 `visible_refs` 过滤，选择事件不会跨页/跨 run 串详情。
    """
    return [ref for ref in extract_selection_ids(state) if ref in visible_refs]


# 停止/暂停类事件：引擎 `_finalize` 写 `stop_<status>`，状态机写 paused/stopped/failed。
# 停止说明必须单独按 kind 查最近一条：分析一次会产生成百条 `phrase_skipped` 之类事件，
# 只看"最近 N 条事件"会让停止记录被挤掉（r2 复核项 4）。
STOP_EVENT_KINDS = ("stop_paused", "stop_completed", "stop_failed", "paused", "stopped", "failed")


def latest_stop_event(conn: object, run_id: str) -> dict | None:
    placeholders = ",".join("?" for _ in STOP_EVENT_KINDS)
    row = conn.execute(
        f"SELECT at, kind, detail_json FROM run_events WHERE run_id = ? AND kind IN ({placeholders})"
        " ORDER BY id DESC LIMIT 1",
        (run_id, *STOP_EVENT_KINDS),
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": str(row["kind"]),
        "at": str(row["at"]),
        "detail": json.loads(str(row["detail_json"] or "{}")),
    }


def last_launch_process_state(paths: RuntimePaths, run_id: str) -> tuple[str, str]:
    """上一次为该 run 启动的进程现在到底还在不在。

    返回 `(state, detail)`，state ∈ live/dead/unknown/none：
    live 与 unknown 都不算"可以再起一个"（查不到状态绝不当作已退出）；
    dead/none 才允许下一次合法启动。本进程持有 Popen 句柄时以 poll 的精确退出码为准，
    否则（例如服务重启后）回落到磁盘记录的 pid 经 `probe_pid` 三态探查。
    """
    from xhs_discovery.storage.worker import LOCK_DEAD, LOCK_LIVE, probe_pid

    entry = _LAUNCHES.get(run_id)
    if entry is not None and entry["record_path"] != _launch_paths(paths, run_id)["record"]:
        entry = None
    if entry is not None:
        code = entry["proc"].poll()
        if code is None:
            return "live", f"本会话启动的 worker（pid={entry['proc'].pid}）仍在运行"
        return "dead", f"本会话启动的 worker 已退出（code={code}）"

    record = read_launch_record(paths, run_id)
    if record is None or record.get("create_error") or not record.get("pid"):
        return "none", "没有该 run 的启动记录（或上次启动未创建进程）"
    state = probe_pid(int(record["pid"]))
    detail = f"磁盘启动登记 pid={record['pid']} 的进程探查={state}"
    if state == LOCK_LIVE:
        return "live", detail
    if state == LOCK_DEAD:
        return "dead", detail
    return "unknown", detail


def plan_worker_launch(
    conn: object,
    paths: RuntimePaths,
    run_id: str,
    command: list[str],
    *,
    session_launched: set[str],
    cwd: Path | str | None = None,
) -> dict:
    """Serialize launch decisions, including the gap before the child takes its worker lock."""
    from xhs_discovery.storage.worker import OsGuard

    guard = OsGuard(paths.root / "ui-launch.guard")
    if not guard.try_acquire():
        return {"launched": False, "reason": "occupied", "detail": "另一个页面正在登记启动请求，请稍后重试。"}
    try:
        # A created child may not have reached WorkerControl.acquire yet.
        # Persisted records also protect separate UI server processes.
        for record_path in (paths.root / LAUNCH_DIRNAME).glob("*.json"):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                other_id = str(record["run_id"])
                services.validate_run_id(other_id)
            except (OSError, ValueError, KeyError, services.UsageError):
                return {"launched": False, "reason": "unknown_launch", "detail": f"无法确认启动登记 {record_path.name} 的状态，请先检查该记录。"}
            if other_id == run_id:
                continue
            state, detail = last_launch_process_state(paths, other_id)
            if state in ("live", "unknown"):
                return {"launched": False, "reason": "occupied", "detail": f"任务 {other_id} 的启动进程尚未确认退出：{detail}"}
        return _plan_worker_launch(conn, paths, run_id, command,
                                   session_launched=session_launched, cwd=cwd)
    finally:
        guard.release()


def _plan_worker_launch(
    conn: object,
    paths: RuntimePaths,
    run_id: str,
    command: list[str],
    *,
    session_launched: set[str],
    cwd: Path | str | None = None,
) -> dict:
    """页面启动决策（创建/恢复共用）：互斥以 OS 锁+租约（WorkerControl）为准，
    再起一层"上一次启动的进程还没确认退出就不许第二个"的判定。

    `session_launched` 只用来标注拒绝原因（这次会话确实发起过），**不是**永久按 run_id
    拦截：一个 run 正常退出后，同一会话里追加额度→恢复、再追加→再恢复 都必须还能发起。
    判重依据是本次请求对应的进程状态与现有 OS 锁，活跃或状态未知的进程一律拒绝。

    返回 {"launched": bool, "record"|"detail": ...}：占用/进程未确认退出时不启动并给出原因。
    """
    occupied, detail = worker_occupied(conn, paths, run_id)
    if occupied:
        return {"launched": False, "reason": "occupied", "detail": f"活跃 worker 未被排除，拒绝启动第二个：{detail}"}
    state, state_detail = last_launch_process_state(paths, run_id)
    if state in ("live", "unknown"):
        reason = "session" if run_id in session_launched else "previous_process"
        return {
            "launched": False,
            "reason": reason,
            "detail": f"上一次启动的 worker 尚未确认退出（{state_detail}），拒绝启动第二个。{detail}",
        }
    record = launch_worker(command, paths, run_id, cwd=cwd)
    if not record["launch_ok"]:
        return {"launched": False, "reason": "create_failed", "detail": record["create_error"], "record": record}
    session_launched.add(run_id)
    return {"launched": True, "record": record, "detail": f"进程已创建（pid={record['pid']}），持锁与否以 WorkerControl 判定为准"}


def read_launch_record(paths: RuntimePaths, run_id: str) -> dict | None:
    target = _launch_paths(paths, run_id)["record"]
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return None


def worker_launch_status(paths: RuntimePaths, run_id: str) -> dict:
    """启动请求的可读回报：created/running/exited(code)/failed，附 pid 与日志位置。

    进程内登记的 Popen 优先（能拿到精确退出码）；否则依据记录里的 pid 经 `probe_pid`
    三态判定（unknown 不当作已退出，也不当作在跑，如实说明）。
    """
    from xhs_discovery.storage.worker import probe_pid

    record = read_launch_record(paths, run_id)
    if record is None:
        return {"state": "none", "detail": f"run {run_id} 没有本页面发起的启动记录"}
    if record.get("create_error"):
        return {"state": "failed", "detail": f"进程创建失败：{record['create_error']}", "record": record}
    pid = record.get("pid")
    entry = _LAUNCHES.get(run_id)
    if entry is not None and entry["record_path"] != _launch_paths(paths, run_id)["record"]:
        entry = None
    if entry is not None:
        code = entry["proc"].poll()
        if code is None:
            return {"state": "running", "pid": pid, "detail": f"子进程 pid={pid} 仍在运行", "record": record}
        return {
            "state": "exited", "pid": pid, "exit_code": code, "record": record,
            "detail": f"子进程已退出（code={code}）；stderr 日志：{record.get('stderr_log')}",
        }
    state = probe_pid(int(pid)) if pid else "invalid"
    return {
        "state": "unknown", "pid": pid, "record": record,
        "detail": f"页面无法直接确认 pid={pid} 的退出码（启动登记不在本进程）；进程探查={state}，"
                  "锁持有与完成与否以运行看板 WorkerControl 判定和日志为准",
    }


def launch_log_excerpt(paths: RuntimePaths, run_id: str, limit: int = 4000) -> str:
    """读回子进程 stderr/stdout 尾部，供页面展示启动后的真实错误/退出信息。"""
    targets = _launch_paths(paths, run_id)
    chunks: list[str] = []
    for label, key in (("stderr", "stderr"), ("stdout", "stdout")):
        target = targets[key]
        if not target.is_file():
            continue
        data = target.read_bytes()[-limit:]
        text = data.decode("utf-8", errors="replace")
        if text.strip():
            chunks.append(f"[{label}]\n{text}")
    return "\n".join(chunks)


def read_dashboard(repo: Repository, conn: object, paths: RuntimePaths, run_id: str) -> dict:
    """一个 run 的看板数据：状态/停止原因、预算已用与剩余、关键词/帖子/有效问题/失败计数、最近发现。"""
    run = repo.get_run(run_id)
    if run is None:
        return {"error": f"run {run_id} 不存在"}
    settings = services.stored_settings(repo, run_id)
    ledger = Ledger.from_settings(conn, run_id, settings)
    budget: list[dict] = []
    for key, view in ledger.status().items():
        budget.append(
            {
                "key": key,
                "limit": view.limit,
                "used": view.used,
                "reserved": view.reserved,
                "remaining": view.remaining,
                "exhausted": view.exhausted,
            }
        )

    conn_ = repo.conn
    keywords = conn_.execute(
        "SELECT COUNT(*) AS n FROM run_keywords WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]
    by_status = {
        str(row["status"]): int(row["n"])
        for row in conn_.execute(
            "SELECT status, COUNT(*) AS n FROM run_keywords WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
    }
    posts = conn_.execute(
        "SELECT COUNT(DISTINCT post_id) AS n FROM post_queries WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]
    failures = conn_.execute(
        "SELECT COUNT(*) AS n FROM operations WHERE run_id = ? AND status = 'failed'", (run_id,)
    ).fetchone()["n"]
    in_flight = conn_.execute(
        "SELECT COUNT(*) AS n FROM operations WHERE run_id = ? AND status IN ('pending','in_flight')",
        (run_id,),
    ).fetchone()["n"]

    events = conn_.execute(
        "SELECT id, at, kind, detail_json FROM run_events WHERE run_id = ? ORDER BY id DESC LIMIT 12",
        (run_id,),
    ).fetchall()
    recent: list[dict] = []
    for row in events:
        recent.append({"at": str(row["at"]), "kind": str(row["kind"]), "detail": json.loads(str(row["detail_json"] or "{}"))})

    # 停止说明走独立按 kind 的查询：一次分析会写入大量 phrase_skipped 等事件，把停止记录挤出
    # "最近 12 条"，只扫近期列表会让真实 fixture run 的预算停止说明在页面消失。
    stop_note = latest_stop_event(conn_, run_id)

    # 有效问题簇/可展示需求属离线分析产物：有已完成批次时读固定文件，不碰可变 needs 表。
    batches = [b for b in services.completed_batches(repo, run_id) if b["verification_ok"]]
    analysis_counts: dict | None = None
    if batches:
        try:
            report = read_batch_report(paths, run_id, batches[-1]["snapshot"])
            excl = report.get("analysis", {}).get("exclusions", {})
            stats = report.get("statistics", {})
            analysis_counts = {
                "snapshot": batches[-1]["snapshot"],
                "analysis_version": batches[-1]["analysis_version"],
                "displayable_needs": excl.get("displayed"),
                "needs_total": stats.get("analysis", {}).get("needs_total"),
                "problems": stats.get("analysis", {}).get("problems"),
                "evidence": stats.get("analysis", {}).get("evidence"),
            }
        except services.AppError:
            analysis_counts = None

    occupied, worker_detail = worker_occupied(conn, paths, run_id)
    return {
        "run_id": run_id,
        "seed_raw": run["seed_raw"],
        "mode": run["mode"],
        "status": run["status"],
        "outcome": run["outcome"],
        "pause_reason": run["pause_reason"],
        "budget_revision": int(run["budget_revision"]),
        "budget": budget,
        "counts": {
            "keywords": int(keywords),
            "keywords_by_status": by_status,
            "posts": int(posts),
            "failures": int(failures),
            "unfinished": int(in_flight),
        },
        "analysis": analysis_counts,
        "recent_events": recent,
        "stop_note": stop_note,
        "worker_occupied": occupied,
        "worker_detail": worker_detail,
    }


# --------------------------------------------------------------------- 批次读取（固定文件+完整性）


def verified_batch_files(
    repo: Repository, paths: RuntimePaths, run_id: str, snapshot: str | None
) -> dict:
    """读所选已完成批次：磁盘哈希=登记哈希=report 内嵌哈希（复用 app.pick_batch/snapshot_self_check）。"""
    batches = services.completed_batches(repo, run_id)
    batch = services.pick_batch(batches, snapshot)
    landed: list[dict] = []
    for item in batch["file_paths"]:
        target = paths.resolve_within_run(str(item["rel_path"]), run_id)
        if not target.is_file():
            raise services.SourceIntegrityError(
                f"批次 {batch['snapshot']} 的登记产物在磁盘上缺失：{target}；历史产物不得伪装可读"
            )
        digest = services.sha256_file(target)
        if digest != str(item["sha256"]):
            raise services.SourceIntegrityError(
                f"{item['filename']} 内容与登记哈希不符，拒绝当作有效导出"
            )
        landed.append({**item, "abs_path": str(target)})
    verification = services.snapshot_self_check(batch, landed, run_id)
    return {
        "snapshot": batch["snapshot"],
        "analysis_version": batch["analysis_version"],
        "extractor_version": batch["extractor_version"],
        "files": landed,
        "verification": verification,
    }


def _file_map(info: dict) -> dict[str, str]:
    return {str(item["filename"]): str(item["abs_path"]) for item in info["files"]}


def _read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_batch_report(paths: RuntimePaths, run_id: str, snapshot: str | None) -> dict:
    from xhs_discovery.analysis.export import EXPORT_FILENAMES  # noqa: F401  (documentation of scope)

    conn = storage_db.connect(paths.db_path)
    try:
        repo = Repository(conn, migrate=False)
        info = verified_batch_files(repo, paths, run_id, snapshot)
        report_path = _file_map(info)["report.json"]
        return json.loads(Path(report_path).read_text(encoding="utf-8"))
    finally:
        conn.close()


def load_batch_dataset(paths: RuntimePaths, run_id: str, snapshot: str | None) -> dict:
    """一次读全四类数据文件 + report.json（经完整性校验），供双图/详情/排行榜共用。"""
    conn = storage_db.connect(paths.db_path)
    try:
        repo = Repository(conn, migrate=False)
        info = verified_batch_files(repo, paths, run_id, snapshot)
        files = _file_map(info)
        return {
            "snapshot": info["snapshot"],
            "analysis_version": info["analysis_version"],
            "verification": info["verification"],
            "keywords": _read_csv(files["keywords.csv"]),
            "needs": _read_csv(files["needs.csv"]),
            "evidence": _read_csv(files["evidence.csv"]),
            "report": json.loads(Path(files["report.json"]).read_text(encoding="utf-8")),
            "graphml": files["graph.graphml"],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------- GraphML 解析


def parse_graphml(path: str) -> tuple[list[dict], list[dict]]:
    """读回 graph.graphml：节点带 ref/kind/label/属性，边带 source_ref/target_ref/relation。"""
    root = ElementTree.parse(path).getroot()
    ns = root.tag.split("}")[0].lstrip("<{") if "}" in root.tag else ""
    tag = (lambda name: f"{{{ns}}}{name}") if ns else (lambda name: name)  # noqa: E731
    keys = {
        key.get("id"): (key.get("for"), key.get("attr.name"), key.get("attr.type"))
        for key in root.findall(tag("key"))
    }
    graph = root.find(tag("graph"))
    nodes: list[dict] = []
    edges: list[dict] = []
    if graph is None:
        return nodes, edges

    def data_map(element, domain: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for data in element.findall(tag("data")):
            meta = keys.get(data.get("key"))
            if meta and meta[0] == domain:
                out[meta[1]] = data.text or ""
        return out

    for node in graph.findall(tag("node")):
        values = data_map(node, "node")
        nodes.append(values)
    for edge in graph.findall(tag("edge")):
        values = data_map(edge, "edge")
        edges.append(values)
    return nodes, edges


def split_graphs(nodes: list[dict], edges: list[dict]) -> tuple[dict, dict]:
    """发现图=keyword 节点 + suggests 边；需求图=其余节点与关系。"""
    kw_ids = {n.get("ref") for n in nodes if n.get("kind") == "keyword"}
    discovery_nodes = [n for n in nodes if n.get("kind") == "keyword"]
    demand_nodes = [n for n in nodes if n.get("kind") != "keyword"]
    demand_ids = {n.get("ref") for n in demand_nodes}
    discovery_edges = [e for e in edges if e.get("relation") == "suggests"]
    demand_edges = [
        e
        for e in edges
        if e.get("relation") != "suggests"
        and e.get("source_ref") in demand_ids
        and e.get("target_ref") in demand_ids
    ]
    return (
        {"nodes": discovery_nodes, "edges": discovery_edges},
        {"nodes": demand_nodes, "edges": demand_edges, "kw_ids": kw_ids},
    )


# --------------------------------------------------------------------- 过滤与布局


def _to_float(value: object) -> float:
    try:
        return float(str(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def filter_demand(nodes: list[dict], edges: list[dict], needs: list[dict], *, topics: Iterable[str], intents: Iterable[str], max_nodes: int) -> tuple[list[dict], list[dict], dict]:
    """按主题/意图过滤需求图并限量；返回 (节点, 边, 过滤说明)。"""
    topic_set = {t for t in topics if t}
    intent_set = {i for i in intents if i}
    keep_nodes: dict[str, dict] = {}
    dropped = {"topic": 0, "intent": 0}

    # 问题节点自带 intent；其 topic 通过 topic→problem 的 has_problem 边还原
    # （topic 是独立节点，不是 problem 的属性）。
    problem_topic: dict[str, str] = {}
    for edge in edges:
        if edge.get("relation") == "has_problem":
            problem_topic[edge.get("target_ref")] = edge.get("source_ref") or ""
    problem_intent = {n.get("ref"): n.get("intent", "") for n in nodes if n.get("kind") == "problem"}

    # 需求 → 问题（经 merged_into 边）用于主题/意图过滤
    need_problems: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("relation") == "merged_into":
            need_problems.setdefault(edge.get("target_ref"), []).append(edge.get("source_ref"))

    for node in nodes:
        kind = node.get("kind")
        ref = node.get("ref")
        if kind == "need":
            problems = need_problems.get(ref, [])
            topics_hit = {problem_topic.get(p, "") for p in problems} or {""}
            intents_hit = {problem_intent.get(p, "") for p in problems} or {""}
            if topic_set and not (topics_hit & topic_set):
                dropped["topic"] += 1
                continue
            if intent_set and not (intents_hit & intent_set):
                dropped["intent"] += 1
                continue
        keep_nodes[ref] = node

    # 需求保留后，只保留两端都在集合内的边
    edges_out = [
        e
        for e in edges
        if e.get("source_ref") in keep_nodes and e.get("target_ref") in keep_nodes
    ]
    capped = len(keep_nodes) > max_nodes
    if capped:
        # 优先保留 need/topic，再补 problem/evidence/post/suggestion，限量并告知
        priority = sorted(
            keep_nodes.values(),
            key=lambda n: (0 if n.get("kind") in ("need", "topic") else 1, n.get("ref") or ""),
        )
        keep_nodes = {n["ref"]: n for n in priority[:max_nodes]}
        edges_out = [
            e for e in edges_out if e.get("source_ref") in keep_nodes and e.get("target_ref") in keep_nodes
        ]
    note = {
        "filtered_topic": dropped["topic"],
        "filtered_intent": dropped["intent"],
        "shown_nodes": len(keep_nodes),
        "capped": capped,
        "max_nodes": max_nodes,
        "total_nodes": len(nodes),
    }
    return list(keep_nodes.values()), edges_out, note


def layout_positions(nodes: list[dict], edges: list[dict], seed: int) -> dict[str, list[float]]:
    """networkx spring_layout（固定 seed，离线确定性）给出二维坐标。"""
    import networkx as nx

    graph = nx.Graph()
    for node in nodes:
        if node.get("ref"):
            graph.add_node(node["ref"])
    for edge in edges:
        source, target = edge.get("source_ref"), edge.get("target_ref")
        if source and target and source in graph and target in graph:
            graph.add_edge(source, target)
    if graph.number_of_nodes() == 0:
        return {}
    if graph.number_of_edges() == 0:
        graph.add_edges_from(zip([n["ref"] for n in nodes][:-1], [n["ref"] for n in nodes][1:]))
    pos = nx.spring_layout(graph, seed=seed % 100000, scale=6.0)
    return {str(key): [float(value[0]), float(value[1])] for key, value in pos.items()}


def fit_positions_to_canvas(
    positions: dict[str, list[float]], *, width: float = 900.0, height: float = 600.0, margin: float = 60.0
) -> dict[str, list[float]]:
    """把布局坐标等比缩放平移到画布像素坐标（OrthographicView zoom=0 时 1 单位=1px）。

    spring_layout 的输出范围与画布无关（scale=6 时 35 个点挤在约 30px 内不可读，C-16 复核项），
    这里按图的包围盒自动适配：可用区 = 画布减去边距，取短边比例，居中放置。
    """
    if not positions:
        return {}
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    avail_w = max(width - 2 * margin, 1.0)
    avail_h = max(height - 2 * margin, 1.0)
    scale = min(avail_w / span_x, avail_h / span_y)
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    return {
        ref: [width / 2 + (p[0] - center_x) * scale, height / 2 + (p[1] - center_y) * scale]
        for ref, p in positions.items()
    }


def label_character_set(labels: Iterable[str]) -> list[str]:
    """TextLayer 字体图集需要覆盖当前实际出现的字符（含中文），否则中文渲染为缺字符警告。"""
    return sorted({ch for label in labels for ch in str(label) if ch.strip()})


# --------------------------------------------------------------------- pydeck 图构建


RADIUS_PX = {"keyword": 11.0, "need": 11.0}
DEFAULT_RADIUS_PX = 8.0
LABEL_HEIGHT_PX = 14.0
CANVAS_W = 900.0
CANVAS_H = 600.0


def build_deck(
    gnodes: list[dict],
    gedges: list[dict],
    positions: dict[str, list[float]],
    kind_color: dict[str, list[int]],
) -> object:
    """组装 pydeck.Deck：像素直角坐标 + OrthographicView(zoom=0)，无地图 token、无外部瓦片。

    - 节点半径为像素值（zoom=0 时数据单位即像素）；
    - TextLayer 显式携带当前标签字符集（character_set→characterSet），中文标签不靠默认图集；
    - 初始视图 target 为画布中心、zoom=0，即 fit_positions_to_canvas 已适配好的像素视野。
    """
    import pydeck as pdk

    points, lines, texts = [], [], []
    for node in gnodes:
        ref = node.get("ref")
        pos = positions.get(ref)
        if pos is None:
            continue
        kind = node.get("kind", "")
        label = node.get("label", "") or ref
        points.append(
            {
                "id": ref,
                "position": pos,
                "color": kind_color.get(kind, [150, 150, 150]),
                "radius": RADIUS_PX.get(kind, DEFAULT_RADIUS_PX),
                "label": label,
            }
        )
        texts.append({"position": [pos[0] + 2, pos[1] + LABEL_HEIGHT_PX], "text": label})
    for edge in gedges:
        source = positions.get(edge.get("source_ref"))
        target = positions.get(edge.get("target_ref"))
        if source and target:
            lines.append({"frm": source, "to": target})

    # 必须是 tuple：pydeck 把 list[str] 序列化成 "@@=[上, 海]"，deck.gl JSON 会把它编译成
    # 访问器函数，TextLayer 的 new Set(characterSet) 随即抛 "function is not iterable"，
    # 整层中文标签消失。tuple 原样落成字面量 JSON 字符串数组。
    charset = tuple(label_character_set(t["text"] for t in texts))

    layers = [
        pdk.Layer(
            "LineLayer", data=lines, get_source_position="frm", get_target_position="to",
            get_color=[120, 120, 120], width_scale=0.3, pickable=False, id="edges",
        ),
        pdk.Layer(
            "ScatterplotLayer", data=points, get_position="position", get_radius="radius",
            get_color="color", pickable=True, id="nodes",
        ),
        pdk.Layer(
            "TextLayer", data=texts, get_position="position", get_text="text",
            get_size=12, get_color=[40, 40, 40], id="labels",
            character_set=charset,
        ),
    ]
    return pdk.Deck(
        views=[pdk.View(type="OrthographicView", controller={"min_zoom": -1, "max_zoom": 8})],
        initial_view_state=pdk.ViewState(target=[CANVAS_W / 2, CANVAS_H / 2], zoom=0, rotation=0, pitch=0),
        layers=layers,
        map_style=None,
        tooltip={"text": "{label}（{id}）"},
    )


# --------------------------------------------------------------------- 排行榜 / 详情


_COMPONENTS = ("S", "Q", "A", "F")


def weight_profile(row: dict) -> tuple:
    """一个需求的 S/Q/A/F 分量档案：每维 (归一化是否可用, 有效权重)。

    分组不能只看 effective_weight_total——总权重相同、缺失分量不同的两条需求，
    证据完整度并不相同，不构成同一名次组（C-17）。
    """
    parts = []
    for letter in _COMPONENTS:
        normalized = str(row.get(f"{letter}_normalized") or "").strip()
        parts.append((letter, bool(normalized), round(_to_float(row.get(f"{letter}_weight")), 6)))
    return tuple(parts)


def _missing_reasons(row: dict) -> dict[str, str]:
    return {
        letter: str(row.get(f"{letter}_missing_reason") or "")
        for letter in _COMPONENTS
        if not str(row.get(f"{letter}_normalized") or "").strip()
    }


def leaderboard(needs: list[dict]) -> list[dict]:
    """按"有效权重配置 + 分量档案 + 表述类别"分层排行。

    分组键 = (statement_kind, effective_weight_total, S/Q/A/F 档案)：
    - 总权重相同但缺失分量不同 → 不同组，不混成同一精确名次（C-17）；
    - 显性问题 / 推断需求（statement_kind）保持明确分区，永不共组；
    - provisional 原因、unknown 分量、原始计数原样保留；无证据/构想/候选不入榜。
    """
    scored: list[dict] = []
    for row in needs:
        if row.get("displayable") != "1" or row.get("score") in (None, ""):
            continue
        if row.get("evidence_status") not in ("verified", "inferred"):
            continue
        if row.get("statement_kind") in ("solution_idea", "search_intent_candidate"):
            continue
        if row.get("evidence_rows") in (None, "", "0"):
            continue
        scored.append(
            {
                "need_id": row["need_id"],
                "canonical_text": row["canonical_text"],
                "statement_kind": row["statement_kind"],
                "evidence_status": row["evidence_status"],
                "score": _to_float(row["score"]),
                "effective_weight_total": _to_float(row.get("effective_weight_total")),
                "weight_profile": weight_profile(row),
                "missing_reasons": _missing_reasons(row),
                "provisional": row.get("provisional"),
                "provisional_reason": row.get("provisional_reason"),
                "evidence_rows": row.get("evidence_rows"),
                "distinct_posts": row.get("distinct_posts"),
                "source_keywords": row.get("source_keywords"),
                "unknown_components": row.get("unknown_components"),
            }
        )
    # 分层：分组键（类别+总权重+分量档案）相同才算同一精确名次组；
    # 组间按总权重降序，再按类别/档案稳定排序；组内按分数降序、need_id 升序。
    weight_groups: dict[tuple, list[dict]] = {}
    for item in scored:
        key = (item["statement_kind"], round(item["effective_weight_total"], 6), item["weight_profile"])
        weight_groups.setdefault(key, []).append(item)
    ordered_keys = sorted(weight_groups, key=lambda k: (-k[1], k[0], repr(k[2])))
    ranked: list[dict] = []
    for group_index, key in enumerate(ordered_keys, start=1):
        members = sorted(weight_groups[key], key=lambda item: (-item["score"], str(item["need_id"])))
        for position, item in enumerate(members, start=1):
            item["weight_group_rank"] = group_index
            item["group_size"] = len(members)
            item["rank_in_group"] = position
            item["group_statement_kind"] = key[0]
            ranked.append(item)
    return ranked


def need_detail(dataset: dict, need_id: str) -> dict | None:
    """从固定批次文件给出一条需求的完整可追溯详情（不重新读可变库）。"""
    row = next((n for n in dataset["needs"] if str(n.get("need_id")) == str(need_id)), None)
    if row is None:
        return None
    linked = [e for e in dataset["evidence"] if str(need_id) in {t for t in str(e.get("need_ids", "")).split(",")}]
    kw_by_norm = {k["normalized"]: k for k in dataset["keywords"]}
    source_terms = [t for t in str(row.get("source_keywords", "")).split(",") if t]
    paths_for_terms = {term: keyword_path(dataset["keywords"], kw_by_norm.get(term, {}).get("keyword_id")) for term in source_terms}
    return {
        "need": row,
        "evidence": linked,
        "source_terms": source_terms,
        "discovery_paths": paths_for_terms,
    }


def keyword_path(keywords: list[dict], keyword_id: str | None) -> list[str]:
    """从 keywords.csv 的 parent 链回溯一个词到种子的发现路径。"""
    by_id = {str(k["keyword_id"]): k for k in keywords}
    chain: list[str] = []
    seen: set[str] = set()
    current = keyword_id
    while current is not None and str(current) in by_id and str(current) not in seen:
        seen.add(str(current))
        node = by_id[str(current)]
        chain.append(str(node["normalized"]))
        current = node.get("parent_keyword_id") or None
    return list(reversed(chain))


# --------------------------------------------------------------------- pydeck 选择解析


def extract_selection_ids(state: object) -> list[str]:
    """从 st.pydeck_chart 的 selection 状态里取被点节点的 ref id。

    当前安装版 Streamlit（`PydeckSelectionState`，streamlit/elements/deck_gl_json_chart.py）
    的结构是 `objects = {layer_id: [metadata_dict, ...]}`：按层 id 分组、值是节点属性字典列表。
    迭代 dict 键拿不到节点——必须展开每个层的 metadata 列表。同时兼容 GeoJSON 风格的
    `[{feature: {id}}]` 列表形态；其余形态一律忽略并返回空，绝不猜测或串用其他 run 的选择。
    """
    ids: list[str] = []
    selection = getattr(state, "selection", None)
    if selection is None:
        return ids
    if isinstance(selection, dict):
        objects = selection.get("objects") or selection.get("points")
    else:
        objects = getattr(selection, "objects", None) or getattr(selection, "points", None)
    if not objects:
        return ids

    records: list[dict] = []
    if isinstance(objects, dict):
        # 真实 API：只取"层 id → 节点 metadata 列表"里合法的列表值
        for layer_id, metadata in objects.items():
            if not isinstance(layer_id, str) or not isinstance(metadata, list):
                continue
            records.extend(item for item in metadata if isinstance(item, dict))
    elif isinstance(objects, (list, tuple)):
        # 兼容形态：[{feature: {...}}] 或 [{id|ref}]
        for item in objects:
            if not isinstance(item, dict):
                continue
            feature = item.get("feature")
            records.append(feature if isinstance(feature, dict) else item)

    for record in records:
        for candidate in (record.get("id"), record.get("ref")):
            if isinstance(candidate, str) and ":" in candidate:
                if candidate not in ids:
                    ids.append(candidate)
                break
    return ids


__all__ = [
    "STOP_EVENT_KINDS",
    "build_deck",
    "derive_settings",
    "extract_selection_ids",
    "filter_demand",
    "fit_positions_to_canvas",
    "keyword_path",
    "label_character_set",
    "last_launch_process_state",
    "latest_stop_event",
    "launch_log_excerpt",
    "launch_worker",
    "layout_positions",
    "leaderboard",
    "live_readiness",
    "load_batch_dataset",
    "need_detail",
    "open_store_conn",
    "parse_graphml",
    "plan_worker_launch",
    "read_batch_report",
    "read_dashboard",
    "read_launch_record",
    "split_graphs",
    "valid_selection_refs",
    "verified_batch_files",
    "weight_profile",
    "worker_launch_status",
    "worker_occupied",
    "write_derived_config",
]
