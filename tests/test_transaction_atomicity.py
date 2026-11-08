"""事务所有权与批次原子性（复核意见 2026-10-24 17:23）。

复现的两个失败必须反过来变成用例：
1. `with db.transaction(conn): repo.ensure_keyword(...); raise` 之后，keyword 不得还在；
2. 迁移不得把调用者没写完的事务顺手提交掉。
存储层只有一个写事务入口 `db.transaction()`：最外层 BEGIN/COMMIT，内层只开 SAVEPOINT。
"""

from __future__ import annotations

import pytest

from conftest import T0
from xhs_discovery.models.enums import OperationType
from xhs_discovery.storage import artifacts, db, migrations
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.migrations import SchemaError

ACCESS_LIMITS = {"suggest_attempts": 5, "search_attempts": 5, "body_attempts": 5,
                 "author_attempts": 5, "calibration_suggest_attempts": 5,
                 "max_posts_stored": 50, "max_runtime_minutes": 60, "model_calls": 0,
                 "model_tokens": 0, "model_cost_usd": 0.0}


def test_repository_writes_never_commit_the_callers_transaction(storage):
    """Codex 的复现场景：外层回滚后不得留下 keyword。"""
    with pytest.raises(RuntimeError, match="批次失败"):
        with db.transaction(storage.conn):
            storage.repo.ensure_keyword("rollback-probe", "rollback-probe")
            assert storage.conn.execute(
                "SELECT COUNT(*) AS n FROM keywords WHERE normalized_text = 'rollback-probe'"
            ).fetchone()["n"] == 1
            raise RuntimeError("批次失败")
    assert storage.conn.execute(
        "SELECT COUNT(*) AS n FROM keywords WHERE normalized_text = 'rollback-probe'"
    ).fetchone()["n"] == 0, "Repository 擅自提交会让外层回滚失效"


def test_a_multi_step_batch_rolls_back_observation_and_budget_together(storage):
    """观察、预算、事件必须同批生效或同批消失，否则崩溃恢复看到的是半截账。"""
    ledger = Ledger(storage.conn, storage.run_id, ACCESS_LIMITS)
    keyword_id = storage.repo.ensure_keyword("上海中考", "上海中考")
    operation_id = storage.repo.open_operation(storage.run_id, keyword_id, OperationType.suggest, 0)
    storage.repo.mark_operation_started(operation_id)
    attempt_id = storage.repo.start_attempt(operation_id, 1)
    with pytest.raises(RuntimeError, match="半批"):
        with db.transaction(storage.conn):
            artifacts.write_artifact(storage.conn, storage.paths, storage.run_id,
                                     kind=artifacts.KINDS[0], payload='{"ok": 1}',
                                     operation_id=operation_id, attempt_id=attempt_id,
                                     filename="sugg.json", at=T0)
            ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                                   attempt_no=1, key="suggest_attempts", reason="批次内记账", at=T0)
            storage.repo.record_event(storage.run_id, "batch_probe", detail={"n": 1}, at=T0)
            raise RuntimeError("半批")
    assert storage.conn.execute("SELECT COUNT(*) AS n FROM raw_artifacts").fetchone()["n"] == 0
    assert storage.conn.execute("SELECT COUNT(*) AS n FROM budget_ledger").fetchone()["n"] == 0
    assert storage.repo.events(storage.run_id, kind="batch_probe") == []
    assert ledger.used("suggest_attempts") == 0


def test_leftover_file_from_a_failed_batch_is_picked_up_by_reimport(storage):
    """事务回滚删不掉已落盘的文件：产物文件必须在，但库里没有引用，恢复扫描要认出来。"""
    ledger = Ledger(storage.conn, storage.run_id, ACCESS_LIMITS)
    keyword_id = storage.repo.ensure_keyword("上海中考", "上海中考")
    operation_id = storage.repo.open_operation(storage.run_id, keyword_id, OperationType.suggest, 0)
    storage.repo.mark_operation_started(operation_id)
    attempt_id = storage.repo.start_attempt(operation_id, 1)
    rel = None
    with pytest.raises(RuntimeError):
        with db.transaction(storage.conn):
            landed = artifacts.write_artifact(storage.conn, storage.paths, storage.run_id,
                                              kind="raw_response", payload="raw bytes",
                                              operation_id=operation_id, attempt_id=attempt_id,
                                              filename="body.json", at=T0)
            rel = landed.rel_path
            ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                                   attempt_no=1, key="suggest_attempts", reason="回滚掉", at=T0)
            raise RuntimeError("崩在提交前")
    assert storage.paths.resolve_within_run(rel, storage.run_id).exists(), \
        "文件写入不由事务回收，这是设计而非缺陷"
    assert storage.conn.execute("SELECT COUNT(*) AS n FROM raw_artifacts").fetchone()["n"] == 0
    assert ledger.used("suggest_attempts") == 0
    orphans = artifacts.scan_unregistered(storage.conn, storage.paths, storage.run_id)
    assert rel in orphans, "补导入必须看得见这次没登记上的文件"


