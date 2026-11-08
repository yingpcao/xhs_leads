"""P0-08：预算账本 —— 预留、结算、非访问配额与"绝不重复扣费"。

账本要守住的不是算术，而是三条口径：
一次实际发起的外部尝试（含失败重试）都占预算；缓存命中只记事实不占额度；
补导入可以重复访问网络，但不能重复记账。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from xhs_discovery.config import load_settings
from xhs_discovery.models.enums import OperationState, OperationType, ResultState
from xhs_discovery.models.run import BudgetLedgerEntry
from xhs_discovery.storage import db as db_module
from xhs_discovery.storage.ledger import BudgetExhausted, Ledger
from conftest import T0, build_run_identity

CONFIG = Path(__file__).resolve().parents[1] / "config" / "tests-convergence-tiny.yaml"


def make_ledger(storage, *, suggest=3, search=2, body=0, posts=10, minutes=30,
                cost=0.0, calibration=1) -> Ledger:
    return Ledger(
        storage.conn,
        storage.run_id,
        {
            "suggest_attempts": suggest,
            "search_attempts": search,
            "body_attempts": body,
            "author_attempts": 0,
            "calibration_suggest_attempts": calibration,
            "max_posts_stored": posts,
            "max_runtime_minutes": minutes,
            "model_calls": 0,
            "model_tokens": 0,
            "model_cost_usd": cost,
        },
    )


def prepared(storage, op: OperationType = OperationType.suggest, attempt_no: int = 1):
    """建一条真实的 operation + attempt，账本引用它们才有外键可依。"""
    keyword_id = storage.repo.ensure_keyword("上海中考", "上海中考")
    operation_id = storage.repo.open_operation(storage.run_id, keyword_id, op, 0)
    storage.repo.mark_operation_started(operation_id)
    attempt_id = storage.repo.start_attempt(operation_id, attempt_no)
    return operation_id, attempt_id


# ------------------------------------------------------------------ 视图与预留


def test_from_settings_uses_the_configured_limits_without_implied_infinity(storage):
    settings = load_settings(CONFIG)
    ledger = Ledger.from_settings(storage.conn, storage.run_id, settings)
    assert ledger.limits["suggest_attempts"] == float(settings.budget.suggest_attempts)
    assert ledger.view("max_runtime_minutes").limit == float(settings.budget.max_runtime_minutes)
    partial = Ledger(storage.conn, storage.run_id, {"suggest_attempts": 1})
    with pytest.raises(ValueError, match="未配置预算键"):
        partial.view("author_attempts")
    assert partial.status().keys() == {"suggest_attempts"}
    with pytest.raises(ValueError, match="未知预算键"):
        Ledger(storage.conn, storage.run_id, {"teleport_attempts": 5})


def test_reservation_blocks_further_attempts_before_any_access(storage):
    ledger = make_ledger(storage, suggest=2)
    ledger.reserve("suggest_attempts", 1)
    ledger.reserve("suggest_attempts", 1)
    assert ledger.view("suggest_attempts").remaining == 0
    assert ledger.used("suggest_attempts") == 0, "预留还没变成事实消耗"
    with pytest.raises(BudgetExhausted, match="剩余 0"):
        ledger.reserve("suggest_attempts", 1)
    with pytest.raises(ValueError, match="至少 1 个单位"):
        ledger.reserve("suggest_attempts", 0)


def test_release_returns_the_quota_without_editing_history(storage):
    ledger = make_ledger(storage, suggest=1)
    reservation = ledger.reserve("suggest_attempts", 1)
    assert ledger.view("suggest_attempts").remaining == 0
    # 只给 id 就放开 = 可能误伤别的 run / 别的操作的预留：必须声明归属
    with pytest.raises(ValueError, match="不接受只给 id"):
        ledger.release(reservation, at=T0)
    with pytest.raises(ValueError, match="记的是"):
        ledger.release(reservation, key="search_attempts", at=T0)
    ledger.release(reservation, key="suggest_attempts", at=T0)
    assert ledger.view("suggest_attempts").remaining == 1
    rows = storage.conn.execute("SELECT consumed_at FROM budget_reservations WHERE id = ?",
                                (reservation,)).fetchone()
    assert rows["consumed_at"] is not None
    with pytest.raises(ValueError, match="已结算或已释放"):
        ledger.release(reservation, key="suggest_attempts", at=T0)


# ------------------------------------------------------------------ 外部访问结算


def test_a_real_attempt_is_charged_once_even_when_imported_twice(storage):
    ledger = make_ledger(storage, suggest=1)
    operation_id, _ = prepared(storage)
    ledger.reserve("suggest_attempts", 1, operation_id=operation_id)
    row_id = ledger.settle_external(
        operation_id=operation_id, operation_type=OperationType.suggest, attempt_no=1,
        key="suggest_attempts", reason="一次真实联想访问",
    )
    assert row_id is not None
    assert ledger.used("suggest_attempts") == 1
    assert ledger.view("suggest_attempts").remaining == 0
    assert ledger.settle_external(
        operation_id=operation_id, operation_type=OperationType.suggest, attempt_no=1,
        key="suggest_attempts", reason="重启后补导入",
    ) is None
    assert len(ledger.ledger_rows("suggest_attempts")) == 1


def test_failed_and_retried_attempts_still_cost(storage):
    ledger = make_ledger(storage, search=3)
    operation_id, attempt_1 = prepared(storage, OperationType.search)
    storage.repo.finish_attempt(attempt_1, result_state=ResultState.failed,
                                error_code="PAGE_TIMEOUT", exit_code=5)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.search,
                           attempt_no=1, key="search_attempts", reason="失败也占额度")
    attempt_2 = storage.repo.start_attempt(operation_id, 2)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.search,
                           attempt_no=2, key="search_attempts", reason="重试是新一次访问")
    assert ledger.used("search_attempts") == 2
    assert storage.repo.attempt_by_number(operation_id, 1)["result_state"] == "failed"
    external = [row for row in ledger.ledger_rows() if row["external_access"] == 1]
    assert [row["attempt_no"] for row in external] == [1, 2]


def test_settlement_consumes_the_reservation(storage):
    ledger = make_ledger(storage, suggest=1)
    operation_id, _ = prepared(storage)
    reservation = ledger.reserve("suggest_attempts", 1, operation_id=operation_id)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="结算",
                           reservation_id=reservation)
    assert ledger.outstanding_reservations("suggest_attempts") == 0
    assert ledger.view("suggest_attempts").remaining == 0


def test_settlement_rejects_a_non_access_key(storage):
    ledger = make_ledger(storage)
    operation_id, _ = prepared(storage)
    with pytest.raises(ValueError, match="不是外部访问预算键"):
        ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                               attempt_no=1, key="max_posts_stored", reason="用错键")


def test_budget_contract_mirrors_the_two_kind_rule():
    """契约层与 schema 层同一条口径：外部访问必带 attempt，配额消耗不算外部访问。"""
    entry = BudgetLedgerEntry(
        run_id="run-a", budget_key="max_posts_stored", consumed_units=4, external_access=False,
        reason="入库 4 篇",
    )
    assert entry.consumed_units == 4
    with pytest.raises(ValueError, match="缓存读取不占外部访问预算"):
        BudgetLedgerEntry(run_id="run-a", budget_key="suggest_attempts", consumed_units=1,
                          external_access=False, reason="缓存却扣了额度")
    with pytest.raises(ValueError, match="不是外部访问预算键"):
        BudgetLedgerEntry(run_id="run-a", budget_key="max_runtime_minutes", consumed_units=1,
                          external_access=True, operation_type=OperationType.suggest, attempt_no=1,
                          reason="把分钟当访问次数")


def test_ledger_rows_satisfy_the_pydantic_budget_contract(storage):
    ledger = make_ledger(storage, suggest=1, cost=1.0)
    operation_id, _ = prepared(storage)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="外部访问")
    ledger.record_cache_hit(key="suggest_attempts", reason="同日缓存")
    ledger.record_quota("max_posts_stored", 3, reason="入库 3 篇")
    for row in ledger.ledger_rows():
        entry = BudgetLedgerEntry(
            run_id=row["run_id"],
            budget_key=row["budget_key"],
            operation_type=row["operation_type"],
            attempt_no=row["attempt_no"],
            consumed_units=row["consumed_units"],
            external_access=bool(row["external_access"]),
            reason=row["reason"],
        )
        assert entry.external_access == (row["external_access"] == 1)


# ------------------------------------------------------------------ 缓存与非访问配额


def test_cache_hit_is_recorded_but_costs_nothing(storage):
    ledger = make_ledger(storage, suggest=2)
    ledger.record_cache_hit(key="suggest_attempts", reason="读本地缓存", at=T0)
    assert ledger.used("suggest_attempts") == 0
    assert ledger.view("suggest_attempts").remaining == 2
    row = ledger.ledger_rows("suggest_attempts")[0]
    assert row["external_access"] == 0 and row["consumed_units"] == 0
    with pytest.raises(ValueError, match="不是外部访问预算键"):
        ledger.record_cache_hit(key="max_posts_stored", reason="用错键")


def test_non_access_rows_stay_attributable_to_an_operation(storage):
    """缓存命中与配额消耗都产生于某次操作：账本要能回答"这 2 篇是谁存进来的"。"""
    ledger = make_ledger(storage, posts=5)
    operation_id, _ = prepared(storage)
    ledger.record_cache_hit(key="suggest_attempts", reason="读本地缓存", operation_id=operation_id)
    ledger.record_quota("max_posts_stored", 2, reason="入库 2 篇", operation_id=operation_id)
    quota = ledger.ledger_rows("max_posts_stored")[0]
    assert quota["operation_id"] == operation_id and quota["external_access"] == 0
    assert quota["consumed_units"] == 2
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with storage.conn:
            storage.conn.execute(
                "INSERT INTO budget_ledger(run_id, operation_id, budget_key, consumed_units,"
                " external_access, reason, created_at) VALUES (?, 9999, 'max_posts_stored', 1, 0, ?, ?)",
                (storage.run_id, "幽灵操作", T0.isoformat()),
            )


def test_storage_and_runtime_quotas_are_metered_separately(storage):
    ledger = make_ledger(storage, posts=5, minutes=2)
    ledger.record_quota("max_posts_stored", 4, reason="入库 4 篇")
    assert ledger.can_afford("max_posts_stored", 1)
    with pytest.raises(BudgetExhausted):
        ledger.record_quota("max_posts_stored", 2, reason="超了")
    ledger.record_quota("max_runtime_minutes", 2, reason="跑了 2 分钟")
    assert ledger.view("max_runtime_minutes").exhausted
    with pytest.raises(ValueError, match="不得为负"):
        ledger.record_quota("max_posts_stored", -1, reason="退款")


def test_model_cost_is_rounded_and_zero_is_not_faked_when_price_unknown(storage):
    ledger = make_ledger(storage, cost=0.02)
    row_id = ledger.record_quota("model_cost_usd", 0.0100004, reason="一次调用")
    row = storage.conn.execute("SELECT consumed_units FROM budget_ledger WHERE id = ?",
                               (row_id,)).fetchone()
    assert row["consumed_units"] == pytest.approx(0.01)
    # NUMERIC 列必须原样存回小数：整型声明会把 0.01 悄悄变成 0，账单就失真了
    assert isinstance(row["consumed_units"], float)
    with pytest.raises(BudgetExhausted):
        ledger.record_quota("model_cost_usd", 0.05, reason="超单价")


# ------------------------------------------------------------------ extend 与恢复


def test_extend_adds_quota_without_resetting_usage_and_bumps_revision(storage):
    ledger = make_ledger(storage, suggest=1)
    operation_id, _ = prepared(storage)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="用满")
    assert ledger.view("suggest_attempts").exhausted
    revision = ledger.extend({"suggest_attempts": 4}, at=T0, note="用户追加")
    assert revision == 2
    assert storage.repo.require_run(storage.run_id)["budget_revision"] == 2
    assert ledger.view("suggest_attempts").limit == 5
    assert ledger.view("suggest_attempts").used == 1
    assert not ledger.view("suggest_attempts").exhausted
    event = storage.repo.events(storage.run_id, kind="extended")[0]
    assert json.loads(event["detail_json"])["deltas"] == {"suggest_attempts": 4}
    with pytest.raises(ValueError, match="正数增量"):
        ledger.extend({"suggest_attempts": 0})
    with pytest.raises(ValueError, match="正数增量"):
        ledger.extend({"suggest_attempts": -3})
    with pytest.raises(ValueError, match="未知预算键"):
        ledger.extend({"teleport_attempts": 5})


def test_stale_reservations_are_released_but_live_work_is_kept(storage):
    ledger = make_ledger(storage, suggest=3)
    live_op, _ = prepared(storage, OperationType.suggest)
    dead_keyword = storage.repo.ensure_keyword("名额分配", "名额分配")
    dead_op = storage.repo.open_operation(storage.run_id, dead_keyword, OperationType.suggest, 0)
    storage.repo.set_operation_status(dead_op, OperationState.uncertain)
    pending_keyword = storage.repo.ensure_keyword("特色班", "特色班")
    pending_op = storage.repo.open_operation(storage.run_id, pending_keyword, OperationType.suggest, 0)

    ledger.reserve("suggest_attempts", 1, operation_id=live_op)
    ledger.reserve("suggest_attempts", 1, operation_id=dead_op)
    ledger.reserve("suggest_attempts", 1, operation_id=pending_op)
    released = ledger.release_stale_reservations(at=T0)
    assert len(released) == 1
    assert ledger.outstanding_reservations("suggest_attempts") == 2


def test_budget_ledger_is_isolated_per_run(storage):
    ledger = make_ledger(storage, suggest=2)
    operation_id, _ = prepared(storage)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="run-a 的访问")
    other = Ledger(storage.conn, "run-b", {"suggest_attempts": 2})
    assert other.used("suggest_attempts") == 0
    assert ledger.used("suggest_attempts") == 1


# ---------------------------------------------------------- 预留归属、精度与恢复（复核意见 17:28）


def test_settlement_refuses_someone_elses_reservation(storage):
    """预留只对"它当初圈住的那次操作"有效：跨操作、跨单位、跨 run 都不能拿来结算。"""
    ledger = make_ledger(storage, suggest=4, search=4)
    op_a, _ = prepared(storage, OperationType.suggest)
    op_b, _ = prepared(storage, OperationType.search, attempt_no=1)
    mine = ledger.reserve("suggest_attempts", 1, operation_id=op_a)
    with pytest.raises(ValueError, match="属于操作"):
        ledger.settle_external(operation_id=op_b, operation_type=OperationType.search,
                               attempt_no=1, key="suggest_attempts", reason="拿别人的预留抵账",
                               reservation_id=mine)
    with pytest.raises(ValueError, match="记的是"):
        ledger.settle_external(operation_id=op_a, operation_type=OperationType.suggest,
                               attempt_no=1, key="search_attempts", reason="换了键",
                               reservation_id=mine)
    other = Ledger(storage.conn, "run-b", {"suggest_attempts": 4})
    with pytest.raises(ValueError, match="属于 run run-a"):
        other.release(mine, key="suggest_attempts")


def test_reservation_units_have_to_match_the_settlement(storage):
    ledger = make_ledger(storage, suggest=5)
    operation_id, _ = prepared(storage)
    reservation = ledger.reserve("suggest_attempts", 2, operation_id=operation_id)
    with pytest.raises(ValueError, match="预留 1 是 2 单位"):
        ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                               attempt_no=1, key="suggest_attempts", units=1, reason="少记一次",
                               reservation_id=reservation)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", units=2, reason="如实记两次",
                           reservation_id=reservation)
    assert ledger.used("suggest_attempts") == 2
    assert ledger.outstanding_reservations("suggest_attempts") == 0


def test_settlement_without_a_reservation_cannot_outrun_the_budget(storage):
    """不 reserve 直接 settle 也不能变成无限超支的后门。"""
    ledger = make_ledger(storage, suggest=1)
    operation_id, _ = prepared(storage)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="第一次")
    # 重试是真的又发起了一次访问（有 attempt 行），额度已满时照样挡下来
    storage.repo.start_attempt(operation_id, 2)
    with pytest.raises(BudgetExhausted, match="剩余 0"):
        ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                               attempt_no=2, key="suggest_attempts", reason="绕开预留")
    assert ledger.used("suggest_attempts") == 1


def test_time_and_cost_can_be_reserved_in_fractional_units(storage):
    ledger = make_ledger(storage, minutes=2, cost=1.0, posts=5)
    minutes = ledger.reserve("max_runtime_minutes", 0.4)
    cost = ledger.reserve("model_cost_usd", 0.05)
    assert ledger.view("max_runtime_minutes").remaining == 1.6
    assert ledger.view("model_cost_usd").remaining == 0.95
    with pytest.raises(ValueError, match="按整数计量"):
        ledger.reserve("max_posts_stored", 1.5)
    with pytest.raises(ValueError, match="正数"):
        ledger.reserve("max_runtime_minutes", 0)
    ledger.record_quota("max_runtime_minutes", 0.4, reason="跑了 24 秒", reservation_id=minutes)
    ledger.record_quota("model_cost_usd", 0.05, reason="一次调用", reservation_id=cost)
    assert ledger.outstanding_reservations("max_runtime_minutes") == 0
    assert ledger.used("model_cost_usd") == pytest.approx(0.05)


def test_quota_accumulates_fractionally_instead_of_truncating_to_zero(storage):
    """三次 0.4 分钟绝不能记成 0：截断会让短任务永远不消耗运行时长预算。"""
    ledger = make_ledger(storage, minutes=2)
    for _ in range(3):
        ledger.record_quota("max_runtime_minutes", 0.4, reason="24 秒")
    assert ledger.used("max_runtime_minutes") == pytest.approx(1.2)
    assert ledger.view("max_runtime_minutes").remaining == pytest.approx(0.8)


def test_outstanding_reservations_are_not_double_spent(storage):
    ledger = make_ledger(storage, minutes=1)
    ledger.reserve("max_runtime_minutes", 0.8)
    with pytest.raises(BudgetExhausted):
        ledger.record_quota("max_runtime_minutes", 0.5, reason="预留还没消费")


def test_extend_is_replayed_from_the_database_not_from_memory(storage):
    """追加额度只认库里的 revision：重启后不消失，也不重复加倍，回滚后内存不留幻觉。"""
    ledger = make_ledger(storage, suggest=1)
    assert ledger.extend({"suggest_attempts": 3}, at=T0) == 2
    rebuilt = Ledger(storage.conn, storage.run_id, {"suggest_attempts": 1})
    assert rebuilt.view("suggest_attempts").limit == 4.0, "重启后追加的额度必须在"
    assert ledger.view("suggest_attempts").limit == 4.0, "同一份库不能算出两个上限"
    with pytest.raises(RuntimeError, match="外层批次失败"):
        with db_module.transaction(storage.conn):
            ledger.extend({"suggest_attempts": 10}, at=T0)
            assert ledger.view("suggest_attempts").limit == 14.0
            raise RuntimeError("外层批次失败")
    assert ledger.view("suggest_attempts").limit == 4.0, "回滚后不得留下只存在于内存的上限"
    assert storage.repo.require_run(storage.run_id)["budget_revision"] == 2
    assert len(storage.conn.execute(
        "SELECT id FROM budget_revisions WHERE run_id = ?", (storage.run_id,)).fetchall()) == 1


def test_extend_cannot_create_budget_for_an_unconfigured_key(storage):
    ledger = Ledger(storage.conn, storage.run_id, {"suggest_attempts": 1})
    with pytest.raises(ValueError, match="未配置预算键"):
        ledger.extend({"author_attempts": 5})


def test_recovery_charges_started_attempts_instead_of_refunding_them(storage):
    """崩溃恢复不等于"访问没发生"：uncertain 操作若已发起 attempt，要补记消耗再收预留。"""
    ledger = make_ledger(storage, suggest=2)
    operation_id, _ = prepared(storage)  # in_flight + 已 start_attempt
    reservation = ledger.reserve("suggest_attempts", 1, operation_id=operation_id)
    storage.repo.set_operation_status(operation_id, OperationState.uncertain)
    released = ledger.release_stale_reservations(at=T0)
    assert released == [reservation]
    assert ledger.used("suggest_attempts") == 1, "已发起的访问不能因为结果不确定就退款"
    assert ledger.outstanding_reservations("suggest_attempts") == 0
    assert ledger.view("suggest_attempts").remaining == 1
    rows = ledger.ledger_rows("suggest_attempts")
    assert rows[0]["attempt_no"] == 1 and "崩溃恢复" in rows[0]["reason"]
    # 恢复后重新发起的尝试是新的一次访问：新 attempt_no、照常计费，绝不是"免费重跑"
    storage.repo.start_attempt(operation_id, 2)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=2, key="suggest_attempts", reason="恢复后新的真实访问", at=T0)
    assert ledger.used("suggest_attempts") == 2
    assert ledger.view("suggest_attempts").exhausted


def test_settlement_claims_its_own_reservation_even_without_the_id(storage):
    """忘了传 reservation_id 也要认领本操作同键同额的预留，否则幽灵预留永久占额度。"""
    ledger = make_ledger(storage, suggest=1)
    operation_id, _ = prepared(storage)
    ledger.reserve("suggest_attempts", 1, operation_id=operation_id)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="结算没带预留 id")
    assert ledger.outstanding_reservations("suggest_attempts") == 0
    assert ledger.used("suggest_attempts") == 1


def test_an_unclaimed_reservation_still_blocks_other_spending(storage):
    """预留没被消费就等于额度还在占用：别的结算不能把它当不存在。"""
    ledger = make_ledger(storage, suggest=2)
    operation_id, _ = prepared(storage)
    ledger.reserve("suggest_attempts", 2, operation_id=operation_id)
    with pytest.raises(BudgetExhausted, match="剩余 0"):
        ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                               attempt_no=1, key="suggest_attempts", units=1, reason="金额对不上")
    assert ledger.used("suggest_attempts") == 0


# ------------------------------------------------- 结算归属与冲突重放（复核意见 20:57 C-07）


def foreign_operation(storage, op: OperationType = OperationType.suggest, attempt_no: int = 1) -> int:
    """在同一条连接上另起一个 run 的操作：账本引用别的 run 的操作就是引用别人的额度。"""
    storage.repo.create_run(build_run_identity("run-b"), config_json='{"budget": {}}')
    keyword_id = storage.repo.ensure_keyword("名额分配", "名额分配")
    operation_id = storage.repo.open_operation("run-b", keyword_id, op, 0)
    storage.repo.mark_operation_started(operation_id)
    storage.repo.start_attempt(operation_id, attempt_no)
    return operation_id


def snapshot(conn: sqlite3.Connection) -> dict:
    """账本、预留、配额的当前状态：任何拒绝都必须让这份快照一字不差。"""
    return {
        "ledger": [tuple(row) for row in conn.execute(
            "SELECT run_id, operation_id, budget_key, operation_type, attempt_no, consumed_units,"
            " external_access FROM budget_ledger ORDER BY id")],
        "reservations": [tuple(row) for row in conn.execute(
            "SELECT run_id, budget_key, units, operation_id, operation_type, consumed_at"
            " FROM budget_reservations ORDER BY id")],
        "revisions": conn.execute("SELECT COUNT(*) FROM budget_revisions").fetchone()[0],
    }


def assert_rejected_without_trace(storage, call, match: str) -> None:
    """拒绝要"干净"：没写账、没动预留、没动额度，报的错还是那句。"""
    before = snapshot(storage.conn)
    with pytest.raises(ValueError, match=match):
        call()
    assert snapshot(storage.conn) == before, "校验失败不得留下任何账本或预留副作用"


def test_an_operation_from_another_run_can_never_be_settled_here(storage):
    """Codex 的复现场景：run-a 的账本不能把 run-b 的 suggest 访问记成自己的 search。"""
    ledger = make_ledger(storage, suggest=2, search=2)
    foreign = foreign_operation(storage, OperationType.suggest)
    assert_rejected_without_trace(
        storage,
        lambda: ledger.settle_external(operation_id=foreign, operation_type=OperationType.search,
                                       attempt_no=1, key="search_attempts", reason="probe"),
        "属于 run run-b",
    )
    assert ledger.used("search_attempts") == 0 and ledger.used("suggest_attempts") == 0


def test_settlement_cannot_rewrite_what_kind_of_operation_ran(storage):
    """类型以库里的登记为准：调用方说成别的类型就拒绝，两本账不能同时被改。"""
    ledger = make_ledger(storage, suggest=2, search=2)
    operation_id, _ = prepared(storage, OperationType.suggest)
    assert_rejected_without_trace(
        storage,
        lambda: ledger.settle_external(operation_id=operation_id, operation_type=OperationType.search,
                                       attempt_no=1, key="search_attempts", reason="换了类型"),
        "类型是 suggest，结算却说是 search",
    )


def test_a_budget_key_must_belong_to_the_operation_type_being_charged(storage):
    """suggest 的访问只能花 suggest/calibration 桶：记到 search_attempts 会两头失真。"""
    ledger = make_ledger(storage, suggest=2, search=2)
    operation_id, _ = prepared(storage, OperationType.search)
    assert_rejected_without_trace(
        storage,
        lambda: ledger.settle_external(operation_id=operation_id, operation_type=OperationType.search,
                                       attempt_no=1, key="suggest_attempts", reason="换了桶"),
        "不属于操作类型 search",
    )
    # 同一条规则也管预留：没发起过的额度圈占同样不许跨类型
    assert_rejected_without_trace(
        storage,
        lambda: ledger.reserve("suggest_attempts", 1, operation_id=operation_id),
        "不属于操作类型 search",
    )


def test_suggest_work_may_spend_the_calibration_bucket(storage):
    """校准用的 suggest 访问仍是 suggest 类型：它在独立预算里花，合法。"""
    ledger = make_ledger(storage, suggest=1, calibration=2)
    operation_id, _ = prepared(storage, OperationType.suggest)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="calibration_suggest_attempts", reason="覆盖校准")
    assert ledger.used("calibration_suggest_attempts") == 1
    assert ledger.used("suggest_attempts") == 0


def test_an_attempt_that_never_started_cannot_be_billed(storage):
    """账本对应"真发起的那一次"：没登记的 attempt_no 一律拒绝，包括带预留的结算。"""
    ledger = make_ledger(storage, suggest=3)
    operation_id, _ = prepared(storage, OperationType.suggest)
    reservation = ledger.reserve("suggest_attempts", 1, operation_id=operation_id)
    assert_rejected_without_trace(
        storage,
        lambda: ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                                       attempt_no=9, key="suggest_attempts", reason="凭空的 attempt",
                                       reservation_id=reservation),
        "没有 attempt 9",
    )
    # 预留没被误消费：那次真实访问照样能用它结算
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="如实结算",
                           reservation_id=reservation)
    assert ledger.used("suggest_attempts") == 1
    assert ledger.outstanding_reservations("suggest_attempts") == 0


def test_a_replay_that_disagrees_with_the_original_entry_is_rejected(storage):
    """补导入只对"同一件事"幂等：换单位、换键、换类型的重放是冲突，不是重复。"""
    ledger = make_ledger(storage, suggest=5, search=5)
    operation_id, _ = prepared(storage, OperationType.suggest)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="首次结算")
    assert_rejected_without_trace(
        storage,
        lambda: ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                                       attempt_no=1, key="suggest_attempts", units=2, reason="改成两次"),
        "重放参数与既成事实冲突",
    )
    assert ledger.used("suggest_attempts") == 1
    assert len(ledger.ledger_rows("suggest_attempts")) == 1
    # 完全一致的重放仍然幂等返回 None（补导入可以重复扫描，但绝不重复计费）
    assert ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                                  attempt_no=1, key="suggest_attempts", reason="重启后补导入") is None
    assert ledger.used("suggest_attempts") == 1


def test_non_access_entries_check_the_operation_too(storage):
    """缓存命中与配额消耗也要能回答"这是本 run 哪个操作产生的"。"""
    ledger = make_ledger(storage, suggest=2, posts=5)
    foreign = foreign_operation(storage, OperationType.suggest)
    assert_rejected_without_trace(
        storage,
        lambda: ledger.record_cache_hit(key="suggest_attempts", reason="别的 run 的缓存",
                                        operation_id=foreign),
        "属于 run run-b",
    )
    assert_rejected_without_trace(
        storage,
        lambda: ledger.record_quota("max_posts_stored", 1, reason="别的 run 的入库",
                                    operation_id=foreign),
        "属于 run run-b",
    )
    assert_rejected_without_trace(
        storage,
        lambda: ledger.record_cache_hit(key="search_attempts", reason="类型对不上",
                                        operation_id=prepared(storage, OperationType.suggest)[0]),
        "不属于操作类型 suggest",
    )
    assert ledger.ledger_rows() == []
