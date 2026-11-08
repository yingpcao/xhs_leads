"""C22 崩溃恢复专项：接管未完成槽位、离线补入库落盘结果，全程本机假 CLI + 临时库，绝不联网。

分工：`test_live_cli_pipeline.py` 测接线与失败形状，`test_live_cli_codex_recovery.py` 测重启/预派
发记账；本份只钻 C22 的两条恢复不变量——

1. **未完成槽位接管**（`frontier.active_revisit_seq`）：认领与重探递增同事务，接管沿用原序号，
   绝不把同一个未完成槽位二次递增成新的一轮重探（那会开出第二个逻辑键、真的重打 CLI、丢首份落盘）；
2. **落盘未入库的规范化结果离线补入库**：只补本 run 本 operation 本 attempt、经路径归属/哈希/契约/
   身份/来源会话核验的结果；坏证据或文件不可用一律标 uncertain 停住，**不重发、不二次扣费、不覆盖旧批次**。

注入方式都落在"规范化信封已落盘之后、入库提交之前"这个真实崩溃窗口：patch `ingest_suggestion`
（引擎在 `write_artifact` 之后才调它入库），令其在首槽抛异常——前置的 CLI 访问已发生、结果已落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_live_cli_pipeline import LiveProject, SEED, happy_steps
from xhs_discovery import app
from xhs_discovery.discovery import engine, stopping
from xhs_discovery.discovery.engine import (
    LANDED_INTEGRITY_CODE,
    prepare_run,
)
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import OperationState, RunStatus
from xhs_discovery.sources import cli as sources_cli
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository


class _Crash(Exception):
    """代表"进程被硬中断"的注入异常：只在崩溃窗口抛出，不伪装成平台返回。"""


def _crash_after_first_land(monkeypatch: pytest.MonkeyPatch, *, times: int = 1) -> None:
    """让规范化结果落盘后的入库前 `times` 次抛异常，其余照常（引擎在 write_artifact 之后才调 ingest）。

    崩溃窗口只在首次驱动出现；恢复驱动时计数已超过阈值，wrapped 自动委托真实 ingest 补入库，
    因此无需中途 undo（undo 会连带回退 LiveProject 注入的 env/chdir 而破坏 resume）。
    """
    original = engine.ingest_suggestion
    state = {"n": 0}

    def wrapped(*args, **kwargs):
        state["n"] += 1
        if state["n"] <= times:
            raise _Crash("normalized result landed but ingest interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "ingest_suggestion", wrapped)


def _crashed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **tweaks) -> LiveProject:
    """跑一条在首个联想槽"落盘后、入库前"被硬中断的 live run：frontier 停在 in_flight、结果已落盘。"""
    tweaks.setdefault("suggest_attempts", 2)
    tweaks.setdefault("search_attempts", 1)
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), **tweaks)
    _crash_after_first_land(monkeypatch)
    with pytest.raises(_Crash):
        project.execute()
    # 崩溃把异常抛回 CLI 之外，run 停在 running、槽位停在 in_flight——这正是恢复要面对的现场
    project.run_id = str(project.query("SELECT id FROM runs")[0]["id"])
    return project


def _suggest_op(project: LiveProject, revisit_seq: int | None = None) -> dict:
    rows = project.query(
        "SELECT * FROM operations WHERE run_id = ? AND operation_type = 'suggest'"
        + ("" if revisit_seq is None else " AND revisit_seq = ?"),
        (project.run_id,) if revisit_seq is None else (project.run_id, revisit_seq),
    )
    return rows[0]


def _seed_suggest_calls(project: LiveProject) -> list[dict]:
    return [c for c in project.calls() if c["command"] == "suggest" and c["seed"] == SEED]


def _external_access(project: LiveProject, operation_id: int) -> list[int]:
    rows = project.query(
        "SELECT attempt_no FROM budget_ledger WHERE operation_id = ? AND external_access = 1"
        " ORDER BY attempt_no",
        (operation_id,),
    )
    return [int(row["attempt_no"]) for row in rows]


# --------------------------------------------------------------------- 交付 1：接管未完成槽位


def test_takeover_preserves_original_revisit_seq_and_reingests_offline(tmp_path, monkeypatch) -> None:
    """接管沿用原重探序号，离线补入库已落盘结果：不再打 CLI、不二次扣费、不新开一轮重探。"""
    project = _crashed_run(tmp_path, monkeypatch)
    # 现场核对：崩溃后有一个带 active_revisit_seq 的在飞槽、一条已落盘的规范化信封
    inflight = project.query(
        "SELECT * FROM frontier WHERE run_id = ? AND status = 'in_flight'", (project.run_id,)
    )
    assert len(inflight) == 1 and inflight[0]["active_revisit_seq"] is not None
    assert _seed_suggest_calls(project) and project.call_count() >= 1

    calls_before = project.call_count()
    result = project.resume(max_slots=1)

    reclaimed = project.events("frontier_reclaimed")
    assert reclaimed, "接管未完成槽位必须留下可复核的事件"
    detail = reclaimed[0]["detail"]
    assert detail["frontier_id"] == int(inflight[0]["id"])
    assert detail["active_revisit_seq"] == int(inflight[0]["active_revisit_seq"])

    reingested = project.events("landed_result_reingested")
    assert reingested and reingested[0]["detail"]["access"] is False, "补入库必须是不访问的离线动作"
    operation = _suggest_op(project, revisit_seq=int(inflight[0]["active_revisit_seq"]))
    assert operation["status"] == OperationState.completed.value

    # 首份落盘被复用：本 run 的联想操作只有崩溃那一次真实访问，恢复不再新增一笔
    assert _external_access(project, int(operation["id"])) == [1], "恢复只补入库，不再扣一次访问"
    assert len(_seed_suggest_calls(project)) == 1, "绝不重打同一种子的联想 CLI"
    # 接管不得把同一未完成槽位二次递增成新的一轮重探：只有一个 suggest 操作行、序号就是崩溃时那个
    assert project.query(
        "SELECT COUNT(*) AS n FROM operations WHERE run_id = ? AND operation_type = 'suggest'"
        " AND keyword_id = (SELECT keyword_id FROM run_keywords WHERE run_id = ? AND depth = 0)",
        (project.run_id, project.run_id),
    )[0]["n"] == 1
    assert project.query(
        "SELECT COUNT(*) AS n FROM frontier WHERE run_id = ? AND status = 'in_flight'",
        (project.run_id,),
    )[0]["n"] == 0, "接管完成后在飞槽位必须收敛，不留悬挂"
    observations = project.query(
        "SELECT state FROM suggestion_observations WHERE operation_id = ?", (int(operation["id"]),)
    )
    assert observations and all(row["state"] == "completed" for row in observations)
    assert calls_before >= 1


def test_takeover_does_not_converge_on_an_empty_looking_queue(tmp_path, monkeypatch) -> None:
    """在飞槽位计入待办：接管前绝不因"pending 看起来空"就判队列排空/收敛。"""
    project = _crashed_run(tmp_path, monkeypatch)
    # 让 pending 组看起来是空的，只剩一个在飞槽——停止策略仍必须认为有待办，不判 exhausted
    pending = project.query(
        "SELECT COUNT(*) AS n FROM frontier WHERE run_id = ? AND status = 'pending'",
        (project.run_id,),
    )[0]["n"]
    assert pending == 0
    conn = storage_db.connect(project.paths.db_path)
    try:
        repo = Repository(conn)
        decision = stopping.evaluate(
            repo, project.run_id, project.settings,
            Ledger.from_settings(conn, project.run_id, project.settings),
            error=None,
        )
        # 关键：pending 为 0 但有一个在飞槽，collect_signals 不得把它当排空
        assert decision.signals.pending_items >= 1, "在飞槽位必须计入待办，否则会被误判为队列已排空"
    finally:
        conn.close()


# --------------------------------------------------------------------- 交付 2：落盘未入库离线补入库


def test_landed_but_not_done_replays_without_second_ingest(tmp_path, monkeypatch) -> None:
    """已入库却没收尾（frontier 仍在飞）：接管走重放，不再补第二条观察、不再打 CLI、不再扣费。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(),
                          suggest_attempts=2, search_attempts=1)
    # 让"入库成功"照常发生，但首次 expand 崩，留下已入库 + 在飞的现场；恢复时委托真实 expand 收尾
    original = engine.DiscoveryEngine._expand_and_enqueue
    boom_state = {"n": 0}

    def wrapped(self, *a, **k):  # noqa: ANN001 - 注入崩溃
        boom_state["n"] += 1
        if boom_state["n"] == 1:
            raise _Crash("ingested but slot not finalized")
        return original(self, *a, **k)

    monkeypatch.setattr(engine.DiscoveryEngine, "_expand_and_enqueue", wrapped)
    with pytest.raises(_Crash):
        project.execute()
    project.run_id = str(project.query("SELECT id FROM runs")[0]["id"])

    inflight = project.query(
        "SELECT * FROM frontier WHERE run_id = ? AND status = 'in_flight'", (project.run_id,)
    )
    assert len(inflight) == 1
    operation = _suggest_op(project, revisit_seq=int(inflight[0]["active_revisit_seq"]))
    obs_before = project.query(
        "SELECT COUNT(*) AS n FROM suggestion_observations WHERE run_id = ?", (project.run_id,)
    )[0]["n"]
    assert obs_before >= 1, "崩溃前观察已入库"

    calls_before = project.call_count()
    project.resume(max_slots=1)

    assert not project.events("landed_result_reingested"), "已入库的槽位不该再补一次离线入库"
    assert project.query(
        "SELECT COUNT(*) AS n FROM suggestion_observations WHERE run_id = ?", (project.run_id,)
    )[0]["n"] == obs_before, "重放不得产生第二条观察"
    assert len(_seed_suggest_calls(project)) == 1, "重放不再打 CLI"
    assert project.call_count() == calls_before
    assert _external_access(project, int(operation["id"])) == [1], "重放不再扣访问"


