"""P1-B-RECOVERY-IDENTITY-r1：恢复路径的来源/归属身份核验（离线：本机假 CLI + 临时库）。

被核验的纪律（`_load_landed_normalized` / `_landed_integrity_reason`）：
- 登记的 attempt 行必须存在且属于本操作；`context.attempt_no` 必须等于登记的尝试序号；
- 规范化信封的落盘路径必须逐字等于本 run/操作/重探/尝试的规范目录（挪进别的 a* 目录即拒）；
- `session.source_name/source_version` 必须与**按固化快照重建的数据源**口径一致
  （核验基准来自库内登记与数据源实例，绝不采信产物自述，也不在引擎里写死来源字面量）。

坏身份一律：标 uncertain + 停在 paused/fault、保留证据与历史、零观察、零新增业务写入、
不再起子进程、不二次扣费。好身份的离线补入库（C22）必须照常可用。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.test_live_cli_pipeline import LiveProject, SEED, happy_steps
from xhs_discovery.discovery import engine
from xhs_discovery.models.common import SessionRef, utc_now
from xhs_discovery.models.enums import OperationState, PauseReason, ResultState, RunStatus
from xhs_discovery.sources import cli as sources_cli
from xhs_discovery.storage import db as storage_db


class _Crash(Exception):
    """代表"进程被硬中断"的注入异常：只在崩溃窗口抛出，不伪装成平台返回。"""


def _crash_first_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """首槽"规范化信封已落盘、入库前"崩溃；恢复驱动时计数已过阈值，自动走真实入库。"""
    original = engine.ingest_suggestion
    state = {"n": 0}

    def wrapped(*args, **kwargs):
        state["n"] += 1
        if state["n"] <= 1:
            raise _Crash("normalized result landed but ingest interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "ingest_suggestion", wrapped)


def _crashed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveProject:
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=2, search_attempts=1)
    _crash_first_ingest(monkeypatch)
    with pytest.raises(_Crash):
        project.execute()
    project.run_id = str(project.query("SELECT id FROM runs")[0]["id"])
    assert project.call_count() == 1
    return project


def _landed_envelope(project: LiveProject) -> tuple[dict, Path]:
    row = project.query(
        "SELECT * FROM raw_artifacts WHERE run_id = ? AND rel_path LIKE '%/suggest.json'"
        " AND rel_path NOT LIKE '%/cli/%'",
        (project.run_id,),
    )[0]
    path = project.paths.resolve_within_run(row["rel_path"], project.run_id)
    return row, path


def _rewrite_document(project: LiveProject, **updates) -> None:
    """按 `updates`（如 {"context": {"attempt_no": 999}}）改写落盘信封并同步登记哈希/大小。"""
    row, path = _landed_envelope(project)
    document = json.loads(path.read_text(encoding="utf-8"))
    for block, fields in updates.items():
        document[block].update(fields)
    content = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path.write_bytes(content)
    db_conn = storage_db.connect(project.paths.db_path)
    try:
        db_conn.execute(
            "UPDATE raw_artifacts SET sha256 = ?, size_bytes = ? WHERE id = ?",
            (hashlib.sha256(content).hexdigest(), len(content), row["id"]),
        )
        db_conn.commit()
    finally:
        db_conn.close()


def _assert_identity_fault(project: LiveProject) -> dict:
    """坏身份恢复的完整形状：零观察、零新访问、uncertain + paused/fault、证据保留。"""
    calls_before = project.call_count()
    ledger_before = _suggest_accesses(project)
    result = project.resume(max_slots=1)

    assert project.call_count() == calls_before == 1, "坏身份不得再敲一次 CLI"
    assert project.query("SELECT state FROM suggestion_observations") == [], "坏证据不得补入库"
    operation = project.query(
        "SELECT * FROM operations WHERE run_id = ? AND operation_type = 'suggest'",
        (project.run_id,),
    )[0]
    assert operation["status"] == OperationState.uncertain.value
    assert _suggest_accesses(project) == ledger_before, "恢复核验本身不新增访问账"
    assert project.query(
        "SELECT COUNT(*) AS n FROM budget_reservations WHERE run_id = ? AND consumed_at IS NULL",
        (project.run_id,),
    )[0]["n"] == 0, "不得留下幽灵预留"
    assert len(project.query(
        "SELECT * FROM operation_attempts WHERE operation_id = ?", (operation["id"],)
    )) == 1, "坏身份不得开新 attempt"
    run = project.run_row()
    assert run["status"] == RunStatus.paused.value and run["pause_reason"] == PauseReason.fault.value
    assert result["engine"]["pause_reason"] == PauseReason.fault.value
    events = project.events("landed_result_invalid")
    assert events, "坏身份必须留下可复核的事件"
    _, path = _landed_envelope(project)
    assert path.is_file(), "拒收不是销毁：证据文件必须原样保留"
    return events[0]["detail"]


def _suggest_accesses(project: LiveProject) -> list[int]:
    operation = project.query(
        "SELECT id FROM operations WHERE run_id = ? AND operation_type = 'suggest'",
        (project.run_id,),
    )[0]
    return [
        int(row["attempt_no"]) for row in project.query(
            "SELECT attempt_no FROM budget_ledger WHERE operation_id = ? AND external_access = 1"
            " ORDER BY attempt_no", (operation["id"],)
        )
    ]


# ------------------------------------------------------------------ repro 两案（与独立探针同改法）


def test_attempt_no_999_is_not_this_operation(tmp_path, monkeypatch) -> None:
    """信封自称 attempt_no=999，登记的 attempt 却是 a1：自述与库内事实不符即拒收。"""
    project = _crashed_run(tmp_path, monkeypatch)
    _rewrite_document(project, context={"attempt_no": 999})
    detail = _assert_identity_fault(project)
    assert "attempt" in detail["reason"] or "尝试" in detail["reason"]


def test_unrelated_source_name_is_rejected(tmp_path, monkeypatch) -> None:
    """session.source_name 被改成无关来源：即便登记哈希/大小同步更新，也不是本次数据源的产物。"""
    project = _crashed_run(tmp_path, monkeypatch)
    _rewrite_document(project, session={"source_name": "unrelated-source"})
    _assert_identity_fault(project)


# ------------------------------------------------------------------ 其余身份维度


def test_attempt_row_owned_by_other_operation_is_rejected(tmp_path, monkeypatch) -> None:
    """登记行的 attempt_id 指向别的操作的 attempt：归属断裂即拒收（文件与哈希不动）。"""
    project = _crashed_run(tmp_path, monkeypatch)
    row, _ = _landed_envelope(project)
    db_conn = storage_db.connect(project.paths.db_path)
    try:
        operation = db_conn.execute(
            "SELECT run_id, keyword_id FROM operations WHERE id = ?", (row["operation_id"],)
        ).fetchone()
        with db_conn:  # 另一条真实存在的操作与 attempt（search/r0），但不属于被驱动的操作
            other = db_conn.execute(
                "INSERT INTO operations(run_id, keyword_id, operation_type, revisit_seq, status)"
                " VALUES (?, ?, 'search', 0, 'pending') RETURNING id",
                (str(operation["run_id"]), int(operation["keyword_id"])),
            ).fetchone()
            foreign_attempt = db_conn.execute(
                "INSERT INTO operation_attempts(operation_id, attempt_no, started_at)"
                " VALUES (?, 1, '2026-10-29T00:00:00+00:00') RETURNING id",
                (int(other["id"]),),
            ).fetchone()
            db_conn.execute(
                "UPDATE raw_artifacts SET attempt_id = ? WHERE id = ?",
                (int(foreign_attempt["id"]), row["id"]),
            )
    finally:
        db_conn.close()
    _assert_identity_fault(project)


def test_wrong_attempt_directory_with_valid_hash_is_rejected(tmp_path, monkeypatch) -> None:
    """把信封**挪**进同操作的 a2 目录并把登记路径/哈希/大小都改到"自洽"：路径不在本尝试的
    规范目录就不是本次操作的结果——哈希完好救不了身份。"""
    project = _crashed_run(tmp_path, monkeypatch)
    row, path = _landed_envelope(project)
    displaced = path.parent.parent / "a2" / path.name
    displaced.parent.mkdir(parents=True, exist_ok=True)
    path.replace(displaced)  # 是真挪走，不是留一份副本让补导入还能认领
    new_rel = str(row["rel_path"]).rsplit("/", 2)[0] + "/a2/" + path.name
    db_conn = storage_db.connect(project.paths.db_path)
    try:
        content = displaced.read_bytes()
        db_conn.execute(
            "UPDATE raw_artifacts SET rel_path = ?, sha256 = ?, size_bytes = ? WHERE id = ?",
            (new_rel, hashlib.sha256(content).hexdigest(), len(content), row["id"]),
        )
        db_conn.commit()
    finally:
        db_conn.close()
    detail = _assert_identity_fault(project)
    assert detail["rel_path"] == new_rel


@pytest.mark.parametrize(
    "bad_version",
    [
        "other-tool@2.0.1/schema:1.0",   # 前缀不是本适配器声明的来源
        "xhs-search@2.0.1/schema:9.9",   # schema 不在本适配器支持的固化口径内
    ],
    ids=["foreign-prefix", "unsupported-schema"],
)
def test_wrong_source_version_is_rejected(tmp_path, monkeypatch, bad_version) -> None:
    """source_name 也对、但版本对不上重建数据源的口径：同样是不可信证据。"""
    project = _crashed_run(tmp_path, monkeypatch)
    _rewrite_document(project, session={"source_version": bad_version})
    _assert_identity_fault(project)


# ------------------------------------------- 版本身份（P1-B-RECOVERY-VERSION-r1）
# 基准 = 本 attempt 登记的原始响应里工具**实际自报**的版本（cliVersion 2.0.1 / schema 1.0），
# 不再是"格式像不像"：同前缀假版本、看着合法的 schema 段、基线降级都必须被拒。

TRUE_REFINED_VERSION = "xhs-search@2.0.1/schema:1.0"
BASELINE_VERSION = (
    f"{sources_cli.EXPECTED_CLI_NAME}@schema<=max({sorted(sources_cli.SUPPORTED_SCHEMA_VERSIONS)})"
)


@pytest.mark.parametrize(
    "forged_version",
    [
        "xhs-search@999.0.0/schema:1.0",   # 与原始响应同前缀同 schema，但 CLI 版本是自报的
        "xhs-search@2.0.1/schema:unknown",  # schema 段缺失口径合法，可原始响应明明写着 1.0
        BASELINE_VERSION,                   # 已取回响应的成功采集不得被降级成基线串
    ],
    ids=["wrong-cli-version", "unknown-schema", "baseline-downgrade"],
)
def test_format_valid_but_not_the_original_version_is_rejected(tmp_path, monkeypatch,
                                                               forged_version) -> None:
    """改写规范化信封里的 source_version 并重算登记哈希/大小：版本以偏概全的"格式核验"必须拒。"""
    project = _crashed_run(tmp_path, monkeypatch)
    _rewrite_document(project, session={"source_version": forged_version})
    detail = _assert_identity_fault(project)
    assert "来源标识" in detail["reason"]


def test_true_original_version_still_reingests(tmp_path, monkeypatch) -> None:
    """逐字重写为**真实的原始自报版本**（字节与登记哈希都换）：真版本必须照样离线补入库。"""
    project = _crashed_run(tmp_path, monkeypatch)
    _rewrite_document(project, session={"source_version": TRUE_REFINED_VERSION})
    project.resume(max_slots=1)
    assert not project.events("landed_result_invalid")
    observations = project.query("SELECT state FROM suggestion_observations")
    assert [row["state"] for row in observations] == [ResultState.completed.value]
    seed_suggests = [c for c in project.calls() if c["command"] == "suggest" and c["seed"] == SEED]
    assert len(seed_suggests) == 1, "真实版本恢复不得重打崩溃前那一次同一种子的联想"


def test_corrupt_raw_response_evidence_is_uncertain(tmp_path, monkeypatch) -> None:
    """篡改本 attempt 登记的 cli_stdout.json 字节而登记不动：证据与登记不符即 uncertain，绝不放行。"""
    project = _crashed_run(tmp_path, monkeypatch)
    row = project.query(
        "SELECT * FROM raw_artifacts WHERE run_id = ? AND rel_path LIKE '%/cli/cli_stdout.json'",
        (project.run_id,),
    )[0]
    path = project.paths.resolve_within_run(row["rel_path"], project.run_id)
    path.write_bytes(path.read_bytes() + b"\n")
    detail = _assert_identity_fault(project)
    assert "原始响应" in detail["reason"]


def test_cli_version_rule_derives_from_registered_response(tmp_path) -> None:
    """适配器口径单元测试：基准只从登记的响应字节派生，无响应才允许基线，绝不自证。"""
    source = sources_cli.CliSearchSource(config=None, paths=None, out_root=tmp_path)

    def session(version: str) -> SessionRef:
        return SessionRef(session_id="unit-1", collected_at=utc_now(),
                          source_name=sources_cli.SOURCE_NAME, source_version=version,
                          synthetic=False)

    envelope = json.dumps({"cliVersion": "2.0.1", "schemaVersion": "1.0"}).encode("utf-8")
    # 没有登记任何原始响应（如 profile_busy 根本没发出）：只有基线形态合法。
    assert source.session_matches_source(session(BASELINE_VERSION), raw_responses=())
    assert not source.session_matches_source(session(TRUE_REFINED_VERSION), raw_responses=())
    # 有响应在：只认由该响应实际自报版本派生的串；基线/同前缀假版本都被拒。
    assert source.session_matches_source(session(TRUE_REFINED_VERSION), raw_responses=(envelope,))
    assert not source.session_matches_source(session(BASELINE_VERSION), raw_responses=(envelope,))
    assert not source.session_matches_source(session("xhs-search@999.0.0/schema:1.0"),
                                             raw_responses=(envelope,))
    # 响应解不出信封时，采集当时的唯一精炼形态是 unknown/unknown——派生口径同一式。
    assert source.session_matches_source(session("xhs-search@unknown/schema:unknown"),
                                         raw_responses=(b"not json",))
    assert not source.session_matches_source(session(TRUE_REFINED_VERSION),
                                             raw_responses=(b"not json",))


# ------------------------------------------------------------------ 正例：好身份必须仍可恢复


def test_valid_landed_envelope_reingests_offline(tmp_path, monkeypatch) -> None:
    """没被篡改的落盘信封：恢复仍走离线补入库（不重打同一联想、一次观察、不二次扣费）。

    补入库后引擎会继续按预算探索新关键词（合法的新访问），本用例只锁定"崩溃那一次
    已落盘的结果绝不再敲同一种子的 CLI"。
    """
    project = _crashed_run(tmp_path, monkeypatch)
    project.resume(max_slots=1)

    seed_suggests = [
        c for c in project.calls() if c["command"] == "suggest" and c["seed"] == SEED
    ]
    assert len(seed_suggests) == 1, "有效恢复不得重打崩溃前那一次同一种子的联想"
    observations = project.query(
        "SELECT state, item_count FROM suggestion_observations")
    assert [row["state"] for row in observations] == [ResultState.completed.value]
    assert int(observations[0]["item_count"]) == 1
    reingested = project.events("landed_result_reingested")
    assert reingested and reingested[0]["detail"]["access"] is False
    assert not project.events("landed_result_invalid")
    assert _suggest_accesses(project) == [1], "补入库不二次扣访问预算"
