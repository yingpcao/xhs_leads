"""P0-07：run 状态机、事件、run_keywords/branches/frontier 与操作持久化。

关注点不是"能不能写进去"，而是文档承诺的可恢复性质：
状态只能走白名单且每次都有事件、跨 run 完全隔离、分支主键稳定、
操作逻辑键幂等、重试是新 attempt、`generated` 候选不伪装 SUGGESTS 边。
"""

from __future__ import annotations

import json

import pytest

from xhs_discovery.models.enums import (
    CandidateOrigin,
    OperationState,
    OperationType,
    PauseReason,
    ResultState,
    RunStatus,
    SlotKind,
    TerminalOutcome,
)
from xhs_discovery.models.run import RewardUpdate, RunTermination
from conftest import build_run_identity

HASH = "a" * 64


def terminate(status: RunStatus, **overrides) -> RunTermination:
    return RunTermination(status=status, **overrides)


def reward(run_id: str, branch: str, *, clusters: int = 0, keywords: int = 1,
           evidence: list[int] | None = None, op: OperationType = OperationType.search) -> RewardUpdate:
    return RewardUpdate(
        run_id=run_id,
        keyword_id=7,
        operation_type=op,
        revisit_seq=0,
        attempt_no=1,
        attributed_branch_id=branch,
        new_valid_problem_clusters=clusters,
        new_related_keywords=keywords,
        normalized_cost=1.0,
        cluster_keyword_weight=0.2,
        result_state=ResultState.completed,
        valid_cluster_evidence_ids=evidence or [],
    )


# ------------------------------------------------------------------ 全局身份层


def test_keyword_identity_merges_by_normalized_text_and_keeps_raw_forms(storage):
    first = storage.repo.ensure_keyword("上海中考", "上海中考", seen_at=None)
    second = storage.repo.ensure_keyword("上海中考", "上海 中考")
    assert first == second
    assert storage.repo.keyword_aliases(first) == ["上海中考", "上海 中考"]
    assert storage.repo.keyword_id("上海 中考") is None, "别名不参与身份查找"
    with pytest.raises(ValueError, match="不得为空"):
        storage.repo.ensure_keyword("   ", "空白")


def test_ambiguous_seed_flag_is_a_hint_not_a_rewrite(storage):
    kid = storage.repo.ensure_keyword("中考", "中考", is_ambiguous_seed=True)
    row = storage.conn.execute("SELECT is_ambiguous_seed FROM keywords WHERE id = ?", (kid,)).fetchone()
    assert row["is_ambiguous_seed"] == 1
    assert storage.repo.keyword_aliases(kid) == ["中考"]


def test_post_identity_is_note_id_only_and_never_rewrites_the_url(storage):
    author = storage.repo.ensure_author(public_ref="user-1")
    post = storage.repo.ensure_post("note-1", "https://www.xiaohongshu.com/explore/note-1",
                                    author_id=author)
    again = storage.repo.ensure_post("note-1", "https://www.xiaohongshu.com/explore/note-1?x=1")
    assert post == again
    stored = storage.conn.execute("SELECT access_url, author_id FROM posts WHERE id = ?", (post,)).fetchone()
    assert stored["access_url"] == "https://www.xiaohongshu.com/explore/note-1"
    assert stored["author_id"] == author
    with pytest.raises(ValueError, match="完整可访问链接"):
        storage.repo.ensure_post("note-2", "xiaohongshu.com/explore/note-2")


def test_unknown_author_is_none_rather_than_a_new_identity(storage):
    assert storage.repo.ensure_author(public_ref=None, kind="unknown") is None
    author = storage.repo.ensure_author(public_ref="user-2")
    storage.repo.ensure_post("note-3", "https://x/note-3", author_id=author)
    storage.repo.bump_author_post_count(author)
    storage.repo.bump_author_post_count(author)
    row = storage.conn.execute("SELECT relevant_post_count FROM authors WHERE id = ?", (author,)).fetchone()
    assert row["relevant_post_count"] == 2
    count = storage.conn.execute("SELECT COUNT(*) AS c FROM authors").fetchone()["c"]
    assert count == 1, "作者未知不能编造第二个身份"


