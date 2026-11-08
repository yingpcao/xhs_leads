"""预算、采样与停止策略（P0-19、P0-20）。

停止判定最容易出错的地方是"把没做完说成做完了"，所以这里的规则写成拒绝式而不是打分式：

- **收敛**要同时满足最低成功观察量、窗口填满、窗口内新增有效问题簇低于下限、推荐重复率高于上限，
  并且**所有活跃分支都停滞**；任何一条不成立就不是收敛，哪怕预算刚好用完；
- **重复率的分母只算成功观察到的建议条数**（completed/partial），观察为空时是"无定义"而不是 0——
  把无定义当 0 会让"平台一直失败"看起来像"从未重复"，反过来当 1 又像"全重复"，两种都会误判收敛；
- **单分支连续停滞只暂缓该分支**：全局停止看的是所有活跃分支都停滞，一个分支跑偏不能停掉整个 run；
- **失败、验证码、无内容预算都不伪装收敛**：异常场景返回 paused 加原因，终态里没有 converged；
- 命中某项预算只封禁对应动作，其他还能执行的动作继续跑（`blocked_actions` 与 `runnable_actions` 分开给）。

阈值全部来自配置，是待实验的起点值；fixture 小任务通常达不到 50 次观察，
按队列或预算退出即可，绝不为了展示收敛而改写指标。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.config import Settings
from xhs_discovery.discovery.merge import PRELIMINARY_ANALYSIS_VERSION
from xhs_discovery.models.enums import (
    ErrorCategory,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    TerminalOutcome,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.run import RunTermination
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository

# completed 与 partial 都拿到了建议，算"成功观察"；失败/跳过/不支持一律排除
SUCCESSFUL_STATES = (ResultState.completed.value, ResultState.partial.value)

# 校准观察是离线分析用的固定抽样，不能进主动探索的收敛判据（计划 5.3：标签与探索隔离）
_CALIBRATION_EXCLUDE = " AND COALESCE(op.sample_kind, '') <> 'coverage_calibration'"

ACTION_BUDGET_KEYS: dict[OperationType, str] = {
    OperationType.suggest: "suggest_attempts",
    OperationType.search: "search_attempts",
    OperationType.body: "body_attempts",
    OperationType.author_posts: "author_attempts",
}

# 异常分类到暂停原因：故障类不停在 running，验证码/登录类必须等人
PAUSE_FOR_CATEGORY: dict[ErrorCategory, PauseReason] = {
    ErrorCategory.auth: PauseReason.auth_required,
    ErrorCategory.action_required: PauseReason.action_required,
    ErrorCategory.profile_busy: PauseReason.profile_busy,
    ErrorCategory.network: PauseReason.network,
    ErrorCategory.page: PauseReason.selector_mismatch,
    ErrorCategory.output: PauseReason.fault,
    ErrorCategory.system: PauseReason.fault,
}


@dataclass(frozen=True)
class WindowMetrics:
    """最近 N 次成功联想观察的重复与新簇统计。"""

    observations: int
    window_observations: int
    window_full: bool
    observed_items: int
    repeat_items: int
    repeat_rate: float | None
    new_clusters: int
    last_observed_at: str | None

    def as_dict(self) -> dict:
        return {
            "successful_observations": self.observations,
            "window_observations": self.window_observations,
            "window_full": self.window_full,
            "observed_items": self.observed_items,
            "repeat_items": self.repeat_items,
            "repeat_rate": self.repeat_rate,
            "new_valid_clusters_in_window": self.new_clusters,
            "window_ends_at": self.last_observed_at,
        }


def successful_observation_count(conn, run_id: str, *, exclude_calibration: bool = True) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM suggestion_observations so JOIN operations op ON op.id = so.operation_id"
        " WHERE so.run_id = ?"
        f" AND so.state IN ({','.join('?' * len(SUCCESSFUL_STATES))})"
        f"{_CALIBRATION_EXCLUDE if exclude_calibration else ''}",
        (run_id, *SUCCESSFUL_STATES),
    ).fetchone()
    return int(row["n"])


def window_metrics(
    conn,
    run_id: str,
    *,
    window: int,
    analysis_version: str | None = None,
    exclude_calibration: bool = True,
) -> WindowMetrics:
    """按时间取最近 `window` 次成功观察，统计重复率与窗口内新增有效问题簇。

    "重复"的定义是**这条建议在本次观察之前就已经被成功观察到过**（同一 keyword_id），
    所以分子相对整个 run 的历史判断，分母只数窗口内的建议条数。
    校准池观察默认排除：分析标签不能混进主动探索的收敛判据。
    """
    exclude = _CALIBRATION_EXCLUDE if exclude_calibration else ""
    states = tuple(SUCCESSFUL_STATES)
    marks = ",".join("?" * len(states))
    total = successful_observation_count(conn, run_id, exclude_calibration=exclude_calibration)
    rows = conn.execute(
        f"SELECT so.id, so.observed_at FROM suggestion_observations so"
        f" JOIN operations op ON op.id = so.operation_id WHERE so.run_id = ?"
        f" AND so.state IN ({marks}){exclude}"
        " ORDER BY so.observed_at DESC, so.id DESC LIMIT ?",
        (run_id, *states, window),
    ).fetchall()
    if not rows:
        return WindowMetrics(
            observations=total,
            window_observations=0,
            window_full=False,
            observed_items=0,
            repeat_items=0,
            repeat_rate=None,
            new_clusters=0,
            last_observed_at=None,
        )
    ids = [int(row["id"]) for row in rows]
    first_observed_at = str(min(row["observed_at"] for row in rows))
    id_marks = ",".join("?" * len(ids))
    items = conn.execute(
        f"SELECT si.keyword_id, si.observation_id, so.observed_at FROM suggestion_items si"
        f" JOIN suggestion_observations so ON so.id = si.observation_id"
        f" WHERE so.run_id = ? AND si.observation_id IN ({id_marks})",
        (run_id, *ids),
    ).fetchall()
    observed_items = len(items)
    repeat_items = 0
    for item in items:
        earlier = conn.execute(
            f"SELECT COUNT(*) AS n FROM suggestion_items si"
            f" JOIN suggestion_observations so ON so.id = si.observation_id"
            f" JOIN operations op ON op.id = so.operation_id"
            f" WHERE so.run_id = ? AND si.keyword_id = ?"
            f" AND (so.observed_at < ? OR (so.observed_at = ? AND so.id < ?))"
            f" AND so.state IN ({marks}){exclude}",
            (
                run_id,
                int(item["keyword_id"]),
                str(item["observed_at"]),
                str(item["observed_at"]),
                int(item["observation_id"]),
                *states,
            ),
        ).fetchone()
        if int(earlier["n"]) > 0:
            repeat_items += 1
    repeat_rate = None if observed_items == 0 else round(repeat_items / observed_items, 6)

    params: list = [run_id, first_observed_at]
    clause = "AND created_at >= ?"
    if analysis_version is not None:
        clause += " AND analysis_version = ?"
        params.append(analysis_version)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM needs WHERE run_id = ?"
        " AND statement_kind = 'explicit_problem'"
        f" {clause}",
        tuple(params),
    ).fetchone()
    return WindowMetrics(
        observations=total,
        window_observations=len(rows),
        window_full=len(rows) >= window,
        observed_items=observed_items,
        repeat_items=repeat_items,
        repeat_rate=repeat_rate,
        new_clusters=int(row["n"]),
        last_observed_at=str(max(row["observed_at"] for row in rows)),
    )


def content_validation_count(conn, run_id: str) -> int:
    """内容验证量：本 run 真正取到正文版本（或明确取到"正文为空"）的帖子数。

    只统计观察卡片不算验证——计划要求收敛前必须有内容证据，卡片标题不能冒充正文。

    归属看观察行自己带的 `run_id`/`operation_id`（v3 起才有），不看全局 `post_id`：
    帖子是跨 run 共用身份，A run 取到过正文并不代表 B run 也取到过。B 只有同篇帖子的
    标题卡片时，`post_queries` 命中而正文观察属于 A——按旧写法会把 A 的读取算成 B 的
    内容验证量，B 就能借 A 的证据过收敛门槛（C-09）。
    来源另有一道核对：行上的 run 与所挂操作的 run 必须都是当前 run，不一致说明数据被
    错位写入，宁可不计数也不能算。老库升级后来源为 NULL 的历史行同样不计入——
    归属无法确定的观察不能事后编一个 run。
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT bo.post_id) AS n FROM body_observations bo"
        " JOIN operations op ON op.id = bo.operation_id"
        " WHERE bo.state = 'completed' AND bo.run_id = ? AND op.run_id = ?"
        " AND EXISTS (SELECT 1 FROM post_queries pq"
        " WHERE pq.post_id = bo.post_id AND pq.run_id = ?)",
        (run_id, run_id, run_id),
    ).fetchone()
    return int(row["n"])


