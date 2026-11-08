"""P0-06：迁移、约束与事务纪律。

覆盖：空库建表、v1→v2 升级保留数据、回滚只撤一层、以旧代码打开新库必须拒绝、
PRAGMA、枚举 CHECK、外键 RESTRICT、逻辑键唯一、外部访问预算的 DB 级防重复扣费，
以及迁移中途失败不留半成品 schema。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage import migrations
from xhs_discovery.storage.repository import Repository

RUN_COLUMNS_INSERT = (
    "id, seed_raw, seed_normalized, mode, status, config_json, config_hash, code_version,"
    " protocol_version, analysis_version, random_seed, synthetic_fixture, created_at, updated_at"
)


def seed_run(conn: sqlite3.Connection, run_id: str = "run-a") -> None:
    with conn:
        conn.execute(
            f"INSERT INTO runs({RUN_COLUMNS_INSERT}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "上海中考",
                "上海中考",
                "fixture",
                "queued",
                "{}",
                "a" * 64,
                "0.0.0",
                "1",
                "p0-rule-baseline.1",
                1,
                1,
                "2026-10-24T12:00:00+00:00",
                "2026-10-24T12:00:00+00:00",
            ),
        )


def make_conn(tmp_path):
    return storage_db.connect(tmp_path / "data.db")


# ------------------------------------------------------------------ 建表与版本


def test_fresh_database_builds_head_schema(tmp_path):
    conn = make_conn(tmp_path)
    assert migrations.applied_versions(conn) == (1, 2, 3, 4, 5, 6, 7, 8)
    assert migrations.current_version(conn) == migrations.HEAD_VERSION
    tables = migrations.table_names(conn)
    assert set(migrations.tables_for(1)) <= tables
    assert set(migrations.tables_for(2)) <= tables
    assert set(migrations.tables_for(4)) <= tables
    for table in ("keywords", "runs", "operations", "budget_ledger", "raw_artifacts", "worker_leases"):
        assert table in tables


def test_v1_is_the_writing_layer_and_v2_is_the_analysis_layer():
    assert {"suggestion_observations", "post_observations", "operations"} <= set(
        migrations.tables_for(1)
    )
    assert {"needs", "problems", "evidence", "cluster_assignments"} <= set(migrations.tables_for(2))
    assert not set(migrations.tables_for(1)) & set(migrations.tables_for(2))


def test_v3_adds_body_observation_provenance_without_new_tables(tmp_path):
    """C-09：v3 不新增表，只给正文观察补来源列与索引；来源列必须可空（老行留 NULL）。"""
    conn = make_conn(tmp_path)
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(body_observations)")}
    assert set(migrations.tables_for(3)) == set()
    for name in ("run_id", "operation_id", "attempt_id", "session_id"):
        assert name in columns, f"缺来源列 {name}"
        assert int(columns[name]["notnull"]) == 0, f"{name} 必须可空：历史行无法确定归属"
        assert columns[name]["dflt_value"] == "NULL", f"{name} 默认 NULL，不带入伪造来源"
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='body_observations'"
        )
    }
    assert "idx_body_observation_attempt" in indexes
    foreign = {row["table"] for row in conn.execute("PRAGMA foreign_key_list(body_observations)")}
    assert {"runs", "operations", "operation_attempts"} <= foreign


def test_v4_adds_the_analysis_facts_p0_d_needs(tmp_path):
    """P0-D 前置：卡片可见字段可空且默认 NULL，家族成员有行可审计，需求稳定键在同版本内唯一。"""
    conn = make_conn(tmp_path)
    pq = {row["name"]: row for row in conn.execute("PRAGMA table_info(post_queries)")}
    for name in ("published_at_text", "likes_text"):
        assert name in pq, f"缺卡片可见字段列 {name}"
        assert int(pq[name]["notnull"]) == 0, f"{name} 必须可空：页面没给就是没给"
        assert pq[name]["dflt_value"] == "NULL", f"{name} 默认 NULL，不伪造可见文本"
    assert set(migrations.tables_for(4)) == {"content_family_members"}
    assert "content_family_members" in migrations.table_names(conn)
    foreign = {
        row["table"] for row in conn.execute("PRAGMA foreign_key_list(content_family_members)")
    }
    assert {"content_families", "post_content_versions"} <= foreign
    assert "stable_key" in {row["name"] for row in conn.execute("PRAGMA table_info(needs)")}


def test_v4_needs_stable_key_is_unique_per_run_and_version(tmp_path):
    conn = make_conn(tmp_path)
    seed_analysis_chain(conn)
    with conn:
        conn.execute(
            "INSERT INTO needs(run_id, canonical_text, statement_kind, evidence_status,"
            " analysis_version, extractor_version, stable_key, created_at, updated_at)"
            " VALUES ('run-a', '志愿到底怎么排', 'explicit_problem', 'verified', 'v1', 'e1',"
            " 'sk-1', ?, ?)",
            (AT, AT),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO needs(run_id, canonical_text, statement_kind, evidence_status,"
                " analysis_version, extractor_version, stable_key, created_at, updated_at)"
                " VALUES ('run-a', '换个说法', 'explicit_problem', 'verified', 'v1', 'e1',"
                " 'sk-1', ?, ?)",
                (AT, AT),
            )
        # 同键跨 analysis_version 允许并存：新版本改判要写新行，历史行一字不动
        conn.execute(
            "INSERT INTO needs(run_id, canonical_text, statement_kind, evidence_status,"
            " analysis_version, extractor_version, stable_key, created_at, updated_at)"
            " VALUES ('run-a', '志愿到底怎么排', 'explicit_problem', 'verified', 'v2', 'e1',"
            " 'sk-1', ?, ?)",
            (AT, AT),
        )
        # 历史行（P0-C 的 p0-preliminary.1）stable_key 为 NULL，多条也不冲突
        for text in ("历史一", "历史二"):
            conn.execute(
                "INSERT INTO needs(run_id, canonical_text, statement_kind, evidence_status,"
                " analysis_version, extractor_version, created_at, updated_at)"
                " VALUES ('run-a', ?, 'explicit_problem', 'verified', 'p0-preliminary.1', 'e1', ?, ?)",
                (text, AT, AT),
            )


def test_v5_gives_phrases_a_version_dimension_that_the_old_unique_could_not_enforce(tmp_path):
    """批次级短语的 `source_version_id` 恒为 NULL，而 SQLite 认为 UNIQUE 里的 NULL 互不相等：
    表级约束拦不住重复写，同版本唯一性由 v5 的部分索引负责，跨版本并存是有意为之。"""
    conn = make_conn(tmp_path)
    seed_analysis_chain(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(phrases)")}
    assert "analysis_version" in columns

    def insert(text: str, kind: str, version: str | None) -> None:
        conn.execute(
            "INSERT INTO phrases(run_id, text, kind, score, freq, source_version_id,"
            " extractor_version, analysis_version) VALUES ('run-a', ?, ?, 0.5, 3, NULL, 'e1', ?)",
            (text, kind, version),
        )

    with conn:
        insert("上海中考", "word", "p0-rule-baseline.2")
        with pytest.raises(sqlite3.IntegrityError, match="phrases.analysis_version"):
            insert("上海中考", "word", "p0-rule-baseline.2")
        # 换 analysis_version 重跑：写新行，旧版本行一字不动
        insert("上海中考", "word", "p0-rule-baseline.3")
        # 同词不同 kind 是两条不同结论（频次 vs 粘合度），不互相顶掉
        insert("上海中考", "pmi_phrase", "p0-rule-baseline.2")
        # v5 之前遗留的无版本行不在这条索引的管辖范围内，允许多条并存
        insert("历史短语一", "ngram", None)
        insert("历史短语二", "ngram", None)
    versions = conn.execute(
        "SELECT COUNT(*) FROM phrases WHERE run_id = 'run-a' AND text = '上海中考'"
    ).fetchone()[0]
    assert versions == 3


def test_v8_adds_nullable_active_revisit_seq_to_frontier(tmp_path):
    """C22：frontier 需要一个可空的活动重探序号，且升级/回滚对称、历史行留 NULL 不伪造。"""
    conn = make_conn(tmp_path)
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(frontier)")}
    assert "active_revisit_seq" in columns, "fresh 库必须已带 v8 的活动重探序号列"
    assert columns["active_revisit_seq"]["notnull"] == 0, "该列必须可空：历史行没有可靠序号"
    assert columns["active_revisit_seq"]["dflt_value"] == "NULL"

    # 升级到 v6（撤掉 v7、v8）再回来：只补列，不替历史行猜序号
    assert migrations.rollback(conn, to_version=6) == (8, 7)
    assert "active_revisit_seq" not in {
        row["name"] for row in conn.execute("PRAGMA table_info(frontier)")
    }
    assert migrations.apply_migrations(conn) == (7, 8)
    assert "active_revisit_seq" in {
        row["name"] for row in conn.execute("PRAGMA table_info(frontier)")
    }
    assert migrations.current_version(conn) == migrations.HEAD_VERSION


def test_upgrade_from_v1_database_keeps_existing_rows(tmp_path):
    conn = make_conn(tmp_path)
    migrations.rollback(conn, to_version=1)
    conn.execute("INSERT INTO keywords(normalized_text, first_seen_at) VALUES (?, ?)",
                 ("上海中考", "2026-10-24T12:00:00+00:00"))
    seed_run(conn)
    conn.commit()
    assert migrations.current_version(conn) == 1

    applied = migrations.apply_migrations(conn)
    assert applied == (2, 3, 4, 5, 6, 7, 8)
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM runs WHERE id = 'run-a'").fetchone()[0] == "queued"
    assert migrations.applied_versions(conn) == (1, 2, 3, 4, 5, 6, 7, 8)


def test_rollback_drops_only_the_target_layer_then_reapplies(tmp_path):
    conn = make_conn(tmp_path)
    rolled = migrations.rollback(conn, to_version=1)
    assert rolled == (8, 7, 6, 5, 4, 3, 2)
    assert not set(migrations.tables_for(2)) & migrations.table_names(conn)
    assert not set(migrations.tables_for(4)) & migrations.table_names(conn)
    assert set(migrations.tables_for(1)) <= migrations.table_names(conn)
    assert migrations.apply_migrations(conn) == (2, 3, 4, 5, 6, 7, 8)
    assert migrations.current_version(conn) == migrations.HEAD_VERSION


def test_rollback_to_zero_and_rebuild_is_repeatable(tmp_path):
    conn = make_conn(tmp_path)
    assert migrations.rollback(conn, to_version=0) == (8, 7, 6, 5, 4, 3, 2, 1)
    assert migrations.current_version(conn) == 0
    assert migrations.apply_migrations(conn) == (1, 2, 3, 4, 5, 6, 7, 8)
    with pytest.raises(migrations.SchemaError, match="超出支持范围"):
        migrations.rollback(conn, to_version=99)


def test_old_code_refuses_a_newer_database(tmp_path):
    conn = make_conn(tmp_path)
    with conn:
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, code_version) VALUES (?, ?, ?)",
            (99, "2026-10-24T12:00:00+00:00", "future"),
        )
    with pytest.raises(migrations.SchemaError, match="高于代码支持版本"):
        migrations.apply_migrations(conn)


def test_failed_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    migrations.rollback(conn, to_version=0)
    broken = migrations.Migration(
        version=1,
        name="故意失败",
        up=(
            "CREATE TABLE keywords (id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE keyword_aliases (id INTEGER PRIMARY KEY);\n"
            "SELECT unknown_function('故意中断迁移');\n"
        ),
        down="DROP TABLE IF EXISTS keyword_aliases;\nDROP TABLE IF EXISTS keywords;\n",
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (broken,))
    with pytest.raises(migrations.SchemaError, match="迁移 1 失败"):
        migrations.apply_migrations(conn)
    assert "keywords" not in migrations.table_names(conn)
    assert "keyword_aliases" not in migrations.table_names(conn)
    assert migrations.applied_versions(conn) == ()


def test_two_statements_on_one_line_still_split_and_rollback(tmp_path, monkeypatch):
    """Codex 的复现写法就是一行两条语句：切分器必须在第一个分号处真拆开，
    否则"第一条成功第二条失败"根本发生不了，原子回滚也就没被真正测到。"""
    conn = make_conn(tmp_path)
    migrations.rollback(conn, to_version=0)
    assert migrations._iter_statements(
        "CREATE TABLE probe(id INTEGER); INSERT INTO missing VALUES (1);"
    ) == ["CREATE TABLE probe(id INTEGER);", "INSERT INTO missing VALUES (1);"]
    assert migrations._iter_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;") == [
        "INSERT INTO t VALUES ('a;b');", "SELECT 1;"]
    broken = migrations.Migration(
        version=1,
        name="一行两条、第二条失败",
        up="CREATE TABLE rollback_probe(id INTEGER); INSERT INTO missing_table VALUES (1);",
        down="DROP TABLE IF EXISTS rollback_probe;",
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (broken,))
    with pytest.raises(migrations.SchemaError, match="no such table"):
        migrations.apply_migrations(conn)
    assert "rollback_probe" not in migrations.table_names(conn), "第一条 DDL 已执行也要跟着回滚"
    assert migrations.applied_versions(conn) == ()


def test_a_failing_down_script_leaves_the_layer_intact(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    assert migrations.current_version(conn) == migrations.HEAD_VERSION
    original = migrations.MIGRATIONS
    broken = migrations.Migration(
        original[1].version,
        "down 中途失败",
        original[1].up,
        "DROP TABLE problems; INSERT INTO missing_table VALUES (1);",
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (original[0], broken))
    with pytest.raises(migrations.SchemaError, match="回滚迁移 2 失败"):
        migrations.rollback(conn, to_version=1)
    assert "problems" in migrations.table_names(conn), "失败的 down 必须整段回滚"
    assert migrations.current_version(conn) == migrations.HEAD_VERSION


# ------------------------------------------------------------------ PRAGMA 与事务


def test_pragmas_match_the_documented_discipline(tmp_path):
    conn = make_conn(tmp_path)
    assert storage_db.pragma_value(conn, "foreign_keys") == "1"
    assert storage_db.pragma_value(conn, "journal_mode") == "wal"
    assert storage_db.pragma_value(conn, "busy_timeout") == "5000"


def test_transaction_rolls_back_a_partial_write(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    with pytest.raises(RuntimeError, match="炸"):
        with storage_db.transaction(conn):
            conn.execute("INSERT INTO keywords(normalized_text, first_seen_at) VALUES (?, ?)",
                         ("半成品", "2026-10-24T12:00:00+00:00"))
            raise RuntimeError("炸")
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


# ------------------------------------------------------------------ 约束


def test_enum_checks_reject_values_outside_the_contract(tmp_path):
    conn = make_conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            f"INSERT INTO runs({RUN_COLUMNS_INSERT}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-bad", "s", "s", "fixture", "dreaming", "{}", "a" * 64, "0", "1", "v", 1, 1,
                "2026-10-24T12:00:00+00:00", "2026-10-24T12:00:00+00:00",
            ),
        )
    seed_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operations(run_id, keyword_id, operation_type, status) VALUES (?, ?, ?, ?)",
            (1, 1, "suggest", "teleported"),
        )


def test_foreign_keys_restrict_deleting_referenced_identity(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    conn.execute("INSERT INTO keywords(normalized_text, first_seen_at) VALUES (?, ?)",
                 ("上海中考", "2026-10-24T12:00:00+00:00"))
    keyword_id = conn.execute("SELECT id FROM keywords").fetchone()[0]
    conn.execute(
        "INSERT INTO run_keywords(run_id, keyword_id, depth, origin, status, first_seen_at, last_seen_at)"
        " VALUES (?, ?, 0, 'seed', 'queued', ?, ?)",
        ("run-a", keyword_id, "2026-10-24T12:00:00+00:00", "2026-10-24T12:00:00+00:00"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run_keywords(run_id, keyword_id, depth, origin, status, first_seen_at, last_seen_at)"
            " VALUES ('run-ghost', 999, 0, 'seed', 'queued', ?, ?)",
            ("2026-10-24T12:00:00+00:00", "2026-10-24T12:00:00+00:00"),
        )


def test_operations_logical_key_is_enforced_by_the_schema(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    conn.execute("INSERT INTO keywords(normalized_text, first_seen_at) VALUES (?, ?)",
                 ("上海中考", "2026-10-24T12:00:00+00:00"))
    keyword_id = conn.execute("SELECT id FROM keywords").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO operations(run_id, keyword_id, operation_type, revisit_seq, status)"
            " VALUES ('run-a', ?, 'suggest', 0, 'pending')",
            (keyword_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operations(run_id, keyword_id, operation_type, revisit_seq, status)"
                " VALUES ('run-a', ?, 'suggest', 0, 'pending')",
                (keyword_id,),
            )
        conn.execute(
            "INSERT INTO operations(run_id, keyword_id, operation_type, revisit_seq, status)"
            " VALUES ('run-a', ?, 'suggest', 1, 'pending')",
            (keyword_id,),
        )


def test_external_access_ledger_cannot_be_charged_twice_or_without_a_attempt(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    conn.execute("INSERT INTO keywords(normalized_text, first_seen_at) VALUES (?, ?)",
                 ("上海中考", "2026-10-24T12:00:00+00:00"))
    keyword_id = conn.execute("SELECT id FROM keywords").fetchone()[0]
    conn.execute(
        "INSERT INTO operations(run_id, keyword_id, operation_type, revisit_seq, status)"
        " VALUES ('run-a', ?, 'suggest', 0, 'in_flight')",
        (keyword_id,),
    )
    operation_id = conn.execute("SELECT id FROM operations").fetchone()[0]
    conn.execute(
        "INSERT INTO operation_attempts(operation_id, attempt_no, started_at, finished_at, result_state)"
        " VALUES (?, 1, ?, ?, 'completed')",
        (operation_id, "2026-10-24T12:00:00+00:00", "2026-10-24T12:00:01+00:00"),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO budget_ledger(run_id, budget_key, consumed_units, external_access, reason, created_at)"
            " VALUES ('run-a', 'suggest_attempts', 1, 1, '缺 attempt', '2026-10-24T12:00:02+00:00')"
        )
    with conn:
        conn.execute(
            "INSERT INTO budget_ledger(run_id, operation_id, budget_key, operation_type, attempt_no,"
            " consumed_units, external_access, reason, created_at)"
            " VALUES ('run-a', ?, 'suggest_attempts', 'suggest', 1, 1, 1, '一次尝试', ?)",
            (operation_id, "2026-10-24T12:00:02+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO budget_ledger(run_id, operation_id, budget_key, operation_type, attempt_no,"
                " consumed_units, external_access, reason, created_at)"
                " VALUES ('run-a', ?, 'suggest_attempts', 'suggest', 1, 1, 1, '重复补导入', ?)",
                (operation_id, "2026-10-24T12:00:03+00:00"),
            )


def test_cache_hit_may_not_consume_units_but_quota_keys_may(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO budget_ledger(run_id, budget_key, consumed_units, external_access, reason, created_at)"
            " VALUES ('run-a', 'suggest_attempts', 2, 0, '缓存命中却扣了两格', '2026-10-24T12:00:00+00:00')"
        )
    conn.execute(
        "INSERT INTO budget_ledger(run_id, budget_key, consumed_units, external_access, reason, created_at)"
        " VALUES ('run-a', 'max_posts_stored', 12, 0, '入库篇数', '2026-10-24T12:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO budget_ledger(run_id, budget_key, consumed_units, external_access, reason, created_at)"
        " VALUES ('run-a', 'suggest_attempts', 0, 0, '缓存命中', '2026-10-24T12:00:00+00:00')"
    )
    assert conn.execute("SELECT COUNT(*) FROM budget_ledger").fetchone()[0] == 2


def test_artifact_paths_are_relative_and_content_addressed(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO raw_artifacts(run_id, rel_path, sha256, size_bytes, kind, created_at)"
            " VALUES ('run-a', 'runs/run-a/a.json', ?, 1, 'stdout_json', ?)",
            ("z" * 63, "2026-10-24T12:00:00+00:00"),
        )
    digest = "b" * 64
    conn.execute(
        "INSERT INTO raw_artifacts(run_id, rel_path, sha256, size_bytes, kind, created_at)"
        " VALUES ('run-a', 'runs/run-a/a.json', ?, 1, 'stdout_json', ?)",
        (digest, "2026-10-24T12:00:00+00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO raw_artifacts(run_id, rel_path, sha256, size_bytes, kind, created_at)"
            " VALUES ('run-a', 'runs/run-a/a.json', ?, 2, 'stdout_json', ?)",
            (digest, "2026-10-24T12:00:01+00:00"),
        )


def test_repository_builds_the_schema_on_a_bare_connection(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "raw.db"))
    conn.row_factory = sqlite3.Row
    assert migrations.current_version(conn) == 0
    Repository(conn)
    assert migrations.current_version(conn) == migrations.HEAD_VERSION
    conn.close()


def test_orphan_events_are_rejected_only_when_the_connection_set_the_pragma(storage):
    """外键是逐连接设置：必须走 db.connect，裸连接能写进孤儿事件不能算实现正确。"""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        storage.repo.record_event("ghost-run", "created")
    assert storage.repo.events("ghost-run") == []


def test_config_json_column_stays_parseable(tmp_path):
    conn = make_conn(tmp_path)
    seed_run(conn)
    snapshot = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
    assert snapshot == {}


# ------------------------------------------------------------------ 分析层（v2）约束

AT = "2026-10-24T12:00:00+00:00"
SHA = "a" * 64


def seed_analysis_chain(conn: sqlite3.Connection) -> None:
    """run→keyword→operation→attempt→observation→suggestion 版本，外加一条帖子标题版本。"""
    seed_run(conn)
    with conn:
        conn.execute(
            "INSERT INTO keywords(id, normalized_text, first_seen_at) VALUES (1, '上海中考', ?)", (AT,)
        )
        conn.execute(
            "INSERT INTO operations(id, run_id, keyword_id, operation_type, status)"
            " VALUES (1, 'run-a', 1, 'suggest', 'completed')"
        )
        conn.execute(
            "INSERT INTO operation_attempts(id, operation_id, attempt_no, started_at, finished_at,"
            " result_state) VALUES (1, 1, 1, ?, ?, 'completed')",
            (AT, AT),
        )
        conn.execute(
            "INSERT INTO suggestion_observations(id, run_id, operation_id, attempt_id, session_id,"
            " keyword_id, observed_at, state, item_count, source_name, source_version, synthetic)"
            " VALUES (1, 'run-a', 1, 1, 's1', 1, ?, 'completed', 1, 'fixture', 'v1', 1)",
            (AT,),
        )
        conn.execute(
            "INSERT INTO suggestion_versions(id, observation_id, rank, text, text_sha256, captured_at)"
            " VALUES (1, 1, 1, '上海中考录取分数线', ?, ?)",
            (SHA, AT),
        )
        conn.execute(
            "INSERT INTO posts(id, note_id, access_url, first_seen_at)"
            " VALUES (1, 'note-1', 'https://example.com/note-1', ?)",
            (AT,),
        )
        conn.execute(
            "INSERT INTO post_content_versions(id, post_id, field, text, text_sha256, captured_at)"
            " VALUES (1, 1, 'title', '上海中考怎么规划', ?, ?)",
            (SHA, AT),
        )


def insert_problem(conn, *, content=None, suggestion=None, field="suggestion"):
    with conn:
        conn.execute(
            "INSERT INTO problems(run_id, canonical_text, content_version_id, suggestion_version_id,"
            " source_field, source_type, extraction_method, extractor_version,"
            " counts_as_valid_problem_cluster, created_at)"
            " VALUES ('run-a', '录取分数线什么时候公布', ?, ?, ?, 'user_help', 'rule',"
            " 'p0-rule-baseline.1', 1, ?)",
            (content, suggestion, field, AT),
        )


def insert_evidence(conn, *, content=None, suggestion=None, field="suggestion", post=None, note=None):
    with conn:
        conn.execute(
            "INSERT INTO evidence(run_id, source_field, quote, char_start, char_end,"
            " content_version_id, suggestion_version_id, container_sha256, operation_id, attempt_no,"
            " session_id, keyword_id, post_id, note_id, extractor_version, analysis_version,"
            " synthetic, created_at)"
            " VALUES ('run-a', ?, '录取分数线', 0, 5, ?, ?, ?, 1, 1, 's1', 1, ?, ?, 'rule',"
            " 'p0-rule-baseline.1', 1, ?)",
            (field, content, suggestion, SHA, post, note, AT),
        )


def test_problems_must_point_at_exactly_one_version_table(tmp_path):
    """来源版本用双 FK + 恰好一个约束：裸 source_version_id 能存幽灵 id，等于没有完整性。"""
    conn = make_conn(tmp_path)
    seed_analysis_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_problem(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_problem(conn, content=1, suggestion=1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_problem(conn, content=1, field="suggestion")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_problem(conn, suggestion=99)
    insert_problem(conn, suggestion=1)
    insert_problem(conn, content=1, field="title")
    assert conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0] == 2


def test_evidence_pairs_a_version_and_requires_post_identity_for_content_fields(tmp_path):
    conn = make_conn(tmp_path)
    seed_analysis_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_evidence(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_evidence(conn, content=1, suggestion=1)
    insert_evidence(conn, suggestion=1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_evidence(conn, content=1, field="body", post=1)
    insert_evidence(conn, content=1, field="body", post=1, note="note-1")
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 2


def test_suggestion_versions_are_rank_addressed_and_survive_identity_deletion(tmp_path):
    conn = make_conn(tmp_path)
    seed_analysis_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO suggestion_versions(id, observation_id, rank, text, text_sha256, captured_at)"
            " VALUES (2, 1, 1, '另一个词', ?, ?)",
            ("c" * 64, AT),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO suggestion_versions(id, observation_id, rank, text, text_sha256, captured_at)"
            " VALUES (2, 42, 2, '另一个词', ?, ?)",
            ("c" * 64, AT),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM suggestion_observations WHERE id = 1")


def test_analysis_checks_keep_inferences_honest(tmp_path):
    """方案构想不得自称已验证；自动合并必须留下相似度；一条问题在一个分析版本里只归一次。"""
    conn = make_conn(tmp_path)
    seed_analysis_chain(conn)
    insert_problem(conn, suggestion=1)
    columns = (
        "run_id, canonical_text, statement_kind, evidence_status, analysis_version,"
        " extractor_version, created_at, updated_at"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO needs({columns}) VALUES ('run-a', ?, 'solution_idea', 'verified',"
            " 'p0-rule-baseline.1', 'p0-rule-baseline.1', ?, ?)",
            ("去机构班", AT, AT),
        )
    conn.execute(
        f"INSERT INTO needs({columns}) VALUES ('run-a', ?, 'solution_idea', 'unverified_candidate',"
        " 'p0-rule-baseline.1', 'p0-rule-baseline.1', ?, ?)",
        ("去机构班", AT, AT),
    )
    problem_id = conn.execute("SELECT id FROM problems").fetchone()[0]
    need_id = conn.execute("SELECT id FROM needs").fetchone()[0]
    assignment = (
        "INSERT INTO cluster_assignments(run_id, problem_id, need_id, method, similarity, auto_merged,"
        " analysis_version, created_at) VALUES ('run-a', ?, ?, 'rule', ?, ?, 'p0-rule-baseline.1', ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(assignment, (problem_id, need_id, None, 1, AT))
    conn.execute(assignment, (problem_id, need_id, 0.91, 1, AT))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(assignment, (problem_id, need_id, 0.91, 0, AT))
    assert conn.execute("SELECT COUNT(*) FROM cluster_assignments").fetchone()[0] == 1