def test_nested_transaction_only_rolls_back_the_inner_savepoint(storage):
    with db.transaction(storage.conn) as conn:
        conn.execute(
            "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'outer', NULL)",
            (storage.run_id, T0.isoformat()),
        )
        with pytest.raises(ValueError, match="内层失败"):
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'inner', NULL)",
                    (storage.run_id, T0.isoformat()),
                )
                raise ValueError("内层失败")
    kinds = [row["kind"] for row in storage.repo.events(storage.run_id)]
    assert "outer" in kinds and "inner" not in kinds, "SAVEPOINT 只回滚内层，不得连带丢掉外层已写的行"


def test_migrations_refuse_an_open_transaction_and_do_not_commit_it(storage):
    """结构性变更不接嵌套：既不静默提交别人的批次，也不在半途建表。"""
    with db.transaction(storage.conn) as conn:
        conn.execute(
            "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'before_migration', NULL)",
            (storage.run_id, T0.isoformat()),
        )
        with pytest.raises(SchemaError, match="已有未提交事务"):
            migrations.apply_migrations(conn)
        with pytest.raises(SchemaError, match="已有未提交事务"):
            migrations.rollback(conn, to_version=0)
        conn.rollback()
    assert storage.repo.events(storage.run_id, kind="before_migration") == []
    assert migrations.applied_versions(storage.conn) == (1, 2, 3, 4, 5, 6, 7, 8), "被拒绝的迁移不得改动版本号"
    assert storage.conn.execute("SELECT COUNT(*) AS n FROM sqlite_master WHERE name='keywords'"
                               ).fetchone()["n"] == 1, "schema 必须原样保留"


def test_a_down_migration_that_fails_leaves_the_schema_untouched(tmp_path, monkeypatch):
    """down 脚本中途失败：表还在、版本号还在，不能留下"回滚了一半"的库。"""
    conn = db.connect(tmp_path / "broken-down" / "x.db")
    assert migrations.current_version(conn) == migrations.HEAD_VERSION
    original = migrations.MIGRATIONS
    broken = migrations.Migration(
        2,
        "故意让 down 中途失败",
        original[1].up,
        "DROP TABLE problems; INSERT INTO missing_table VALUES (1);",
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (original[0], broken))
    with pytest.raises(SchemaError, match="回滚迁移 2 失败"):
        migrations.rollback(conn, to_version=1)
    # 库已在 HEAD（含 v3～v8）；被 patch 掉的迁移元组里没有它们，所以版本号必须一字未动
    assert migrations.applied_versions(conn) == (1, 2, 3, 4, 5, 6, 7, 8), "失败的 down 不得改动版本号"
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "problems" in tables, "失败的 down 必须整段回滚"
    conn.close()


def test_an_inner_failure_does_not_prevent_the_outer_commit(storage):
    """内层失败被外层接住时，外层照样能提交自己的写入（SAVEPOINT 已释放干净）。"""
    with db.transaction(storage.conn) as conn:
        try:
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'doomed', NULL)",
                    (storage.run_id, T0.isoformat()),
                )
                raise ValueError("炸掉内层")
        except ValueError:
            pass
        conn.execute(
            "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, 'survivor', NULL)",
            (storage.run_id, T0.isoformat()),
        )
    kinds = [row["kind"] for row in storage.repo.events(storage.run_id)]
    assert "survivor" in kinds and "doomed" not in kinds
    assert conn.in_transaction is False