@dataclass(frozen=True)
class BranchState:
    branch_key: str
    trials: int
    stale_streak: int
    min_validation_share_left: int
    pending_items: int

    @property
    def covered(self) -> bool:
        return self.trials > 0


def branch_states(repo: Repository, run_id: str) -> tuple[BranchState, ...]:
    states: list[BranchState] = []
    rows = repo.conn.execute(
        "SELECT b.branch_key, COALESCE(s.trials, 0) AS trials,"
        " COALESCE(s.stale_streak, 0) AS stale_streak,"
        " COALESCE(s.min_validation_share_left, 0) AS min_validation_share_left"
        " FROM branches b LEFT JOIN branch_stats s ON s.run_id = b.run_id AND s.branch_key = b.branch_key"
        " WHERE b.run_id = ? ORDER BY b.branch_key",
        (run_id,),
    ).fetchall()
    for row in rows:
        key = str(row["branch_key"])
        pending = repo.conn.execute(
            "SELECT COUNT(*) AS n FROM frontier f JOIN branches b ON b.id = f.branch_id"
            " WHERE f.run_id = ? AND b.branch_key = ? AND f.status IN ('pending', 'in_flight')",
            (run_id, key),
        ).fetchone()
        states.append(
            BranchState(
                branch_key=key,
                trials=int(row["trials"]),
                stale_streak=int(row["stale_streak"]),
                min_validation_share_left=int(row["min_validation_share_left"]),
                pending_items=int(pending["n"]),
            )
        )
    return tuple(states)