def test_missing_landed_file_admits_uncertain_without_resend(tmp_path, monkeypatch) -> None:
    """落盘文件确实不可用：承认不确定 + 走 C10 既有记账，绝不重发、不二次扣访问。"""
    project = _crashed_run(tmp_path, monkeypatch)
    operation = _suggest_op(project)
    op_id = int(operation["id"])
    env_rows = project.query(
        "SELECT rel_path FROM raw_artifacts WHERE operation_id = ? AND kind = 'stdout_json'"
        " AND rel_path LIKE '%/suggest.json'", (op_id,),
    )
    assert env_rows, "崩溃现场必须有一份已登记的规范化信封"
    target = project.paths.resolve_within_run(env_rows[0]["rel_path"], project.run_id)
    target.unlink()  # 假装磁盘上的落盘结果真的丢了

    calls_before = project.call_count()
    result = project.resume(max_slots=1)

    assert project.events("normalized_artifact_unavailable")
    invalid = project.events("landed_result_invalid")
    assert invalid and invalid[0]["detail"]["status"] == "missing_file"
    assert project.query(
        "SELECT status FROM operations WHERE id = ?", (op_id,)
    )[0]["status"] == OperationState.uncertain.value
    # 承认不确定：不再打 CLI、不再扣访问、停在非终态交人工
    assert len(_seed_suggest_calls(project)) == 1, "文件不可用也不重发"
    assert project.call_count() == calls_before
    assert _external_access(project, op_id) == [1]
    assert result["engine"]["status"] != RunStatus.completed.value, "坏结果恢复绝不伪装成收敛/完成"


