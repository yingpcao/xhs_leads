"""P0-09：原始产物落盘、补导入与崩溃恢复。

要证明的是四件事：写盘原子、路径归属、重复导入不重复记账，
以及崩溃窗口"没有结果"必须显式成为 `uncertain`，不能被读成"确实没有"。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from xhs_discovery.models.enums import OperationType, ResultState
from xhs_discovery.storage import artifacts
from xhs_discovery.storage.ledger import Ledger

PAYLOAD = '{"suggestions": ["上海中考 志愿"]}'
DIGEST = hashlib.sha256(PAYLOAD.encode("utf-8")).hexdigest()


def opened(storage, op: OperationType = OperationType.suggest, revisit_seq: int = 0, attempt_no: int = 1):
    keyword_id = storage.repo.ensure_keyword("上海中考", "上海中考")
    operation_id = storage.repo.open_operation(storage.run_id, keyword_id, op, revisit_seq)
    storage.repo.mark_operation_started(operation_id)
    attempt_id = storage.repo.start_attempt(operation_id, attempt_no)
    return keyword_id, operation_id, attempt_id


def drop_crash_file(storage, keyword_id: int, op: OperationType, revisit_seq: int, attempt_no: int,
                    name: str = "stdout.json", payload: str = PAYLOAD) -> str:
    """模拟进程在写库前就断了：文件已经在盘上，库里什么都没有。"""
    target = storage.paths.attempt_dir(storage.run_id, keyword_id, op, revisit_seq, attempt_no) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return storage.paths.relative(target)


# ------------------------------------------------------------------ 落盘


def test_write_artifact_lands_the_file_and_registers_the_reference(storage):
    keyword_id, operation_id, attempt_id = opened(storage)
    landed = artifacts.write_artifact(
        storage.conn, storage.paths, storage.run_id,
        kind="stdout_json", payload=PAYLOAD, operation_id=operation_id, attempt_id=attempt_id,
        filename="stdout.json", at=None,
    )
    assert landed.rel_path == f"runs/run-a/operations/k{keyword_id}/suggest/r0/a1/stdout.json"
    assert landed.sha256 == DIGEST and landed.size_bytes == len(PAYLOAD.encode())
    file = storage.paths.resolve_within_run(landed.rel_path, storage.run_id)
    assert file.read_text(encoding="utf-8") == PAYLOAD
    row = storage.conn.execute("SELECT * FROM raw_artifacts WHERE id = ?", (landed.artifact_id,)).fetchone()
    assert row["operation_id"] == operation_id and row["attempt_id"] == attempt_id
    assert row["kind"] == "stdout_json" and row["run_id"] == storage.run_id
    leftovers = [p.name for p in file.parent.iterdir() if p.name != "stdout.json"]
    assert leftovers == [], "临时文件必须被原子改名吃掉，不能留在 run 目录里"


def test_rewriting_identical_content_is_idempotent_and_does_not_touch_the_file(storage):
    _, operation_id, attempt_id = opened(storage)
    first = artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                                     payload=PAYLOAD, operation_id=operation_id, attempt_id=attempt_id,
                                     filename="stdout.json")
    second = artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                                      payload=PAYLOAD, operation_id=operation_id, attempt_id=attempt_id,
                                      filename="stdout.json")
    assert second.artifact_id == first.artifact_id
    assert second.file_written is False and second.row_inserted is False
    assert storage.conn.execute("SELECT COUNT(*) AS c FROM raw_artifacts").fetchone()["c"] == 1


def test_different_content_at_the_same_path_is_refused_rather_than_overwritten(storage):
    _, operation_id, attempt_id = opened(storage)
    artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                             payload=PAYLOAD, operation_id=operation_id, attempt_id=attempt_id,
                             filename="stdout.json")
    with pytest.raises(artifacts.ArtifactConflict, match="不可复现"):
        artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                                 payload='{"suggestions": []}', operation_id=operation_id,
                                 attempt_id=attempt_id, filename="stdout.json")
    file = storage.paths.resolve_within_run(
        f"runs/run-a/operations/k1/suggest/r0/a1/stdout.json", storage.run_id)
    assert file.read_text(encoding="utf-8") == PAYLOAD


@pytest.mark.parametrize("rel", [
    "/etc/passwd",
    "C:/Windows/x.json",
    "../outside.json",
    r"..\outside.json",
    "//server/share/x.json",
    "runs/run-b/operations/k1/suggest/r0/a1/stdout.json",
])
def test_paths_outside_this_run_never_reach_the_disk_or_the_table(storage, rel):
    with pytest.raises(ValueError):
        artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                                 payload=PAYLOAD, rel_path=rel)
    assert storage.conn.execute("SELECT COUNT(*) AS c FROM raw_artifacts").fetchone()["c"] == 0


def test_a_directory_link_out_of_the_run_is_rejected_after_resolution(storage, make_dir_link):
    outside = storage.paths.root.parent / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "leak.json").write_text(PAYLOAD, encoding="utf-8")
    link = storage.paths.run_dir(storage.run_id) / "shared"
    link.parent.mkdir(parents=True, exist_ok=True)
    if make_dir_link(outside, link) is None:
        pytest.skip("本机无权创建符号链接或 junction：链接逃逸未验证")
    with pytest.raises(ValueError, match="解析后路径越出"):
        artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                                 payload=PAYLOAD, rel_path="runs/run-a/shared/leak.json")


def test_default_rel_path_requires_matching_operation_and_attempt(storage):
    keyword_id = storage.repo.ensure_keyword("上海中考", "上海中考")
    other_run_id = "run-b"
    from conftest import build_run_identity
    storage.repo.create_run(build_run_identity(other_run_id), config_json="{}")
    foreign_op = storage.repo.open_operation(other_run_id, keyword_id, OperationType.suggest, 0)
    foreign_attempt = storage.repo.start_attempt(foreign_op, 1)
    with pytest.raises(ValueError, match="不属于 run"):
        artifacts.default_rel_path(storage.conn, storage.paths, storage.run_id,
                                   operation_id=foreign_op, attempt_id=foreign_attempt,
                                   filename="stdout.json")
    with pytest.raises(ValueError, match="必须同时给出 operation_id"):
        artifacts.default_rel_path(storage.conn, storage.paths, storage.run_id,
                                   operation_id=None, attempt_id=None, filename="stdout.json")
    with pytest.raises(ValueError, match="不得含目录分隔"):
        artifacts.default_rel_path(storage.conn, storage.paths, storage.run_id,
                                   operation_id=foreign_op, attempt_id=foreign_attempt,
                                   filename="../escape.json")


# ------------------------------------------------------------------ 补导入


def test_unimported_artifacts_are_found_and_linked_by_path(storage):
    keyword_id, operation_id, attempt_id = opened(storage)
    rel = drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 1)
    assert artifacts.scan_unregistered(storage.conn, storage.paths, storage.run_id) == [rel]
    report = artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    assert [row.rel_path for row in report.imported] == [rel]
    assert report.orphaned == [] and report.conflicts == []
    row = storage.conn.execute("SELECT * FROM raw_artifacts WHERE rel_path = ?", (rel,)).fetchone()
    assert row["operation_id"] == operation_id and row["attempt_id"] == attempt_id
    assert row["sha256"] == DIGEST and row["kind"] == "stdout_json"


def test_re_running_the_import_creates_no_second_row_and_no_second_charge(storage):
    keyword_id, operation_id, _ = opened(storage)
    ledger = Ledger(storage.conn, storage.run_id, {"suggest_attempts": 5})
    rel = drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 1)
    artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                           attempt_no=1, key="suggest_attempts", reason="首次结算")
    again = artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    assert again.imported == []
    assert storage.conn.execute("SELECT COUNT(*) AS c FROM raw_artifacts").fetchone()["c"] == 1
    double = ledger.settle_external(operation_id=operation_id, operation_type=OperationType.suggest,
                                    attempt_no=1, key="suggest_attempts", reason="补导入再记一次")
    assert double is None
    assert ledger.used("suggest_attempts") == 1


def test_retries_land_in_separate_attempt_directories(storage):
    keyword_id, operation_id, _ = opened(storage)
    first = drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 1, payload="一次")
    second = drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 2, payload="重试")
    report = artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    assert {row.rel_path for row in report.imported} == {first, second}
    storage.repo.finish_attempt(int(storage.repo.attempt_by_number(operation_id, 1)["id"]),
                                result_state=ResultState.failed, error_code="PAGE_TIMEOUT")
    storage.repo.start_attempt(operation_id, 2)
    assert len(artifacts.scan_unregistered(storage.conn, storage.paths, storage.run_id)) == 0


def test_artifact_that_cannot_be_attributed_is_recorded_as_orphaned(storage):
    stray = storage.paths.run_dir(storage.run_id) / "manual-dump.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(PAYLOAD, encoding="utf-8")
    report = artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    assert report.orphaned == ["runs/run-a/manual-dump.json"]
    row = storage.conn.execute("SELECT operation_id FROM raw_artifacts").fetchone()
    assert row["operation_id"] is None
    kinds = [event["kind"] for event in storage.repo.events(storage.run_id)]
    assert "artifact_orphaned" in kinds and "imported_after_restart" in kinds


def test_imported_kind_is_inferred_from_the_name_not_guessed(storage):
    keyword_id, _, _ = opened(storage, OperationType.search)
    drop_crash_file(storage, keyword_id, OperationType.search, 0, 1, name="stderr.log", payload="超时")
    drop_crash_file(storage, keyword_id, OperationType.search, 0, 1, name="page.bin", payload="x")
    report = artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    assert {row.kind for row in report.imported} == {"stderr_log", "raw_response"}


def test_import_never_reaches_outside_the_run_even_when_the_file_moves(storage):
    keyword_id, _, _ = opened(storage)
    rel = drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 1)
    resolved = storage.paths.resolve_within_run(rel, storage.run_id)
    resolved.unlink()
    outside = storage.paths.runs_dir / "elsewhere.json"
    outside.write_text("别的 run 的产物", encoding="utf-8")
    (storage.paths.run_dir(storage.run_id) / "operations" / "k1").mkdir(parents=True, exist_ok=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.symlink_to(outside)
    report = artifacts.import_unregistered(storage.conn, storage.paths, storage.run_id)
    assert report.imported == [] and report.escaped == [rel]
    assert storage.conn.execute("SELECT COUNT(*) AS c FROM raw_artifacts").fetchone()["c"] == 0


# ------------------------------------------------------------------ 恢复


def test_recovery_backfills_the_operation_from_a_written_attempt_result(storage):
    keyword_id, operation_id, attempt_id = opened(storage)
    storage.repo.finish_attempt(attempt_id, result_state=ResultState.partial, raw_artifact_count=1)
    # 模拟"attempt 事实已写、operation 状态写丢"的崩溃窗口
    with storage.conn:
        storage.conn.execute(
            "UPDATE operations SET status = 'in_flight', last_finished_at = NULL WHERE id = ?",
            (operation_id,),
        )
    drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 1)
    report = artifacts.recover_run(storage.conn, storage.paths, storage.run_id)
    assert report.imports.imported_count == 1
    assert report.uncertain == []
    assert [row["to"] for row in report.reconciled] == ["partial"]
    assert storage.repo.get_operation(operation_id)["status"] == "partial"


def test_recovery_marks_the_blind_window_uncertain(storage):
    _, operation_id, attempt_id = opened(storage)
    report = artifacts.recover_run(storage.conn, storage.paths, storage.run_id)
    assert report.uncertain == [operation_id]
    row = storage.repo.get_operation(operation_id)
    assert row["status"] == "uncertain" and row["last_finished_at"] is not None
    attempt = storage.repo.attempt_by_number(operation_id, 1)
    assert attempt["finished_at"] is None, "没有结果就是没有结果，不假装 attempt 已经结束"
    event = [item for item in storage.repo.events(storage.run_id) if item["kind"] == "artifact_orphaned"]
    assert json.loads(event[-1]["detail_json"])["operation_id"] == operation_id


def test_finished_operations_are_not_replayed(storage):
    keyword_id, operation_id, attempt_id = opened(storage)
    storage.repo.finish_attempt(attempt_id, result_state=ResultState.completed)
    storage.repo.upsert_run_keyword(storage.run_id, keyword_id, status="explored")
    report = artifacts.recover_run(storage.conn, storage.paths, storage.run_id)
    assert report.reconciled == [] and report.uncertain == []
    assert storage.repo.get_operation(operation_id)["status"] == "completed"
    assert storage.repo.unfinished_operations(storage.run_id) == []


def test_recovery_releases_reservations_of_dead_operations(storage):
    keyword_id, operation_id, _ = opened(storage)
    ledger = Ledger(storage.conn, storage.run_id, {"suggest_attempts": 3})
    dead_reservation = ledger.reserve("suggest_attempts", 1, operation_id=operation_id)
    orphan_reservation = ledger.reserve("suggest_attempts", 1)
    other = storage.repo.ensure_keyword("名额分配", "名额分配")
    pending_op = storage.repo.open_operation(storage.run_id, other, OperationType.suggest, 0)
    kept = ledger.reserve("suggest_attempts", 1, operation_id=pending_op)

    report = artifacts.recover_run(storage.conn, storage.paths, storage.run_id, ledger=ledger)
    assert report.uncertain == [operation_id]
    assert set(report.released_reservations) == {dead_reservation, orphan_reservation}
    assert ledger.outstanding_reservations("suggest_attempts") == 1
    assert storage.conn.execute(
        "SELECT consumed_at FROM budget_reservations WHERE id = ?", (kept,)
    ).fetchone()["consumed_at"] is None


def test_recover_run_works_on_a_reopened_connection(storage):
    """真正的崩溃恢复：换一条连接（进程重启）后仍能看到未入库的产物。"""
    keyword_id, _, _ = opened(storage)
    rel = drop_crash_file(storage, keyword_id, OperationType.suggest, 0, 1)
    storage.conn.commit()
    from xhs_discovery.storage import db as storage_db
    from xhs_discovery.storage.repository import Repository

    reopened = storage_db.connect(storage.paths.db_path)
    try:
        assert Repository(reopened, migrate=False).operations(storage.run_id)
        report = artifacts.recover_run(reopened, storage.paths, storage.run_id)
        assert [row.rel_path for row in report.imports.imported] == [rel]
    finally:
        reopened.close()


def test_parse_attempt_path_only_accepts_the_documented_layout(storage):
    keyword_id, _, _ = opened(storage)
    good = storage.paths.relative(
        storage.paths.attempt_dir(storage.run_id, keyword_id, OperationType.body, 2, 3) / "stdout.json"
    )
    key = artifacts.parse_attempt_path(good, storage.paths, storage.run_id)
    assert key == artifacts.AttemptKey(keyword_id, OperationType.body, 2, 3)
    for wrong in (
        "runs/run-a/operations/k1/suggest/r0/stdout.json",
        "runs/run-a/operations/kx/suggest/r0/a1/stdout.json",
        "runs/run-a/operations/k1/teleport/r0/a1/stdout.json",
        "runs/run-a/manual.json",
        "runs/run-b/operations/k1/suggest/r0/a1/stdout.json",
    ):
        assert artifacts.parse_attempt_path(wrong, storage.paths, storage.run_id) is None


def test_write_artifact_rejects_unknown_kind_and_naive_timestamps(storage):
    _, operation_id, attempt_id = opened(storage)
    with pytest.raises(ValueError, match="非法产物类别"):
        artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="mystery",
                                 payload=PAYLOAD, operation_id=operation_id, attempt_id=attempt_id,
                                 filename="stdout.json")
    with pytest.raises(ValueError, match="必须带时区"):
        artifacts.write_artifact(storage.conn, storage.paths, storage.run_id, kind="stdout_json",
                                 payload=PAYLOAD, operation_id=operation_id, attempt_id=attempt_id,
                                 filename="stdout.json", at=datetime(2026, 10, 24))
    assert storage.conn.execute("SELECT COUNT(*) AS c FROM raw_artifacts").fetchone()["c"] == 0


def test_write_artifact_needs_an_existing_run(storage):
    with pytest.raises(ValueError, match="run 不存在"):
        artifacts.write_artifact(storage.conn, storage.paths, "run-ghost", kind="stdout_json",
                                 payload=PAYLOAD, rel_path="runs/run-ghost/x.json")