def test_content_versions_accumulate_instead_of_overwriting_history(storage):
    post = storage.repo.ensure_post("note-4", "https://x/note-4")
    v1 = storage.repo.ensure_content_version(post, "title", "志愿到底怎么排")
    same = storage.repo.ensure_content_version(post, "title", "志愿到底怎么排")
    v2 = storage.repo.ensure_content_version(post, "title", "志愿到底怎么排（更新）")
    assert v1 == same and v1 != v2
    assert storage.repo.content_version(v1)["text"] == "志愿到底怎么排"
    missing = storage.repo.ensure_content_version(post, "body", None, is_missing=True)
    assert storage.repo.content_version(missing)["is_missing"] == 1
    with pytest.raises(ValueError, match="field"):
        storage.repo.ensure_content_version(post, "comment", "不是标题也不是正文")


# ------------------------------------------------------------------ run 状态机


def test_create_run_persists_the_identity_snapshot_and_rejects_bad_config_json(storage):
    repo = storage.repo
    identity = build_run_identity("run-b")
    repo.create_run(identity, config_json=json.dumps({"budget": {"suggest_attempts": 3}}))
    run = repo.require_run("run-b")
    assert run["config_hash"] == HASH
    assert json.loads(run["config_json"])["budget"]["suggest_attempts"] == 3
    assert run["synthetic_fixture"] == 1
    assert repo.events("run-b")[0]["kind"] == "created"
    with pytest.raises(json.JSONDecodeError):
        repo.create_run(build_run_identity("run-c"), config_json="不是 JSON")


def test_run_status_only_moves_along_the_whitelist(storage):
    repo = storage.repo
    repo.set_run_status("run-a", terminate(RunStatus.running))
    repo.set_run_status("run-a", terminate(RunStatus.paused, pause_reason=PauseReason.user))
    repo.set_run_status("run-a", terminate(RunStatus.running))
    repo.set_run_status("run-a", terminate(RunStatus.completed, outcome=TerminalOutcome.converged,
                                             note="窗口内新增有效簇为 0"))
    assert repo.require_run("run-a")["outcome"] == "converged"
    recorded = [json.loads(event["detail_json"]) for event in repo.events("run-a", kind="status_changed")]
    assert [row["to"] for row in recorded] == ["running", "paused", "running", "completed"]
    assert [row["from"] for row in recorded] == ["queued", "running", "paused", "running"]
    with pytest.raises(ValueError, match="非法状态迁移"):
        repo.set_run_status("run-a", terminate(RunStatus.running))


def test_queued_run_cannot_jump_straight_to_completed(storage):
    with pytest.raises(ValueError, match="非法状态迁移"):
        storage.repo.set_run_status("run-a", terminate(RunStatus.completed,
                                                       outcome=TerminalOutcome.budget_exhausted))


def test_terminal_cause_events_carry_the_reason_not_just_the_state(storage):
    repo = storage.repo
    repo.set_run_status("run-a", terminate(RunStatus.running))
    repo.set_run_status("run-a", terminate(RunStatus.paused, pause_reason=PauseReason.auth_required,
                                           note="需要登录"))
    paused = [event for event in repo.events("run-a") if event["kind"] == "paused"]
    assert json.loads(paused[0]["detail_json"])["pause_reason"] == "auth_required"
    assert repo.require_run("run-a")["pause_reason"] == "auth_required"
    repo.set_run_status("run-a", terminate(RunStatus.running))
    repo.clear_pause_reason("run-a")
    assert repo.require_run("run-a")["pause_reason"] is None
    assert len([event for event in repo.events("run-a") if event["kind"] == "paused"]) == 1


def test_converged_termination_model_rejects_a_reasonless_claim(storage):
    with pytest.raises(ValueError, match="converged 必须写明"):
        terminate(RunStatus.completed, outcome=TerminalOutcome.converged)
    with pytest.raises(ValueError, match="paused 必须记录原因"):
        terminate(RunStatus.paused)