def test_tampered_landed_file_pauses_never_resends(tmp_path, monkeypatch) -> None:
    """落盘文件被篡改（哈希对不上登记）：这是坏证据，必须停在暂停态交人工，绝不按当前口径重发覆盖。"""
    project = _crashed_run(tmp_path, monkeypatch)
    op_id = int(_suggest_op(project)["id"])
    env = project.query(
        "SELECT rel_path FROM raw_artifacts WHERE operation_id = ? AND kind = 'stdout_json'"
        " AND rel_path LIKE '%/suggest.json'", (op_id,),
    )[0]
    target = project.paths.resolve_within_run(env["rel_path"], project.run_id)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["context"]["run_id"] = "run-别的-run"  # 身份被换掉：既是内容篡改也是归属不符
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    calls_before = project.call_count()
    result = project.resume(max_slots=1)

    assert project.events("landed_result_invalid")
    halted = project.events("frontier_halted_integrity")
    assert halted and halted[0]["detail"]["code"] == LANDED_INTEGRITY_CODE
    # 坏证据槽位保留在飞待核查，既不重发也不清序号
    assert project.query(
        "SELECT status FROM frontier WHERE run_id = ? AND id = ?",
        (project.run_id, halted[0]["detail"]["frontier_id"]),
    )[0]["status"] == "in_flight"
    assert len(_seed_suggest_calls(project)) == 1, "篡改的证据绝不重发覆盖旧批次"
    assert project.call_count() == calls_before
    assert result["engine"]["status"] != RunStatus.completed.value, "篡改恢复绝不伪装完成"