def stalled_branches(states: tuple[BranchState, ...], *, stall_trials: int) -> tuple[str, ...]:
    """单分支停滞：连续 `stall_trials` 次操作无新增。

    队列临时为空不算停滞（扩展后还会重新有候选），只有无新增的连续次数达阈值才暂缓，
    这样"一个分支跑偏"不会误停整个 run。
    """
    return tuple(
        state.branch_key for state in states if state.stale_streak >= stall_trials
    )


def exhausted_branches(states: tuple[BranchState, ...]) -> tuple[str, ...]:
    """覆盖过但已无候选的分支：参与"所有活跃分支停滞/无候选"的全局判定。"""
    return tuple(state.branch_key for state in states if state.covered and state.pending_items == 0)


def uncovered_branches(states: tuple[BranchState, ...]) -> tuple[str, ...]:
    return tuple(state.branch_key for state in states if not state.covered)


@dataclass(frozen=True)
class BudgetState:
    exhausted_keys: tuple[str, ...]
    blocked_actions: tuple[OperationType, ...]
    runnable_actions: tuple[OperationType, ...]
    runtime_limit_hit: bool

    def as_dict(self) -> dict:
        return {
            "exhausted_keys": list(self.exhausted_keys),
            "blocked_actions": [action.value for action in self.blocked_actions],
            "runnable_actions": [action.value for action in self.runnable_actions],
            "runtime_limit_hit": self.runtime_limit_hit,
        }