# ------------------------------------------------------------------ run_keywords / branches / frontier


def test_run_keyword_state_is_one_row_per_run_and_keeps_revisit_count(storage):
    kid = storage.repo.ensure_keyword("上海中考", "上海中考")
    storage.repo.upsert_run_keyword("run-a", kid, origin="seed", depth=0)
    storage.repo.upsert_run_keyword("run-a", kid, status="explored")
    rows = storage.conn.execute("SELECT COUNT(*) AS c FROM run_keywords WHERE run_id='run-a'").fetchone()
    assert rows["c"] == 1
    assert storage.repo.run_keyword("run-a", kid)["status"] == "explored"
    assert storage.repo.increment_revisit("run-a", kid) == 1
    with pytest.raises(ValueError, match="剔除原因"):
        storage.repo.upsert_run_keyword("run-a", kid, status="irrelevant")
    storage.repo.upsert_run_keyword("run-a", kid, status="irrelevant", irrelevant_reason="与需求无关")
    assert storage.repo.run_keyword("run-a", kid)["irrelevant_reason"] == "与需求无关"


def test_the_same_keyword_keeps_independent_state_in_two_runs(storage):
    kid = storage.repo.ensure_keyword("名额分配", "名额分配")
    storage.repo.create_run(build_run_identity("run-b"), config_json="{}")
    storage.repo.upsert_run_keyword("run-a", kid, status="explored")
    storage.repo.upsert_run_keyword("run-b", kid, status="queued")
    assert storage.repo.run_keyword("run-a", kid)["status"] == "explored"
    assert storage.repo.run_keyword("run-b", kid)["status"] == "queued"


def test_branch_identity_survives_a_display_label_change(storage):
    branch = storage.repo.ensure_branch("run-a", "b1", label="联想纵向")
    assert storage.repo.branch_id("run-a", "b1") == branch
    storage.repo.ensure_branch("run-a", "b1", label="平台联想")
    rows = storage.conn.execute("SELECT id, display_label FROM branches WHERE run_id='run-a'").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == branch and rows[0]["display_label"] == "平台联想"
    with pytest.raises(ValueError, match="branch_key 必填"):
        storage.repo.ensure_branch("run-a", "")


def test_reward_is_attributed_to_the_chosen_branch_only(storage):
    storage.repo.ensure_branch("run-a", "b1")
    storage.repo.ensure_branch("run-a", "b2")
    stat = storage.repo.apply_reward(reward("run-a", "b1", clusters=1, evidence=[1]))
    assert stat.trials == 1 and stat.stale_streak == 0
    assert stat.total_reward == pytest.approx(1.2)
    other = storage.repo.get_branch_stat("run-a", "b2")
    assert other.trials == 0 and other.total_reward == 0.0
    stalled = storage.repo.apply_reward(reward("run-a", "b2", keywords=0, clusters=0))
    assert stalled.trials == 1 and stalled.stale_streak == 1
    assert storage.repo.get_branch_stat("run-a", "b2").mean_reward == 0.0


def test_branch_stall_does_not_touch_the_run_state(storage):
    storage.repo.set_run_status("run-a", terminate(RunStatus.running))
    storage.repo.ensure_branch("run-a", "b1")
    storage.repo.stall_branch("run-a", "b1")
    assert storage.repo.get_branch_stat("run-a", "b1").stale_streak == 1
    assert storage.repo.require_run("run-a")["status"] == "running"
    assert storage.repo.events("run-a", kind="branch_stalled")