def test_raw_cli_stdout_is_never_parsed_as_normalized(tmp_path, monkeypatch) -> None:
    """补入库只认 `{op}.json` 规范化信封，绝不把 `cli/cli_stdout.json`（同为 stdout_json）当结果。"""
    project = _crashed_run(tmp_path, monkeypatch)
    op_id = int(_suggest_op(project)["id"])
    # 只删规范化信封，留一份同 kind 的原始 stdout 在 cli/ 下：解析器必须跳过它 → 判 missing_file
    env = project.query(
        "SELECT rel_path FROM raw_artifacts WHERE operation_id = ? AND kind = 'stdout_json'"
        " AND rel_path LIKE '%/suggest.json'", (op_id,),
    )[0]
    project.paths.resolve_within_run(env["rel_path"], project.run_id).unlink()
    cli_stdout = project.query(
        "SELECT rel_path FROM raw_artifacts WHERE operation_id = ? AND rel_path LIKE '%/cli/%'"
        " AND rel_path LIKE '%cli_stdout.json'", (op_id,),
    )
    assert cli_stdout, "现场必须有一份同 kind 的原始 stdout 作为诱饵"
    project.resume(max_slots=1)
    # 若把 cli_stdout 当规范化信封解析，身份/契约校验会拦下并记 invalid；这里应是 missing_file 而非补入库
    assert not project.events("landed_result_reingested"), "原始 stdout 绝不被补入库"
    assert project.events("normalized_artifact_unavailable")


# --------------------------------------------------------------------- 交付 3：安全重启与幂等


def test_recovery_only_runs_missing_actions_with_one_billing_each(tmp_path, monkeypatch) -> None:
    """崩溃后接管：已有可靠观察就重放，只缺的后续动作才访问；每种操作至多一笔访问账。"""
    project = _crashed_run(tmp_path, monkeypatch)
    project.resume(max_slots=1)  # 接管补入库 suggest，然后按缺的 search 才访问

    suggest_op = _suggest_op(project)
    assert _external_access(project, int(suggest_op["id"])) == [1], "联想一次访问"
    searches = project.query(
        "SELECT id FROM operations WHERE run_id = ? AND operation_type = 'search'", (project.run_id,)
    )
    for row in searches:
        assert len(_external_access(project, int(row["id"]))) <= 1, "内容操作至多一笔访问"
    # 恢复来的数据不是"新的一次采集"：不重复建发现边到同一子词
    edges = project.query(
        "SELECT parent_keyword_id, child_keyword_id, COUNT(*) AS n FROM discovery_edges"
        " WHERE run_id = ? GROUP BY parent_keyword_id, child_keyword_id HAVING n > 1", (project.run_id,)
    )
    assert edges == [], "恢复不得重复堆叠同一条发现边"


def test_recovery_respects_max_slots_and_single_revisit(tmp_path, monkeypatch) -> None:
    """接管在 max_slots 预算内只推进一个槽位，且不重复递增重探数、不重复计调度槽。"""
    project = _crashed_run(tmp_path, monkeypatch)
    kw_id = int(project.query(
        "SELECT keyword_id FROM run_keywords WHERE run_id = ? AND depth = 0", (project.run_id,)
    )[0]["keyword_id"])
    revisit_before = int(project.query(
        "SELECT revisit_count FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
        (project.run_id, kw_id),
    )[0]["revisit_count"])
    project.resume(max_slots=1)
    revisit_after = int(project.query(
        "SELECT revisit_count FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
        (project.run_id, kw_id),
    )[0]["revisit_count"])
    assert revisit_after == revisit_before, "接管沿用原序号，绝不二次递增重探数"


def test_blocking_error_halts_same_slot_before_next_access(tmp_path, monkeypatch) -> None:
    """联想认证失败：即便搜索预算与采样额度都 >0，同一槽位也必须只有 ONE 次调用，不再往下敲。"""
    step = {"command": "suggest", "seed": SEED, "returncode": 3, "no_rows": True,
            "top_error": {"code": "AUTH_REQUIRED", "message": "需要登录", "hint": "运行 init"}}
    project = LiveProject(tmp_path, monkeypatch, [step],
                          suggest_attempts=2, search_attempts=2, card_limit=2, max_posts_stored=2)
    result = app.execute_run(project.config_path, project.cwd, mode="live", seed=SEED)
    project.run_id = str(result["run_id"])

    assert project.call_count() == 1, "认证失败后不得继续发起搜索/正文/作者访问"
    assert result["engine"]["status"] == RunStatus.paused.value
    assert result["engine"]["pause_reason"] == "auth_required"
    assert project.query(
        "SELECT COUNT(*) AS n FROM operations WHERE run_id = ? AND operation_type IN"
        " ('search','body','author_posts')", (project.run_id,),
    )[0]["n"] == 0, "同一槽位被阻断后不应开出任何后续操作"