def budget_state(
    repo: Repository,
    run_id: str,
    ledger: Ledger,
    *,
    actions: tuple[OperationType, ...] = tuple(OperationType),
    now: datetime | None = None,
) -> BudgetState:
    status = ledger.status()
    exhausted = tuple(key for key, view in status.items() if view.exhausted)
    blocked = tuple(
        action
        for action in actions
        if ACTION_BUDGET_KEYS[action] in exhausted
        or not ledger.can_afford(ACTION_BUDGET_KEYS[action])
    )
    runnable = tuple(action for action in actions if action not in blocked)
    runtime_hit = "max_runtime_minutes" in exhausted
    if not runtime_hit:
        started = repo.conn.execute(
            "SELECT started_at FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        view = status.get("max_runtime_minutes")
        if view is not None and view.limit > 0:
            if started is not None and started["started_at"] is not None and now is not None:
                elapsed = (now - datetime.fromisoformat(str(started["started_at"]))).total_seconds() / 60.0
                runtime_hit = elapsed >= view.limit
    return BudgetState(
        exhausted_keys=exhausted,
        blocked_actions=blocked,
        runnable_actions=runnable,
        runtime_limit_hit=runtime_hit,
    )


@dataclass(frozen=True)
class StopSignals:
    window: WindowMetrics
    budget: BudgetState
    branches: tuple[BranchState, ...]
    stalled: tuple[str, ...]
    exhausted: tuple[str, ...]
    uncovered: tuple[str, ...]
    content_validations: int
    pending_items: int

    @property
    def quiet_branches(self) -> frozenset[str]:
        """停滞或无候选的分支：全局收敛看的是"活跃分支全部安静"。"""
        return frozenset(self.stalled) | frozenset(self.exhausted)

    def as_dict(self) -> dict:
        return {
            "window": self.window.as_dict(),
            "budget": self.budget.as_dict(),
            "stalled_branches": list(self.stalled),
            "exhausted_branches": list(self.exhausted),
            "uncovered_branches": list(self.uncovered),
            "content_validations": self.content_validations,
            "pending_items": self.pending_items,
        }


@dataclass(frozen=True)
class StopDecision:
    status: RunStatus
    outcome: TerminalOutcome | None
    pause_reason: PauseReason | None
    note: str
    signals: StopSignals

    def as_termination(self) -> RunTermination:
        return RunTermination(
            status=self.status, outcome=self.outcome, pause_reason=self.pause_reason, note=self.note
        )


def collect_signals(
    repo: Repository,
    run_id: str,
    settings: Settings,
    ledger: Ledger,
    *,
    actions: tuple[OperationType, ...] = tuple(OperationType),
    now: datetime | None = None,
) -> StopSignals:
    stopping = settings.stopping
    window = window_metrics(
        repo.conn,
        run_id,
        window=stopping.convergence_window,
        analysis_version=PRELIMINARY_ANALYSIS_VERSION,
    )
    states = branch_states(repo, run_id)
    return StopSignals(
        window=window,
        budget=budget_state(repo, run_id, ledger, actions=actions, now=now),
        branches=states,
        stalled=stalled_branches(states, stall_trials=stopping.branch_stall_trials),
        exhausted=exhausted_branches(states),
        uncovered=uncovered_branches(states),
        content_validations=content_validation_count(repo.conn, run_id),
        # C22：未完成（in_flight）槽位也是待办的一部分。接管前它可能正等着离线补入库或人工处置，
        # 只数 pending 会把"还剩一个在飞槽位"误判成队列已排空，进而假收敛/假 queue_exhausted。
        pending_items=len(repo.frontier_rows(run_id, status="pending"))
        + len(repo.frontier_rows(run_id, status="in_flight")),
    )


def convergence_failures(signals: StopSignals, settings: Settings) -> list[str]:
    """逐条列出"为什么还不算收敛"，报告里必须能看到缺哪一项。"""
    stopping = settings.stopping
    window = signals.window
    failures: list[str] = []
    if window.observations < stopping.min_successful_suggestion_observations:
        failures.append(
            f"成功联想观察 {window.observations} 次，不足最低 {stopping.min_successful_suggestion_observations} 次"
        )
    if not window.window_full:
        failures.append(
            f"收敛窗口只填满 {window.window_observations}/{stopping.convergence_window} 次观察"
        )
    if stopping.require_content_validation and signals.content_validations == 0:
        failures.append("还没有任何内容验证（正文），不能只凭联想重复判定收敛")
    if window.repeat_rate is None:
        failures.append("窗口内没有成功观察到的建议，重复率无定义")
    elif window.repeat_rate <= stopping.repeat_rate_ceiling:
        failures.append(
            f"窗口重复率 {window.repeat_rate:.4f} 未超过阈值 {stopping.repeat_rate_ceiling:.2f}"
        )
    if window.new_clusters >= stopping.new_problem_cluster_floor:
        failures.append(
            f"窗口内新增有效问题簇 {window.new_clusters}，未低于下限 {stopping.new_problem_cluster_floor}"
        )
    if signals.uncovered:
        failures.append(f"仍有分支未覆盖一次：{sorted(signals.uncovered)}")
    active = [
        state.branch_key
        for state in signals.branches
        if state.branch_key not in signals.quiet_branches
    ]
    if active:
        failures.append(f"仍有活跃分支未停滞且无候选：{sorted(active)}")
    return failures


def evaluate(
    repo: Repository,
    run_id: str,
    settings: Settings,
    ledger: Ledger,
    *,
    actions: tuple[OperationType, ...] = tuple(OperationType),
    error: ErrorInfo | None = None,
    now: datetime | None = None,
) -> StopDecision:
    signals = collect_signals(
        repo, run_id, settings, ledger, actions=actions, now=now
    )
    if error is not None:
        if error.category is ErrorCategory.cancelled:
            return StopDecision(
                status=RunStatus.completed,
                outcome=TerminalOutcome.cancelled,
                pause_reason=None,
                note=f"操作被取消（code={error.code}）：终态记 cancelled，不记收敛",
                signals=signals,
            )
        if error.category is ErrorCategory.budget:
            return StopDecision(
                status=RunStatus.paused,
                outcome=None,
                pause_reason=PauseReason.fault,
                note=(
                    f"预算不足跳过（code={error.code}）：这是预算未达，追加预算后可恢复，"
                    "不得记为收敛"
                ),
                signals=signals,
            )
        reason = PAUSE_FOR_CATEGORY.get(error.category, PauseReason.fault)
        return StopDecision(
            status=RunStatus.paused,
            outcome=None,
            pause_reason=reason,
            note=(
                f"{error.category.value} 异常（code={error.code}）"
                f"{'需人工处理' if error.human_action_required else '重试已用尽'}"
                f"：{error.message}；异常与验证码不得记为收敛"
            ),
            signals=signals,
        )

    missing = convergence_failures(signals, settings)
    if not missing:
        window = signals.window
        return StopDecision(
            status=RunStatus.completed,
            outcome=TerminalOutcome.converged,
            pause_reason=None,
            note=(
                f"收敛：{window.observations} 次成功联想观察，窗口 {window.window_observations} 次内"
                f"新增有效问题簇 {window.new_clusters} < {settings.stopping.new_problem_cluster_floor}，"
                f"重复率 {window.repeat_rate:.4f} > {settings.stopping.repeat_rate_ceiling}，"
                f"内容验证 {signals.content_validations} 篇，全部活跃分支停滞"
            ),
            signals=signals,
        )

    if signals.pending_items == 0:
        blocked = signals.budget.blocked_actions
        all_blocked = len(blocked) == len(actions) or signals.budget.runtime_limit_hit
        if all_blocked:
            keys = sorted(signals.budget.exhausted_keys)
            return StopDecision(
                status=RunStatus.completed,
                outcome=TerminalOutcome.budget_exhausted,
                pause_reason=None,
                note=(
                    f"队列为空且全部动作不可执行（预算/时长：{keys}）终止；"
                    f"未满足的收敛条件：{'；'.join(missing)}"
                ),
                signals=signals,
            )
        return StopDecision(
            status=RunStatus.completed,
            outcome=TerminalOutcome.queue_exhausted,
            pause_reason=None,
            note=f"待探索队列耗尽，可执行动作仍在预算内；未满足的收敛条件：{'；'.join(missing)}",
            signals=signals,
        )

    if not signals.budget.runnable_actions and not signals.budget.runtime_limit_hit:
        return StopDecision(
            status=RunStatus.paused,
            outcome=None,
            pause_reason=PauseReason.fault,
            note=(
                f"队列尚有 {signals.pending_items} 项但所有动作预算耗尽（{sorted(signals.budget.exhausted_keys)}）；"
                "这是预算未达而非收敛，追加预算后可恢复"
            ),
            signals=signals,
        )

    return StopDecision(
        status=RunStatus.running,
        outcome=None,
        pause_reason=None,
        note=f"继续探索（{signals.pending_items} 项待办，可执行动作 {len(signals.budget.runnable_actions)} 类）",
        signals=signals,
    )


def apply_branch_stalls(
    repo: Repository, run_id: str, settings: Settings, signals: StopSignals
) -> tuple[str, ...]:
    """单分支停滞只暂缓该分支：记审计事件，run 状态与 stale_streak 都不再动。

    `stale_streak` 由奖励更新负责，这里若再 +1 会把"暂缓"算成又一次无新增；
    暂缓的实际效果是调度器按 `signals.stalled` 过滤候选，分支之后若又产出新簇会自动解除。
    """
    stall_trials = settings.stopping.branch_stall_trials
    recorded: dict[str, int] = {}
    for row in repo.events(run_id, kind="branch_suspended"):
        detail = json.loads(row["detail_json"] or "{}")
        key = str(detail.get("branch_key", ""))
        recorded[key] = max(recorded.get(key, 0), int(detail.get("stale_streak", 0)))
    suspended: list[str] = []
    for state in signals.branches:
        if state.trials == 0 or state.stale_streak < stall_trials:
            continue
        if recorded.get(state.branch_key, 0) >= state.stale_streak:
            continue
        repo.record_event(
            run_id,
            "branch_suspended",
            detail={"branch_key": state.branch_key, "stale_streak": state.stale_streak},
        )
        suspended.append(state.branch_key)
    return tuple(suspended)


__all__ = [
    "ACTION_BUDGET_KEYS",
    "PAUSE_FOR_CATEGORY",
    "SUCCESSFUL_STATES",
    "BranchState",
    "BudgetState",
    "StopDecision",
    "StopSignals",
    "WindowMetrics",
    "apply_branch_stalls",
    "branch_states",
    "budget_state",
    "collect_signals",
    "content_validation_count",
    "exhausted_branches",
    "convergence_failures",
    "evaluate",
    "stalled_branches",
    "successful_observation_count",
    "uncovered_branches",
    "window_metrics",
]
