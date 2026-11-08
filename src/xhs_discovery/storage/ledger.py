"""预算账本（P0-08）：预留-结算、外部访问计量、幂等记账。

规则来自 `docs/DATA_MODEL.md` 第 2/6 节与 `models/run.BudgetLedgerEntry`：
- 一次实际发起的外部尝试（含失败重试）都必须计量，失败不退款；
- 缓存命中不占外部访问预算，但单独记 0 单位事件；
- 存储/时长/模型额度是非访问类配额，可以不记外部访问但仍要计量；
- 幂等只作用于"同一个已完成 attempt 的同一笔账"：同一 `(run_id, operation_id, attempt_no)`
  的外部访问只能记一条（部分唯一索引）。重启后新发起的尝试必须用新的 `attempt_no` 并照常计费，
  账本绝不承诺"重复访问网络不重复扣费"；重放时参数与原账本不一致（换键、换类型、换单位）
  是两笔互相矛盾的账，必须拒绝而不是悄悄返回"已记过"；
- 任何写账入口都先核对归属：操作属于本 run、操作类型与库里登记的一致、预算键属于该操作类型、
  要结算的 attempt 真的发起过——有预留和没预留同口径；
- 额度上限 = 配置快照 + `budget_revisions` 已提交增量，每次读数都从库里算，
  所以重启/重建对象会重放追加，而事务回滚不会留下只存在于内存里的额度。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.config import Settings
from xhs_discovery.models.enums import OperationState, OperationType
from xhs_discovery.storage import db
from xhs_discovery.storage.migrations import ACCESS_KEYS, BUDGET_KEYS
from xhs_discovery.models.common import utc_now

_NOT_ENOUGH = "预算不足：{key} 请求 {requested}，剩余 {remaining}（已用 {used}/{limit}）"

# 只有这两类配额天生是小数；其余按键必须整数记账，避免 int() 悄悄截断
FRACTIONAL_QUOTA_KEYS = frozenset({"max_runtime_minutes", "model_cost_usd"})

# 还有 worker 会接手或正在跑的操作：为它们留着的预留不属于"幽灵占用"
LIVE_OPERATION_STATES = frozenset({OperationState.pending.value, OperationState.in_flight.value})

# 每个操作类型只能花自己那一桶访问额度：把 suggest 的访问记进 search_attempts，
# 等于同时改了两本账——一簿虚增、一簿虚减，事后按桶复盘必然对不上实际发起。
ACCESS_KEY_BY_OPERATION_TYPE: dict[OperationType, frozenset[str]] = {
    OperationType.suggest: frozenset({"suggest_attempts", "calibration_suggest_attempts"}),
    OperationType.search: frozenset({"search_attempts"}),
    OperationType.body: frozenset({"body_attempts"}),
    OperationType.author_posts: frozenset({"author_attempts"}),
}


class BudgetExhausted(RuntimeError):
    """预算耗尽：调用方应把 run 停在 `budget_exhausted`，而不是继续发起访问。"""

    def __init__(self, key: str, requested: float, remaining: float, used: float, limit: float) -> None:
        super().__init__(_NOT_ENOUGH.format(key=key, requested=requested, remaining=remaining,
                                           used=used, limit=limit))
        self.budget_key = key
        self.requested = requested
        self.remaining = remaining
        self.used = used
        self.limit = limit


@dataclass(frozen=True)
class BudgetView:
    budget_key: str
    limit: float
    used: float
    reserved: float

    @property
    def remaining(self) -> float:
        return max(self.limit - self.used - self.reserved, 0.0)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0


def _iso(value: datetime | None) -> str:
    if value is None:
        return utc_now().isoformat()
    if value.tzinfo is None:
        raise ValueError("预算时间戳必须带时区")
    return value.isoformat()


class Ledger:
    """一个 run 的预算视图与写入入口；上限来自配置快照，不读全局可变量。"""

    def __init__(self, conn: sqlite3.Connection, run_id: str, limits: dict[str, float]) -> None:
        self.conn = conn
        self.run_id = run_id
        unknown = set(limits) - set(BUDGET_KEYS)
        if unknown:
            raise ValueError(f"未知预算键：{sorted(unknown)}")
        self.base_limits = {key: float(limits[key]) for key in BUDGET_KEYS if key in limits}

    @contextmanager
    def write(self):
        """账本写入的唯一出口：外层事务在时只开 SAVEPOINT，不替调用方提交半批。"""
        with db.transaction(self.conn):
            yield self.conn

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, run_id: str, settings: Settings) -> "Ledger":
        budget = settings.budget
        limits = {
            "suggest_attempts": budget.suggest_attempts,
            "search_attempts": budget.search_attempts,
            "body_attempts": budget.body_attempts,
            "author_attempts": budget.author_attempts,
            "calibration_suggest_attempts": budget.calibration_suggest_attempts,
            "max_posts_stored": budget.max_posts_stored,
            "max_runtime_minutes": budget.max_runtime_minutes,
            "model_calls": budget.model_calls,
            "model_tokens": budget.model_tokens,
            "model_cost_usd": budget.model_cost_usd,
        }
        return cls(conn, run_id, limits)

    # -------------------------------------------------- 读数

    def _committed_adds(self) -> dict[str, float]:
        """重放已提交的追加：额度只认库里的 budget_revisions，不认对象里的私有状态。"""
        adds: dict[str, float] = {}
        for row in self.conn.execute(
            "SELECT added_json FROM budget_revisions WHERE run_id = ? ORDER BY id", (self.run_id,)
        ):
            payload = json.loads(row["added_json"])
            for key, value in payload.items():
                if key not in BUDGET_KEYS:
                    raise ValueError(f"budget_revisions 里有未知预算键 {key}，账本已不可信")
                adds[key] = adds.get(key, 0.0) + float(value)
        return adds

    @property
    def limits(self) -> dict[str, float]:
        effective = dict(self.base_limits)
        for key, value in self._committed_adds().items():
            if key not in effective:
                raise ValueError(f"该 run 未配置预算键 {key}：追加额度不能凭空造出预算")
            effective[key] += value
        return effective

    def used(self, key: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(consumed_units), 0) AS total FROM budget_ledger"
            " WHERE run_id = ? AND budget_key = ?",
            (self.run_id, key),
        ).fetchone()
        return float(row["total"])

    def outstanding_reservations(self, key: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS total FROM budget_reservations"
            " WHERE run_id = ? AND budget_key = ? AND consumed_at IS NULL",
            (self.run_id, key),
        ).fetchone()
        return float(row["total"])

    def view(self, key: str) -> BudgetView:
        limits = self.limits
        if key not in limits:
            raise ValueError(f"该 run 未配置预算键 {key}：不存在隐含无限预算")
        return BudgetView(
            budget_key=key,
            limit=limits[key],
            used=self.used(key),
            reserved=self.outstanding_reservations(key),
        )

    def status(self) -> dict[str, BudgetView]:
        return {key: self.view(key) for key in self.base_limits}

    def can_afford(self, key: str, units: float = 1) -> bool:
        return self.view(key).remaining >= units

    def _limit(self, key: str) -> float:
        limits = self.limits
        if key not in limits:
            raise ValueError(f"该 run 未配置预算键 {key}：不存在隐含无限预算")
        return limits[key]

    def _validated_units(self, key: str, units: float, *, for_reserve: bool) -> float:
        """按键的类型校核单位：访问次数只能整数，时长/费用保留小数精度。"""
        if key in ACCESS_KEYS:
            if float(units) != int(units):
                raise ValueError(f"{key} 按整次访问计量，不接受小数 {units}")
            if int(units) < 1:
                raise ValueError("预留至少 1 个单位；0 单位请直接记缓存/配额事件" if for_reserve
                                 else f"{key} 一次外部访问至少 1 单位")
            return float(int(units))
        if float(units) <= 0:
            raise ValueError("配额单位必须为正数；0 单位事件请直接记缓存")
        if key not in FRACTIONAL_QUOTA_KEYS and float(units) != int(units):
            raise ValueError(f"{key} 按整数计量，{units} 会被截断——请记整数或换键")
        return round(float(units), 6)

    def _validated_operation(
        self, operation_id: int, *, operation_type: OperationType | None = None,
        key: str | None = None, attempt_no: int | None = None
    ) -> sqlite3.Row:
        """写账前核对操作归属：跨 run、类型不符、键与类型不符、attempt 不存在一律拒绝。

        外键只保证 operation_id 曾经存在，不保证它就是"本次结算声称的那个操作"：
        不核对，就能拿 B run 的 suggest 访问去扣 A run 的 search 额度，两本账同时失真。
        """
        row = self.conn.execute(
            "SELECT id, run_id, operation_type FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"操作不存在：{operation_id}")
        if row["run_id"] != self.run_id:
            raise ValueError(f"操作 {operation_id} 属于 run {row['run_id']}，本账只服务 {self.run_id}")
        actual = OperationType(row["operation_type"])
        if operation_type is not None and actual is not operation_type:
            raise ValueError(
                f"操作 {operation_id} 的类型是 {actual.value}，结算却说是 {operation_type.value}"
            )
        if key is not None and key in ACCESS_KEYS:
            allowed = ACCESS_KEY_BY_OPERATION_TYPE[actual]
            if key not in allowed:
                raise ValueError(
                    f"预算键 {key} 不属于操作类型 {actual.value}"
                    f"（该类型只花 {sorted(allowed)}）"
                )
        if attempt_no is not None:
            attempt = self.conn.execute(
                "SELECT attempt_no FROM operation_attempts WHERE operation_id = ? AND attempt_no = ?",
                (operation_id, attempt_no),
            ).fetchone()
            if attempt is None:
                raise ValueError(
                    f"操作 {operation_id} 没有 attempt {attempt_no}：没发起过的访问不能记账"
                )
        return row

    def _access_row(self, operation_id: int, attempt_no: int) -> sqlite3.Row | None:
        """该 attempt 已有的一笔外部访问账（部分唯一索引保证至多一条）。"""
        return self.conn.execute(
            "SELECT * FROM budget_ledger WHERE run_id = ? AND operation_id = ? AND attempt_no = ?"
            " AND external_access = 1",
            (self.run_id, operation_id, attempt_no),
        ).fetchone()

    def _check_replay(
        self, row: sqlite3.Row, *, operation_id: int, operation_type: OperationType,
        key: str, units: float
    ) -> None:
        """重放只对"同一件事"幂等：键、类型、单位任一不符就是两笔互相矛盾的账，拒绝改写。"""
        conflicts = []
        if str(row["budget_key"]) != key:
            conflicts.append(f"预算键 {row['budget_key']} ≠ {key}")
        if str(row["operation_type"]) != operation_type.value:
            conflicts.append(f"操作类型 {row['operation_type']} ≠ {operation_type.value}")
        if float(row["consumed_units"]) != units:
            conflicts.append(f"消耗单位 {row['consumed_units']} ≠ {units}")
        if conflicts:
            raise ValueError(
                f"操作 {operation_id} 的 attempt {row['attempt_no']} 已有账本行 {row['id']}，"
                f"重放参数与既成事实冲突：{'；'.join(conflicts)}"
            )

    def _reservation(self, reservation_id: int, *, key: str | None, operation_id: int | None) -> sqlite3.Row:
        """取预留并校验归属：跨 run、换键、换操作、换单位都不能拿别人的预留来结算。"""
        row = self.conn.execute(
            "SELECT * FROM budget_reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"预留不存在：{reservation_id}")
        if row["run_id"] != self.run_id:
            raise ValueError(f"预留 {reservation_id} 属于 run {row['run_id']}，本账只服务 {self.run_id}")
        if key is not None and row["budget_key"] != key:
            raise ValueError(f"预留 {reservation_id} 记的是 {row['budget_key']}，结算说是 {key}")
        if operation_id is not None and row["operation_id"] != operation_id:
            raise ValueError(
                f"预留 {reservation_id} 属于操作 {row['operation_id']}，结算说是操作 {operation_id}"
            )
        return row

    def _match_reservation(self, operation_id: int, key: str, units: float) -> sqlite3.Row | None:
        """调用方忘了带 reservation_id 时，认领本操作本键上金额吻合的那笔预留。

        不认领就会留下一条永远占着额度的幽灵预留；认领条件收紧到"同操作 + 同键 + 同单位"，
        不会把别人的预留糊里糊涂花掉。
        """
        return self.conn.execute(
            "SELECT * FROM budget_reservations WHERE run_id = ? AND budget_key = ?"
            " AND operation_id = ? AND consumed_at IS NULL AND units = ? ORDER BY id LIMIT 1",
            (self.run_id, key, operation_id, units),
        ).fetchone()

    # -------------------------------------------------- 写入

    def reserve(
        self, key: str, units: float = 1, *, operation_id: int | None = None, at: datetime | None = None
    ) -> int:
        """先预留再访问：预留未消费的额度同样挡住后续操作，防止越预算发起。

        访问次数按"整次"预留；运行分钟、模型美元这类配额允许小数，否则 0.4 分钟的预留
        会被要求成"至少 1 分钟/1 美元"，短任务永远无法开工。
        """
        value = self._validated_units(key, units, for_reserve=True)
        snapshot = self.view(key)
        if snapshot.remaining < value:
            raise BudgetExhausted(key, value, snapshot.remaining, snapshot.used, snapshot.limit)
        operation_type = None
        if operation_id is not None:
            operation_type = self._validated_operation(operation_id, key=key)["operation_type"]
        with self.write():
            cursor = self.conn.execute(
                "INSERT INTO budget_reservations(run_id, budget_key, units, created_at, operation_id,"
                " operation_type) VALUES (?, ?, ?, ?, ?, ?)",
                (self.run_id, key, value, _iso(at), operation_id, operation_type),
            )
        return int(cursor.lastrowid)

    def release(
        self,
        reservation_id: int,
        *,
        key: str | None = None,
        operation_id: int | None = None,
        at: datetime | None = None,
    ) -> None:
        """放弃预留（操作最终没有发生）：标记 consumed_at，额度立刻放回可用池，不改历史行。

        只按 id 更新会误伤别的 run / 别的操作的预留，所以调用方至少要交出该预留的键或操作，
        对不上就拒绝；UPDATE 再带一次 run_id 兜底。
        """
        if key is None and operation_id is None:
            raise ValueError("释放预留必须声明归属（budget_key 或 operation_id），不接受只给 id")
        row = self._reservation(reservation_id, key=key, operation_id=operation_id)
        if row["consumed_at"] is not None:
            raise ValueError(f"预留 {reservation_id} 已结算或已释放，不能重复放开")
        with self.write():
            self.conn.execute(
                "UPDATE budget_reservations SET consumed_at = ?"
                " WHERE id = ? AND run_id = ? AND consumed_at IS NULL",
                (_iso(at), reservation_id, self.run_id),
            )

    def settle_external(
        self,
        *,
        operation_id: int,
        operation_type: OperationType,
        attempt_no: int,
        key: str,
        units: float = 1,
        reason: str,
        reservation_id: int | None = None,
        at: datetime | None = None,
    ) -> int | None:
        """一次真实外部访问结算：同一 attempt 的同一件事幂等，参数冲突一律拒绝。

        有预留和没预留走同一套归属核对：操作属于本 run、类型与库里登记的一致、预算键属于该
        类型、attempt 真的发起过。任何一条不符都在写账之前失败，不留半笔账也不消费别人的预留。
        """
        if key not in ACCESS_KEYS:
            raise ValueError(f"{key} 不是外部访问预算键，非访问额度请用 record_quota")
        value = self._validated_units(key, units, for_reserve=False)
        reservation = None
        if reservation_id is not None:
            reservation = self._reservation(reservation_id, key=key, operation_id=operation_id)
            if reservation["operation_type"] not in (None, operation_type.value):
                raise ValueError(
                    f"预留 {reservation_id} 记的操作类型是 {reservation['operation_type']}，"
                    f"结算却说是 {operation_type.value}"
                )
            if float(reservation["units"]) != value:
                raise ValueError(f"预留 {reservation_id} 是 {reservation['units']} 单位，结算写了 {value}")
        self._validated_operation(
            operation_id, operation_type=operation_type, key=key, attempt_no=attempt_no
        )
        existing = self._access_row(operation_id, attempt_no)
        if existing is not None:
            self._check_replay(existing, operation_id=operation_id, operation_type=operation_type,
                               key=key, units=value)
            return None
        # 没有预留的结算同样要过额度：可用额度还要扣掉别的未消费预留，
        # 否则"绕过 reserve"或"同一份额度花两次"就是无限超支的后门。
        if reservation is None:
            reservation = self._match_reservation(operation_id, key, value)
        limit = self._limit(key)
        used = self.used(key)
        outstanding = self.outstanding_reservations(key)
        if reservation is not None and reservation["consumed_at"] is None:
            outstanding -= float(reservation["units"])
        if used + value + outstanding > limit:
            raise BudgetExhausted(key, value, max(limit - used - outstanding, 0.0), used, limit)
        return self._record_access(
            operation_id=operation_id,
            operation_type=operation_type,
            attempt_no=attempt_no,
            key=key,
            units=value,
            reason=reason,
            reservation=reservation,
            at=at,
        )

    def _record_access(
        self,
        *,
        operation_id: int,
        operation_type: OperationType,
        attempt_no: int,
        key: str,
        units: float,
        reason: str,
        reservation: sqlite3.Row | None = None,
        at: datetime | None = None,
    ) -> int | None:
        """写一条外部访问消耗（同一 attempt 的同一件事幂等）；额度校验由调用方负责，恢复路径要能记录既成事实。

        恢复路径绕过的是"够不够花"，绕不过归属核对：补记的账同样必须落在本 run、本类型、
        真发起过的 attempt 上，否则崩溃恢复反而成了伪造账目的入口。
        """
        self._validated_operation(
            operation_id, operation_type=operation_type, key=key, attempt_no=attempt_no
        )
        existing = self._access_row(operation_id, attempt_no)
        if existing is not None:
            self._check_replay(existing, operation_id=operation_id, operation_type=operation_type,
                               key=key, units=units)
            return None
        reservation_id = None if reservation is None else int(reservation["id"])
        at_iso = _iso(at)
        with self.write():
            cursor = self.conn.execute(
                "INSERT INTO budget_ledger(run_id, operation_id, budget_key, operation_type, attempt_no,"
                " consumed_units, external_access, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    self.run_id,
                    operation_id,
                    key,
                    operation_type.value,
                    attempt_no,
                    units,
                    reason,
                    at_iso,
                ),
            )
            if reservation_id is not None:
                self.conn.execute(
                    "UPDATE budget_reservations SET consumed_at = ?, operation_type = ?"
                    " WHERE id = ? AND run_id = ? AND consumed_at IS NULL",
                    (at_iso, operation_type.value, reservation_id, self.run_id),
                )
        return int(cursor.lastrowid)

    def record_cache_hit(self, *, key: str, reason: str, operation_id: int | None = None,
                         at: datetime | None = None) -> int:
        """缓存命中：0 外部单位，但仍是一条可审计事实，归属同样要核对到操作上。"""
        if key not in ACCESS_KEYS:
            raise ValueError(f"{key} 不是外部访问预算键")
        if operation_id is not None:
            self._validated_operation(operation_id, key=key)
        with self.write():
            cursor = self.conn.execute(
                "INSERT INTO budget_ledger(run_id, operation_id, budget_key, consumed_units,"
                " external_access, reason, created_at) VALUES (?, ?, ?, 0, 0, ?, ?)",
                (self.run_id, operation_id, key, reason, _iso(at)),
            )
        return int(cursor.lastrowid)

    def record_quota(
        self, key: str, units: float, *, reason: str, operation_id: int | None = None,
        reservation_id: int | None = None, at: datetime | None = None
    ) -> int:
        """非访问配额（存储篇数、运行分钟、模型额度）。

        带 `reservation_id` 时是把先前的预留转成消耗；不带时可用额度还要扣掉未消费的预留，
        否则"预留 + 直接记账"会各占一份同一额度。小数单位按原值存回，不再 int() 截断。
        归属核对发生在写账之前：配额行也要能回答"这是本 run 哪个操作花掉的"。
        """
        if key in ACCESS_KEYS:
            raise ValueError(f"{key} 是外部访问预算键，请用 settle_external 结算")
        if units < 0:
            raise ValueError("配额消耗不得为负")
        value = self._validated_units(key, units, for_reserve=False)
        reservation = None
        if reservation_id is not None:
            reservation = self._reservation(reservation_id, key=key, operation_id=operation_id)
        owner_id = operation_id
        if owner_id is None and reservation is not None and reservation["operation_id"] is not None:
            owner_id = int(reservation["operation_id"])
        if owner_id is not None:
            self._validated_operation(owner_id)
        limit = self._limit(key)
        used = self.used(key)
        available = limit - used
        if reservation_id is None:
            available -= self.outstanding_reservations(key)
        if used + value > limit or available < value:
            raise BudgetExhausted(key, value, max(available, 0.0), used, limit)
        with self.write():
            cursor = self.conn.execute(
                "INSERT INTO budget_ledger(run_id, operation_id, budget_key, consumed_units,"
                " external_access, reason, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (self.run_id, operation_id, key, value, reason, _iso(at)),
            )
            if reservation is not None:
                self.conn.execute(
                    "UPDATE budget_reservations SET consumed_at = ?"
                    " WHERE id = ? AND run_id = ? AND consumed_at IS NULL",
                    (_iso(at), int(reservation["id"]), self.run_id),
                )
        return int(cursor.lastrowid)

    def extend(self, deltas: dict[str, float], *, at: datetime | None = None, note: str | None = None) -> int:
        """追加预算只增不减：写 budget_revisions 并递增 runs.budget_revision。

        额度只落库、不改内存：`limits` 每次读数都重放 `budget_revisions`，所以
        重建 Ledger / 重启后追加照样生效（不会消失也不会翻倍），而外层事务回滚后
        内存里也不会残留一个数据库里不存在的上限。
        """
        if not deltas:
            raise ValueError("extend 必须是正数增量，不允许用追加来重置总量")
        unknown = set(deltas) - set(BUDGET_KEYS)
        if unknown:
            raise ValueError(f"未知预算键：{sorted(unknown)}")
        unconfigured = set(deltas) - set(self.base_limits)
        if unconfigured:
            raise ValueError(f"该 run 未配置预算键 {sorted(unconfigured)}：追加不能凭空造出预算")
        values: dict[str, float] = {}
        for key, delta in deltas.items():
            if float(delta) <= 0:
                raise ValueError("extend 必须是正数增量，不允许用追加来重置总量")
            values[key] = self._validated_units(key, delta, for_reserve=True)
        row = self.conn.execute("SELECT budget_revision FROM runs WHERE id = ?", (self.run_id,)).fetchone()
        if row is None:
            raise ValueError(f"run 不存在：{self.run_id}")
        added = {k: (int(v) if v == int(v) else v) for k, v in values.items()}
        with self.write():
            self.conn.execute(
                "INSERT INTO budget_revisions(run_id, added_json, created_at, note) VALUES (?, ?, ?, ?)",
                (self.run_id, json.dumps(added, sort_keys=True), _iso(at), note),
            )
            self.conn.execute(
                "UPDATE runs SET budget_revision = budget_revision + 1, updated_at = ? WHERE id = ?",
                (_iso(at), self.run_id),
            )
            self.conn.execute(
                "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'extended', ?)",
                (
                    self.run_id,
                    _iso(at),
                    json.dumps({"deltas": added, "note": note}, ensure_ascii=False),
                ),
            )
        return int(row["budget_revision"]) + 1

    def ledger_rows(self, key: str | None = None) -> list[sqlite3.Row]:
        if key is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM budget_ledger WHERE run_id = ? ORDER BY id", (self.run_id,)
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM budget_ledger WHERE run_id = ? AND budget_key = ? ORDER BY id",
                (self.run_id, key),
            )
        )

    def release_stale_reservations(self, *, at: datetime | None = None) -> list[int]:
        """崩溃恢复：收回到期没人认领的预留，但绝不为已经发生的访问退款。

        逐条按归属判断，而不是一句 `status NOT IN (...)` 全放开：
        - `pending` / `in_flight` 的操作还有 worker 会接手或正在跑，预留必须留着；
        - 没有归属操作（`operation_id IS NULL`）的预留没有事实可核对，直接放开；
        - 其余操作（含 `uncertain` 和各种终态）：只要它有过 `started_at` 的 attempt，
          就说明访问真的发生过——结果不确定不等于没发生，这些 attempt 要补记消耗，
          之后预留才算被消费掉而不是退回额度；从未发起过的才退回额度。
        放开只写 `consumed_at`，不改历史行。
        """
        rows = self.conn.execute(
            "SELECT r.*, o.status AS op_status FROM budget_reservations r"
            " LEFT JOIN operations o ON o.id = r.operation_id"
            " WHERE r.run_id = ? AND r.consumed_at IS NULL ORDER BY r.id",
            (self.run_id,),
        ).fetchall()
        closed: list[int] = []
        for row in rows:
            reservation_id = int(row["id"])
            key = str(row["budget_key"])
            operation_id = None if row["operation_id"] is None else int(row["operation_id"])
            if operation_id is None:
                self.release(reservation_id, key=key, at=at)
                closed.append(reservation_id)
                continue
            if row["op_status"] in LIVE_OPERATION_STATES:
                continue
            if self._charge_started_attempts(reservation_id, key, operation_id, at=at):
                # 已发起的访问补记账后，预留同样变成"已消费"：不退回额度
                self.conn.execute(
                    "UPDATE budget_reservations SET consumed_at = ?"
                    " WHERE id = ? AND run_id = ? AND consumed_at IS NULL",
                    (_iso(at), reservation_id, self.run_id),
                )
            else:
                self.release(reservation_id, key=key, operation_id=operation_id, at=at)
            closed.append(reservation_id)
        return closed

    def _charge_started_attempts(
        self, reservation_id: int, key: str, operation_id: int, *, at: datetime | None
    ) -> bool:
        """把该操作里"发起了却没记账"的 attempt 补成消耗；返回是否确实发生过访问。"""
        attempts = self.conn.execute(
            "SELECT attempt_no, finished_at, result_state, error_json FROM operation_attempts WHERE operation_id = ?"
            " AND started_at IS NOT NULL ORDER BY attempt_no",
            (operation_id,),
        ).fetchall()
        chargeable = []
        for attempt in attempts:
            try:
                error = json.loads(attempt["error_json"] or "{}")
            except (TypeError, ValueError):
                error = {}
            if not isinstance(error, dict):
                error = {}
            detail = error.get("detail")
            local_proof = isinstance(detail, dict) and detail.get("external_access") == "not_started"
            exception_proof = error.get("access_charged") == 0 and error.get("access_phase") in ("not_started", "access")
            if attempt["finished_at"] is not None and attempt["result_state"] == "failed" and (local_proof or exception_proof):
                continue
            chargeable.append(attempt)
        attempts = chargeable
        if not attempts:
            return False
        if key in ACCESS_KEYS:
            row = self.conn.execute(
                "SELECT operation_type FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            operation_type = OperationType(row["operation_type"])
            for attempt in attempts:
                # 补导入只对同一已完成 attempt 幂等；新的实际尝试是新的一次访问，照常计费。
                # 这里不能因超出上限而拒绝记账：既成事实必须留在账上，超限由读数暴露。
                self._record_access(
                    operation_id=operation_id,
                    operation_type=operation_type,
                    attempt_no=int(attempt["attempt_no"]),
                    key=key,
                    units=1.0,
                    reason="崩溃恢复：已发起的访问按实际次数计费",
                    at=at,
                )
            return True
        reserved = float(self.conn.execute(
            "SELECT units FROM budget_reservations WHERE id = ?", (reservation_id,)
        ).fetchone()["units"])
        self.record_quota(
            key,
            reserved,
            reason="崩溃恢复：已发起的操作按预留消耗配额",
            operation_id=operation_id,
            reservation_id=reservation_id,
            at=at,
        )
        return True


__all__ = ["BudgetExhausted", "BudgetView", "Ledger"]