# --------------------------------------------------------------------- 交付 4：原子认领与历史遗留


def test_claim_frontier_slot_is_atomic_and_idempotent(tmp_path, monkeypatch) -> None:
    """认领把状态、活动重探序号与 revisit 递增锁在同一事务；非 pending 行二次认领返回 None。"""
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=2, search_attempts=2)
    conn = storage_db.connect(project.paths.db_path)
    try:
        repo = Repository(conn)
        prepare_run(repo, project.settings, run_id="run-claim", created_at=utc_now())
        row = repo.frontier_rows("run-claim", status="pending")[0]
        fid, kw = int(row["id"]), int(row["keyword_id"])
        before = int(repo.run_keyword("run-claim", kw)["revisit_count"])

        seq = repo.claim_frontier_slot(fid, "run-claim", kw, at=utc_now())
        conn.commit()
        assert seq == before, "认领返回的是递增前的重探序号"
        claimed = repo.frontier_rows("run-claim", status="in_flight")
        assert len(claimed) == 1 and claimed[0]["active_revisit_seq"] == seq
        after = int(repo.run_keyword("run-claim", kw)["revisit_count"])
        assert after == before + 1, "认领同事务递增一次重探数"

        # 已在飞：二次认领必须被拒（绝不把它再递增成新的一轮重探）
        assert repo.claim_frontier_slot(fid, "run-claim", kw, at=utc_now()) is None
        assert int(repo.run_keyword("run-claim", kw)["revisit_count"]) == after
    finally:
        conn.close()


def test_reset_frontier_for_retry_clears_seq_but_not_history(tmp_path, monkeypatch) -> None:
    """阻断错误退回 pending 会清活动重探序号，但历史 attempt/观察/预算一律不动。"""
    project = _crashed_run(tmp_path, monkeypatch)
    conn = storage_db.connect(project.paths.db_path)
    try:
        repo = Repository(conn)
        inflight = repo.frontier_rows(project.run_id, status="in_flight")[0]
        fid = int(inflight["id"])
        op_id = int(_suggest_op(project)["id"])
        attempts_before = len(project.attempts(op_id))
        repo.reset_frontier_for_retry(fid)
        conn.commit()
        fresh = repo.frontier_rows(project.run_id, status="pending")
        assert any(int(r["id"]) == fid and r["active_revisit_seq"] is None for r in fresh)
        assert len(project.attempts(op_id)) == attempts_before, "退回 pending 不得抹掉历史 attempt"
    finally:
        conn.close()


def test_legacy_in_flight_without_seq_recovers_conservatively(tmp_path, monkeypatch) -> None:
    """v7 之前遗留的在飞槽位没有 active_revisit_seq：保守标待核查、保留在飞，绝不猜序号重发。"""
    project = _crashed_run(tmp_path, monkeypatch)
    conn = storage_db.connect(project.paths.db_path)
    try:
        with storage_db.transaction(conn):
            conn.execute(
                "UPDATE frontier SET active_revisit_seq = NULL WHERE run_id = ? AND status = 'in_flight'",
                (project.run_id,),
            )
    finally:
        conn.close()

    calls_before = project.call_count()
    project.resume(max_slots=1)

    legacy = project.events("frontier_legacy_uncertain")
    assert legacy, "无序号的在飞槽位必须留下保守处置事件"
    assert project.call_count() == calls_before, "保守恢复绝不重打 CLI"
    assert project.query(
        "SELECT status FROM frontier WHERE run_id = ? AND id = ?",
        (project.run_id, int(legacy[0]["detail"]["frontier_id"])),
    )[0]["status"] == "in_flight", "保守处置保留在飞状态，交人工而非自动重发"


def test_recovered_rows_are_attributed_non_synthetic_live(tmp_path, monkeypatch) -> None:
    """恢复补入库的数据仍带真实来源与 synthetic=0：恢复不是"重新采集"，也不冒充合成。"""
    project = _crashed_run(tmp_path, monkeypatch)
    project.resume(max_slots=1)
    rows = project.query(
        "SELECT synthetic, source_name, source_version FROM suggestion_observations WHERE run_id = ?",
        (project.run_id,),
    )
    assert rows and all(int(r["synthetic"]) == 0 for r in rows)
    assert all(r["source_name"] == sources_cli.SOURCE_NAME for r in rows)
    assert project.run_row()["mode"] == "live" and int(project.run_row()["synthetic_fixture"]) == 0