def test_frontier_is_queue_scoped_by_run_and_explains_its_choice(storage):
    kid = storage.repo.ensure_keyword("上海中考 志愿", "上海中考 志愿")
    other_run = build_run_identity("run-b")
    storage.repo.create_run(other_run, config_json="{}")
    storage.repo.ensure_branch("run-a", "b1")
    storage.repo.ensure_branch("run-b", "b1")
    first = storage.repo.enqueue("run-a", kid, "b1", score=0.8,
                                 score_breakdown={"raw": {"S": 3}, "weights": {"S": 0.4}},
                                 slot_kind=SlotKind.high_score, selection_reason="最高分槽")
    row = storage.repo.frontier_rows("run-a")[0]
    assert json.loads(row["score_breakdown_json"])["weights"]["S"] == 0.4
    assert row["selection_reason"] == "最高分槽"
    again = storage.repo.enqueue("run-a", kid, "b1", score=0.95, selection_reason="重复入队取最新分")
    assert first == again, "同一 (run, 关键词, 分支) 只有一条队列行，重新入队覆盖分数与理由"
    latest = storage.repo.frontier_rows("run-a")[0]
    assert latest["score"] == 0.95 and latest["selection_reason"] == "重复入队取最新分"
    storage.repo.enqueue("run-b", kid, "b1", score=0.3, selection_reason="另一个 run 独立探索")
    assert [r["run_id"] for r in storage.repo.frontier_rows("run-b")] == ["run-b"]
    with pytest.raises(ValueError, match="选取理由"):
        storage.repo.enqueue("run-a", kid, "b1", selection_reason="   ")


def test_frontier_status_lifecycle_and_illegal_value(storage):
    kid = storage.repo.ensure_keyword("自招", "自招")
    storage.repo.ensure_branch("run-a", "b1")
    frontier_id = storage.repo.enqueue("run-a", kid, "b1", score=0.5, selection_reason="探索槽")
    storage.repo.set_frontier_status(frontier_id, "in_flight")
    assert storage.repo.frontier_rows("run-a", status="in_flight")[0]["attempted_at"]
    storage.repo.set_frontier_status(frontier_id, "done")
    assert storage.repo.frontier_rows("run-a", status="pending") == []
    with pytest.raises(ValueError, match="只能是"):
        storage.repo.set_frontier_status(frontier_id, "teleported")


# ------------------------------------------------------------------ 操作 / attempt / 发现边


def test_open_operation_is_idempotent_on_the_logical_key(storage):
    kid = storage.repo.ensure_keyword("上海中考", "上海中考")
    storage.repo.upsert_run_keyword("run-a", kid, origin="seed")
    storage.repo.ensure_branch("run-a", "b1")
    op = storage.repo.open_operation("run-a", kid, OperationType.suggest, 0, branch_key="b1",
                                     slot_kind=SlotKind.high_score)
    assert storage.repo.open_operation("run-a", kid, OperationType.suggest, 0) == op
    assert storage.repo.open_operation("run-a", kid, OperationType.suggest, 1) != op
    row = storage.repo.get_operation(op)
    assert row["slot_kind"] == "high_score" and row["branch_id"] == storage.repo.branch_id("run-a", "b1")


def test_scheduling_requires_a_registered_keyword(storage):
    with pytest.raises(ValueError, match="必须已入库"):
        storage.repo.open_operation("run-a", 999, OperationType.suggest)


def test_retry_creates_a_new_attempt_and_keeps_the_operation_row(storage):
    kid = storage.repo.ensure_keyword("上海中考", "上海中考")
    op = storage.repo.open_operation("run-a", kid, OperationType.search, 0)
    storage.repo.mark_operation_started(op)
    attempt_1 = storage.repo.start_attempt(op, 1)
    assert storage.repo.start_attempt(op, 1) == attempt_1
    storage.repo.finish_attempt(attempt_1, result_state=ResultState.failed, error_code="PAGE_TIMEOUT",
                                exit_code=5, duration_ms=1200, raw_artifact_count=1)
    assert storage.repo.get_operation(op)["status"] == "failed"
    attempt_2 = storage.repo.start_attempt(op, 2)
    storage.repo.finish_attempt(attempt_2, result_state=ResultState.completed, raw_artifact_count=2)
    assert [row["attempt_no"] for row in storage.repo.attempts(op)] == [1, 2]
    assert storage.repo.attempt_by_number(op, 1)["exit_code"] == 5
    with pytest.raises(ValueError, match="attempt_no 从 1 开始"):
        storage.repo.start_attempt(op, 0)


def test_uncertain_is_a_legal_operation_state_and_finish_attempt_validates(storage):
    kid = storage.repo.ensure_keyword("上海中考", "上海中考")
    op = storage.repo.open_operation("run-a", kid, OperationType.suggest, 0)
    storage.repo.set_operation_status(op, OperationState.uncertain)
    assert storage.repo.get_operation(op)["status"] == "uncertain"
    with pytest.raises(ValueError, match="非法 operation 状态"):
        storage.repo.set_operation_status(op, "dreaming")


def test_unfinished_operations_are_the_recovery_surface(storage):
    kid = storage.repo.ensure_keyword("上海中考", "上海中考")
    pending = storage.repo.open_operation("run-a", kid, OperationType.suggest, 0)
    done = storage.repo.open_operation("run-a", kid, OperationType.search, 0)
    storage.repo.set_operation_status(done, OperationState.completed)
    stuck = storage.repo.open_operation("run-a", kid, OperationType.body, 0)
    storage.repo.mark_operation_started(stuck)
    found = {int(row["id"]) for row in storage.repo.unfinished_operations("run-a")}
    assert found == {pending, stuck}


def test_generated_candidates_keep_the_edge_but_never_claim_an_observed_suggestion(storage):
    parent = storage.repo.ensure_keyword("上海中考", "上海中考")
    child = storage.repo.ensure_keyword("上海中考 名额分配", "上海中考 名额分配")
    generated_child = storage.repo.ensure_keyword("上海中考 怎么报名", "上海中考 怎么报名")
    kid_row = storage.repo.open_operation("run-a", parent, OperationType.suggest, 0)
    storage.repo.record_discovery_edge("run-a", parent, child, origin=CandidateOrigin.observed,
                                       rank=2, session_id="sess-1", operation_id=kid_row)
    storage.repo.record_discovery_edge("run-a", parent, child, origin=CandidateOrigin.observed,
                                       rank=2, session_id="sess-1", operation_id=kid_row)
    storage.repo.record_discovery_edge("run-a", parent, generated_child, origin=CandidateOrigin.generated,
                                       session_id="sess-1")
    assert len(storage.repo.discovery_edges("run-a", origin=CandidateOrigin.observed)) == 1
    assert len(storage.repo.discovery_edges("run-a")) == 2
    with pytest.raises(ValueError, match="自环"):
        storage.repo.record_discovery_edge("run-a", parent, parent, origin=CandidateOrigin.observed)


def test_multi_parent_paths_are_all_kept(storage):
    seed = storage.repo.ensure_keyword("上海中考", "上海中考")
    middle = storage.repo.ensure_keyword("名额分配", "名额分配")
    leaf = storage.repo.ensure_keyword("名额分配 条件", "名额分配 条件")
    storage.repo.record_discovery_edge("run-a", seed, leaf, origin=CandidateOrigin.observed,
                                      session_id="sess-1")
    storage.repo.record_discovery_edge("run-a", middle, leaf, origin=CandidateOrigin.observed,
                                      session_id="sess-2")
    edges = storage.repo.discovery_edges("run-a", origin=CandidateOrigin.observed)
    assert {row["parent_keyword_id"] for row in edges} == {seed, middle}


def test_budget_revision_history_only_adds(storage):
    storage.repo.bump_budget_revision("run-a", {"suggest_attempts": 5}, note="第一次追加")
    storage.repo.bump_budget_revision("run-a", {"search_attempts": 2}, note="第二次追加")
    rows = storage.repo.budget_revisions("run-a")
    assert [json.loads(row["added_json"]) for row in rows] == [{"suggest_attempts": 5},
                                                              {"search_attempts": 2}]
    assert storage.repo.require_run("run-a")["budget_revision"] == 3
