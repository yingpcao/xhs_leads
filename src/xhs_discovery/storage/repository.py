"""Repository：把 DATA_MODEL 的层规则落成可执行读写（P0-06/P0-07）。

纪律：
- 全局身份层 upsert 只按身份键合并，run 状态一律带 `run_id`；
- 状态写入必须走 `set_run_status`（写 `run_events` 记原因），没有裸 UPDATE status 的入口；
- 每个写方法都走 `self.write()`：单独调用即是一个短事务，被外层 `db.transaction()` 包住时
  只开 SAVEPOINT，提交权归最外层调用者（批次原子性）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from xhs_discovery._version import ANALYSIS_VERSION, CODE_VERSION, PROTOCOL_VERSION
from xhs_discovery.models.common import sha256_text, utc_now
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    CandidateOrigin,
    OperationState,
    OperationType,
    ResultState,
    RunStatus,
    SlotKind,
)
from xhs_discovery.models.run import BranchStat, RewardUpdate, RunIdentity, RunTermination
from xhs_discovery.storage import db, migrations

_TERMINAL = (RunStatus.completed, RunStatus.failed)
_ALLOWED: dict[RunStatus, tuple[RunStatus, ...]] = {
    RunStatus.queued: (RunStatus.running,),
    RunStatus.running: (RunStatus.paused, RunStatus.completed, RunStatus.failed),
    RunStatus.paused: (RunStatus.running, RunStatus.completed, RunStatus.failed),
    RunStatus.completed: (),
    RunStatus.failed: (),
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("时间戳必须带时区")
    return value.astimezone(utc_now().tzinfo).isoformat()


def _row(conn: sqlite3.Connection, sql: str, params: tuple, *, one: bool = True) -> Any:
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


class Repository:
    """一个连接一个 Repository；不做连接池（P0 只有单 worker）。"""

    def __init__(self, conn: sqlite3.Connection, *, migrate: bool = True) -> None:
        self.conn = conn
        if migrate:
            migrations.apply_migrations(conn)

    @contextmanager
    def write(self):
        """所有写入的唯一出口：外层已开事务时只用 SAVEPOINT，绝不替调用方提交批次。

        `with self.conn:` 会把调用者的外层事务一起提交掉，崩溃恢复与"观察/证据/预算同批写入"
        的原子承诺就成了空话（Codex 复核意见 2026-10-24 17:23 第 2 条）。
        """
        with db.transaction(self.conn):
            yield self.conn

    # -------------------------------------------------- 全局身份层

    def ensure_keyword(
        self,
        normalized: str,
        raw: str,
        *,
        seen_at: datetime | None = None,
        is_ambiguous_seed: bool = False,
    ) -> int:
        """按规范化文本合并身份；原始写法永远保留在 keyword_aliases。"""
        if not normalized.strip():
            raise ValueError("normalized 不得为空；空规范化文本会吞掉不同词")
        at = _iso(seen_at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO keywords(normalized_text, first_seen_at, is_ambiguous_seed)"
                " VALUES (?, ?, ?)",
                (normalized, at, 1 if is_ambiguous_seed else 0),
            )
            if is_ambiguous_seed:
                # 只提示不改写：已存在的身份也可能后来被判为歧义种子
                self.conn.execute(
                    "UPDATE keywords SET is_ambiguous_seed = 1 WHERE normalized_text = ?",
                    (normalized,),
                )
            self.conn.execute(
                "INSERT OR IGNORE INTO keyword_aliases(keyword_id, raw_text, first_seen_at)"
                " SELECT id, ?, ? FROM keywords WHERE normalized_text = ?",
                (raw, at, normalized),
            )
        row = _row(self.conn, "SELECT id FROM keywords WHERE normalized_text = ?", (normalized,))
        assert row is not None
        return int(row["id"])

    def keyword_id(self, normalized: str) -> int | None:
        row = _row(self.conn, "SELECT id FROM keywords WHERE normalized_text = ?", (normalized,))
        return None if row is None else int(row["id"])

    def keyword_aliases(self, keyword_id: int) -> list[str]:
        return [
            row["raw_text"]
            for row in _row(
                self.conn,
                "SELECT raw_text FROM keyword_aliases WHERE keyword_id = ? ORDER BY id",
                (keyword_id,),
                one=False,
            )
        ]

    def ensure_author(self, *, public_ref: str | None, kind: str = "public_ref") -> int | None:
        """作者引用未知时返回 None：未知不等于"另一个作者"。"""
        if kind == "unknown":
            return None
        if not public_ref:
            raise ValueError("kind=public_ref 时 public_ref 必填")
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO authors(public_ref, kind) VALUES (?, 'public_ref')",
                (public_ref,),
            )
        row = _row(self.conn, "SELECT id FROM authors WHERE public_ref = ?", (public_ref,))
        assert row is not None
        return int(row["id"])

    def bump_author_post_count(self, author_id: int, *, delta: int = 1) -> None:
        with self.write():
            self.conn.execute(
                "UPDATE authors SET relevant_post_count = relevant_post_count + ? WHERE id = ?",
                (delta, author_id),
            )

    def ensure_post(
        self,
        note_id: str,
        access_url: str,
        *,
        seen_at: datetime | None = None,
        author_id: int | None = None,
    ) -> int:
        """帖子身份只依 note_id；已有链接不因去重而改写或截断。"""
        if not note_id:
            raise ValueError("note_id 必填")
        if not access_url.startswith(("http://", "https://")):
            raise ValueError("access_url 必须是完整可访问链接")
        at = _iso(seen_at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO posts(note_id, access_url, first_seen_at, author_id)"
                " VALUES (?, ?, ?, ?)",
                (note_id, access_url, at, author_id),
            )
            if author_id is not None:
                # 早期观察作者未知，后来补到引用时允许回填（不新建第二个身份）
                self.conn.execute(
                    "UPDATE posts SET author_id = ? WHERE note_id = ? AND author_id IS NULL",
                    (author_id, note_id),
                )
        row = _row(self.conn, "SELECT id FROM posts WHERE note_id = ?", (note_id,))
        assert row is not None
        return int(row["id"])

    def post_id(self, note_id: str) -> int | None:
        row = _row(self.conn, "SELECT id FROM posts WHERE note_id = ?", (note_id,))
        return None if row is None else int(row["id"])

    def ensure_content_version(
        self,
        post_id: int,
        field: str,
        text: str | None,
        *,
        captured_at: datetime | None = None,
        is_missing: bool = False,
    ) -> int:
        """文本变化产生新版本，旧证据继续指向旧版本。"""
        if field not in ("title", "body"):
            raise ValueError("field 只能是 title 或 body")
        if is_missing:
            if text is not None:
                raise ValueError("is_missing=True 表示确实为空/不可访问，不得同时带文本")
            payload = ""
        else:
            if text is None or text == "":
                raise ValueError("非缺失内容必须有文本；确实为空请用 is_missing=True 表达")
            payload = text
        digest = sha256_text(payload)
        at = _iso(captured_at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO post_content_versions"
                "(post_id, field, text, text_sha256, captured_at, is_missing)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (post_id, field, payload, digest, at, 1 if is_missing else 0),
            )
        row = _row(
            self.conn,
            "SELECT id FROM post_content_versions WHERE post_id = ? AND field = ?"
            " AND text_sha256 = ?",
            (post_id, field, digest),
        )
        assert row is not None
        return int(row["id"])

    def content_version(self, version_id: int) -> dict | None:
        return _row(self.conn, "SELECT * FROM post_content_versions WHERE id = ?", (version_id,))

    # -------------------------------------------------- 任务状态层：runs

    def create_run(
        self,
        identity: RunIdentity,
        *,
        config_json: str,
        status: RunStatus = RunStatus.queued,
        analysis_version: str = ANALYSIS_VERSION,
        code_version: str = CODE_VERSION,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> str:
        json.loads(config_json)  # 配置快照必须可反序列化，不能塞任意文本
        created = _iso(identity.created_at)
        with self.write():
            self.conn.execute(
                "INSERT INTO runs(id, seed_raw, seed_normalized, mode, status, config_json,"
                " config_hash, code_version, protocol_version, analysis_version, random_seed,"
                " synthetic_fixture, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.run_id,
                    identity.seed.raw,
                    identity.seed.normalized,
                    identity.mode,
                    status.value,
                    config_json,
                    identity.config_hash,
                    code_version,
                    protocol_version,
                    analysis_version,
                    identity.random_seed,
                    1 if identity.synthetic_fixture else 0,
                    created,
                    created,
                ),
            )
            self._add_event(
                identity.run_id, "created", detail={"status": status.value}, at=identity.created_at
            )
        return identity.run_id

    def get_run(self, run_id: str) -> dict | None:
        return _row(self.conn, "SELECT * FROM runs WHERE id = ?", (run_id,))

    def require_run(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"run 不存在：{run_id}")
        return run

    def _add_event(
        self, run_id: str, kind: str, *, detail: dict | None = None, at: datetime | None = None
    ) -> int:
        """事务内写事件：不自己 commit，避免把外层多语句写拆成两段。"""
        cursor = self.conn.execute(
            "INSERT INTO run_events(run_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
            (run_id, _iso(at or utc_now()), kind, json.dumps(detail or {}, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    def record_event(
        self, run_id: str, kind: str, *, detail: dict | None = None, at: datetime | None = None
    ) -> int:
        """独立调用入口：自带一个短事务，绝不留未提交的孤儿事件。"""
        with self.write():
            return self._add_event(run_id, kind, detail=detail, at=at)

    def events(self, run_id: str, *, kind: str | None = None) -> list[dict]:
        if kind is None:
            return _row(
                self.conn,
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY id",
                (run_id,),
                one=False,
            )
        return _row(
            self.conn,
            "SELECT * FROM run_events WHERE run_id = ? AND kind = ? ORDER BY id",
            (run_id, kind),
            one=False,
        )

    def set_run_status(
        self,
        run_id: str,
        termination: RunTermination,
        *,
        at: datetime | None = None,
    ) -> None:
        """唯一的状态写入口：白名单迁移 + 事件，禁止直接 UPDATE。"""
        run = self.require_run(run_id)
        current = RunStatus(run["status"])
        target = termination.status
        if target not in _ALLOWED[current]:
            raise ValueError(
                f"非法状态迁移：{current.value} → {target.value}（允许：{[s.value for s in _ALLOWED[current]]}）"
            )
        stamp = _iso(at or utc_now())
        finished = stamp if target in _TERMINAL else None
        outcome = termination.outcome.value if termination.outcome else None
        pause_reason = termination.pause_reason.value if termination.pause_reason else None
        if target not in _TERMINAL and outcome is not None:
            raise ValueError("非终态不得携带 outcome（paused 的原因写 pause_reason）")
        with self.write():
            self.conn.execute(
                "UPDATE runs SET status = ?, outcome = COALESCE(?, outcome),"
                " pause_reason = ?, started_at = COALESCE(started_at, ?),"
                " finished_at = ?, updated_at = ? WHERE id = ?",
                (
                    target.value,
                    outcome,
                    pause_reason,
                    stamp if target is RunStatus.running else None,
                    finished,
                    stamp,
                    run_id,
                ),
            )
            kind = {
                RunStatus.running: "resumed" if current is RunStatus.paused else "started",
                RunStatus.paused: "paused",
                RunStatus.completed: "stopped",
                RunStatus.failed: "failed",
                RunStatus.queued: "queued",
            }[target]
            self._add_event(
                run_id,
                "status_changed",
                detail={"from": current.value, "to": target.value},
                at=at,
            )
            self._add_event(
                run_id,
                kind,
                detail={
                    "outcome": outcome,
                    "pause_reason": pause_reason,
                    "note": termination.note,
                },
                at=at,
            )

    def clear_pause_reason(self, run_id: str) -> None:
        """resume 之后旧暂停原因不再有效，但事件历史保留。"""
        with self.write():
            self.conn.execute("UPDATE runs SET pause_reason = NULL WHERE id = ?", (run_id,))

    def bump_budget_revision(
        self, run_id: str, added: dict, *, at: datetime | None = None, note: str | None = None
    ) -> int:
        """追加预算重开 run：新增一行历史并递增 revision，绝不重置总量。"""
        self.require_run(run_id)
        with self.write():
            self.conn.execute(
                "INSERT INTO budget_revisions(run_id, added_json, created_at, note) VALUES (?, ?, ?, ?)",
                (run_id, json.dumps(added, ensure_ascii=False, sort_keys=True), _iso(at or utc_now()), note),
            )
            self.conn.execute(
                "UPDATE runs SET budget_revision = budget_revision + 1, updated_at = ? WHERE id = ?",
                (_iso(at or utc_now()), run_id),
            )
        row = _row(self.conn, "SELECT budget_revision AS r FROM runs WHERE id = ?", (run_id,))
        assert row is not None
        return int(row["r"])

    def budget_revisions(self, run_id: str) -> list[dict]:
        return _row(
            self.conn, "SELECT * FROM budget_revisions WHERE run_id = ? ORDER BY id", (run_id,), one=False
        )

    # -------------------------------------------------- run_keywords / branches / frontier

    def upsert_run_keyword(
        self,
        run_id: str,
        keyword_id: int,
        *,
        parent_keyword_id: int | None = None,
        depth: int = 0,
        branch_id: int | None = None,
        origin: str = "observed",
        status: str = "queued",
        seen_at: datetime | None = None,
        irrelevant_reason: str | None = None,
    ) -> int:
        if origin not in ("observed", "generated", "seed"):
            raise ValueError("origin 必须是 observed/generated/seed")
        if status == "irrelevant" and not irrelevant_reason:
            raise ValueError("判为 irrelevant 必须保存剔除原因")
        at = _iso(seen_at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT INTO run_keywords(run_id, keyword_id, parent_keyword_id, depth, branch_id,"
                " origin, status, first_seen_at, last_seen_at, irrelevant_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, keyword_id) DO UPDATE SET"
                " last_seen_at = excluded.last_seen_at,"
                " status = excluded.status,"
                " irrelevant_reason = COALESCE(excluded.irrelevant_reason, run_keywords.irrelevant_reason)",
                (run_id, keyword_id, parent_keyword_id, depth, branch_id, origin, status, at, at,
                 irrelevant_reason),
            )
        row = _row(
            self.conn, "SELECT id FROM run_keywords WHERE run_id = ? AND keyword_id = ?", (run_id, keyword_id)
        )
        assert row is not None
        return int(row["id"])

    def run_keyword(self, run_id: str, keyword_id: int) -> dict | None:
        return _row(
            self.conn,
            "SELECT * FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
            (run_id, keyword_id),
        )

    def increment_revisit(self, run_id: str, keyword_id: int) -> int:
        with self.write():
            self.conn.execute(
                "UPDATE run_keywords SET revisit_count = revisit_count + 1"
                " WHERE run_id = ? AND keyword_id = ?",
                (run_id, keyword_id),
            )
        row = _row(
            self.conn,
            "SELECT revisit_count AS c FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
            (run_id, keyword_id),
        )
        assert row is not None
        return int(row["c"])

    def ensure_branch(self, run_id: str, branch_key: str, *, label: str | None = None,
                      min_validation_share_left: int = 0) -> int:
        """分支主键稳定：展示标签可变，`branch_key` 不变，否则历史奖励无法解释。"""
        if not branch_key:
            raise ValueError("branch_key 必填")
        self.require_run(run_id)
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO branches(run_id, branch_key, display_label, created_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, branch_key, label, _iso(utc_now())),
            )
            if label is not None:
                self.conn.execute(
                    "UPDATE branches SET display_label = ? WHERE run_id = ? AND branch_key = ?",
                    (label, run_id, branch_key),
                )
            self.conn.execute(
                "INSERT OR IGNORE INTO branch_stats(run_id, branch_key, trials, total_reward,"
                " stale_streak, min_validation_share_left, updated_at)"
                " VALUES (?, ?, 0, 0.0, 0, ?, ?)",
                (run_id, branch_key, min_validation_share_left, _iso(utc_now())),
            )
        row = _row(
            self.conn, "SELECT id FROM branches WHERE run_id = ? AND branch_key = ?", (run_id, branch_key)
        )
        assert row is not None
        return int(row["id"])

    def branch_id(self, run_id: str, branch_key: str) -> int | None:
        row = _row(
            self.conn, "SELECT id FROM branches WHERE run_id = ? AND branch_key = ?", (run_id, branch_key)
        )
        return None if row is None else int(row["id"])

    def get_branch_stat(self, run_id: str, branch_key: str) -> BranchStat | None:
        row = _row(
            self.conn,
            "SELECT * FROM branch_stats WHERE run_id = ? AND branch_key = ?",
            (run_id, branch_key),
        )
        if row is None:
            return None
        return BranchStat(
            run_id=run_id,
            branch_id=branch_key,
            trials=int(row["trials"]),
            total_reward=float(row["total_reward"]),
            stale_streak=int(row["stale_streak"]),
            min_validation_share_left=int(row["min_validation_share_left"]),
        )

    def apply_reward(self, update: RewardUpdate, *, at: datetime | None = None) -> BranchStat:
        """奖励只归实际选中的分支；失败不给新增奖励（模型层已拒绝，这里只落账）。"""
        self.ensure_branch(update.run_id, update.attributed_branch_id)
        with self.write():
            self.conn.execute(
                "UPDATE branch_stats SET trials = trials + 1, total_reward = total_reward + ?,"
                " stale_streak = CASE WHEN ? > 0 THEN 0 ELSE stale_streak + 1 END,"
                " min_validation_share_left = MAX(min_validation_share_left - 1, 0),"
                " updated_at = ? WHERE run_id = ? AND branch_key = ?",
                (
                    update.reward,
                    update.new_valid_problem_clusters + update.new_related_keywords,
                    _iso(at or utc_now()),
                    update.run_id,
                    update.attributed_branch_id,
                ),
            )
        stat = self.get_branch_stat(update.run_id, update.attributed_branch_id)
        assert stat is not None
        return stat

    def stall_branch(self, run_id: str, branch_key: str, *, at: datetime | None = None) -> None:
        """单分支停滞只暂缓该分支：记事件并累加 stale_streak，不动 run 状态。"""
        self.ensure_branch(run_id, branch_key)
        with self.write():
            self.conn.execute(
                "UPDATE branch_stats SET stale_streak = stale_streak + 1, updated_at = ?"
                " WHERE run_id = ? AND branch_key = ?",
                (_iso(at or utc_now()), run_id, branch_key),
            )
        self.record_event(run_id, "branch_stalled", detail={"branch_key": branch_key}, at=at)

    def enqueue(
        self,
        run_id: str,
        keyword_id: int,
        branch_key: str,
        *,
        score: float | None = None,
        score_breakdown: dict | None = None,
        slot_kind: SlotKind | None = None,
        sample_kind: CalibrationSampleKind = CalibrationSampleKind.active_exploration,
        selection_reason: str,
        selection_probability: float | None = None,
        queued_at: datetime | None = None,
    ) -> int:
        """队列按 run 隔离；已 done/dropped 的同名条目重新入队而不新建第二个身份。"""
        if not selection_reason.strip():
            raise ValueError("选取理由必须可解释，不能空着入队")
        branch = self.branch_id(run_id, branch_key)
        if branch is None:
            branch = self.ensure_branch(run_id, branch_key)
        at = _iso(queued_at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT INTO frontier(run_id, keyword_id, branch_id, score, score_breakdown_json,"
                " slot_kind, sample_kind, selection_reason, selection_probability, queued_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')"
                " ON CONFLICT(run_id, keyword_id, branch_id) DO UPDATE SET"
                " status = 'pending', score = excluded.score,"
                " score_breakdown_json = excluded.score_breakdown_json,"
                " slot_kind = excluded.slot_kind, sample_kind = excluded.sample_kind,"
                " selection_reason = excluded.selection_reason,"
                " selection_probability = excluded.selection_probability,"
                " queued_at = excluded.queued_at, attempted_at = NULL,"
                " active_revisit_seq = NULL",
                (
                    run_id,
                    keyword_id,
                    branch,
                    score,
                    json.dumps(score_breakdown or {}, ensure_ascii=False, sort_keys=True),
                    slot_kind.value if slot_kind else None,
                    sample_kind.value,
                    selection_reason,
                    selection_probability,
                    at,
                ),
            )
        row = _row(
            self.conn,
            "SELECT id FROM frontier WHERE run_id = ? AND keyword_id = ? AND branch_id = ?",
            (run_id, keyword_id, branch),
        )
        assert row is not None
        return int(row["id"])

    def set_frontier_status(
        self, frontier_id: int, status: str, *, at: datetime | None = None
    ) -> None:
        if status not in ("pending", "in_flight", "done", "dropped"):
            raise ValueError("frontier.status 只能是 pending/in_flight/done/dropped")
        with self.write():
            self.conn.execute(
                "UPDATE frontier SET status = ?, attempted_at = COALESCE(attempted_at, ?) WHERE id = ?",
                (status, _iso(at or utc_now()) if status == "in_flight" else None, frontier_id),
            )

    def reset_frontier_for_retry(self, frontier_id: int) -> None:
        """阻断错误停住的未完成槽位：退回 pending 并清活动重探序号，留待人工处置后再采。

        这不是崩溃恢复的 in_flight 槽位（那种保留 active_revisit_seq 供离线补入库），而是
        访问已如实完成、只是被认证/验证/占用阻断的情况：清序号后按新一轮重探重新认领，
        历史 attempt、预算与观察都不改动。
        """
        with self.write():
            self.conn.execute(
                "UPDATE frontier SET status = 'pending', active_revisit_seq = NULL WHERE id = ?",
                (frontier_id,),
            )

    def claim_frontier_slot(self, frontier_id: int, run_id: str, keyword_id: int,
                            *, at: datetime | None = None) -> int | None:
        """认领一个 pending 槽位：把状态、活动重探序号与 revisit 递增锁在**同一事务**里。

        返回本次槽位锁定的 `revisit_seq`（即递增前的 `run_keywords.revisit_count`）。
        认领只认 `status='pending'` 的行——已是 in_flight/done/dropped 的行不会被二次认领，
        避免两个 worker（或同一 worker 的重入）把同一未完成槽位重新 increment 成下一轮 revisit，
        那样会开出第二个 logical key 的操作并真的重打 CLI（重复扣预算 + 丢第一次落盘结果）。
        若行不是 pending（并发下已被别人认领），返回 None，调用方跳过。
        """
        stamp = _iso(at or utc_now())
        with self.write():
            row = _row(
                self.conn,
                "SELECT status FROM frontier WHERE id = ? AND run_id = ? AND keyword_id = ?",
                (frontier_id, run_id, keyword_id),
            )
            if row is None or row["status"] != "pending":
                return None
            count = _row(
                self.conn,
                "SELECT revisit_count AS c FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
                (run_id, keyword_id),
            )
            if count is None:
                raise ValueError(
                    f"keyword_id={keyword_id} 未入库 run_keywords：认领槽位前必须已有 revisit_count 归属"
                )
            seq = int(count["c"])
            self.conn.execute(
                "UPDATE frontier SET status = 'in_flight', active_revisit_seq = ?,"
                " attempted_at = COALESCE(attempted_at, ?) WHERE id = ?",
                (seq, stamp, frontier_id),
            )
            self.conn.execute(
                "UPDATE run_keywords SET revisit_count = revisit_count + 1"
                " WHERE run_id = ? AND keyword_id = ?",
                (run_id, keyword_id),
            )
        return seq

    def frontier_rows(self, run_id: str, *, status: str | None = None) -> list[dict]:
        if status is None:
            return _row(
                self.conn,
                "SELECT f.*, b.branch_key, k.normalized_text FROM frontier f"
                " JOIN branches b ON b.id = f.branch_id JOIN keywords k ON k.id = f.keyword_id"
                " WHERE f.run_id = ? ORDER BY f.score DESC, f.id",
                (run_id,),
                one=False,
            )
        return _row(
            self.conn,
            "SELECT f.*, b.branch_key, k.normalized_text FROM frontier f"
            " JOIN branches b ON b.id = f.branch_id JOIN keywords k ON k.id = f.keyword_id"
            " WHERE f.run_id = ? AND f.status = ? ORDER BY f.score DESC, f.id",
            (run_id, status),
            one=False,
        )

    # -------------------------------------------------- 操作 / attempt / 发现边

    def open_operation(
        self,
        run_id: str,
        keyword_id: int,
        operation_type: OperationType,
        revisit_seq: int = 0,
        *,
        branch_key: str | None = None,
        slot_kind: SlotKind | None = None,
        sample_kind: CalibrationSampleKind | None = None,
    ) -> int:
        """逻辑键 `UNIQUE(run_id, keyword_id, operation_type, revisit_seq)` 幂等打开。"""
        if not self.require_keyword_registered(run_id, keyword_id):
            raise ValueError(
                f"keyword_id={keyword_id} 不存在：调度与预算扣减前必须已入库（见 DATA_MODEL 第 6.7 条）"
            )
        branch = self.branch_id(run_id, branch_key) if branch_key else None
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO operations(run_id, keyword_id, operation_type, revisit_seq,"
                " status, branch_id, slot_kind, sample_kind)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    run_id,
                    keyword_id,
                    operation_type.value,
                    revisit_seq,
                    branch,
                    slot_kind.value if slot_kind else None,
                    sample_kind.value if sample_kind else None,
                ),
            )
        row = self.operation_by_key(run_id, keyword_id, operation_type, revisit_seq)
        assert row is not None
        return int(row["id"])

    def require_keyword_registered(self, run_id: str, keyword_id: int) -> bool:
        """run 归属检查：关键词必须存在于全局身份层。"""
        row = _row(self.conn, "SELECT 1 AS ok FROM keywords WHERE id = ?", (keyword_id,))
        return row is not None

    def operation_by_key(
        self, run_id: str, keyword_id: int, operation_type: OperationType, revisit_seq: int = 0
    ) -> dict | None:
        return _row(
            self.conn,
            "SELECT * FROM operations WHERE run_id = ? AND keyword_id = ? AND operation_type = ?"
            " AND revisit_seq = ?",
            (run_id, keyword_id, operation_type.value, revisit_seq),
        )

    def get_operation(self, operation_id: int) -> dict | None:
        return _row(self.conn, "SELECT * FROM operations WHERE id = ?", (operation_id,))

    def require_observation_source(
        self,
        operation_id: int,
        attempt_id: int,
        *,
        run_id: str,
        operation_type: OperationType,
        session_id: str,
    ) -> dict:
        """入库前核对"这条观察声称的来源"在库里真是这么回事。

        外键只保证这些 id 曾经存在，不保证它们互相归属、也不保证属于当前 run：
        不核对，正文观察就能挂着 A run 的操作写进 B run 的收敛计数（C-09）。
        与账本 `_validated_operation` 同口径：全部校验在写账/写入之前，拒绝不留半行。
        """
        operation = _row(
            self.conn,
            "SELECT id, run_id, operation_type, keyword_id FROM operations WHERE id = ?",
            (operation_id,),
        )
        if operation is None:
            raise ValueError(f"操作不存在：{operation_id}")
        if operation["run_id"] != run_id:
            raise ValueError(
                f"操作 {operation_id} 属于 run {operation['run_id']}，观察却要写进 {run_id}"
            )
        actual = OperationType(operation["operation_type"])
        if actual is not operation_type:
            raise ValueError(
                f"操作 {operation_id} 的类型是 {actual.value}，观察却说是 {operation_type.value}"
            )
        attempt = _row(
            self.conn,
            "SELECT id, operation_id, attempt_no FROM operation_attempts WHERE id = ?",
            (attempt_id,),
        )
        if attempt is None:
            raise ValueError(f"尝试 {attempt_id} 不存在：没发起过的读取不能落观察")
        if int(attempt["operation_id"]) != operation_id:
            raise ValueError(
                f"尝试 {attempt_id} 属于操作 {attempt['operation_id']}，不是操作 {operation_id}"
            )
        if not session_id.strip():
            raise ValueError(
                "观察必须带非空 session_id：会话号与 (operation_id, attempt_id) 一起才是"
                "幂等键，空会话等于放弃幂等保护"
            )
        return dict(operation)

    def mark_operation_started(self, operation_id: int, *, at: datetime | None = None) -> None:
        stamp = _iso(at or utc_now())
        with self.write():
            self.conn.execute(
                "UPDATE operations SET status = 'in_flight', first_started_at = COALESCE(first_started_at, ?)"
                " WHERE id = ?",
                (stamp, operation_id),
            )

    def set_operation_status(self, operation_id: int, status: OperationState, *, at: datetime | None = None) -> None:
        if status not in OperationState:
            raise ValueError(f"非法 operation 状态：{status}")
        with self.write():
            self.conn.execute(
                "UPDATE operations SET status = ?, last_finished_at = ? WHERE id = ?",
                (status.value, _iso(at or utc_now()), operation_id),
            )

    def start_attempt(self, operation_id: int, attempt_no: int, *, at: datetime | None = None) -> int:
        """重试 = 新 attempt；同一 attempt 重复开始不产生第二行。"""
        if attempt_no < 1:
            raise ValueError("attempt_no 从 1 开始")
        stamp = _iso(at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO operation_attempts(operation_id, attempt_no, started_at)"
                " VALUES (?, ?, ?)",
                (operation_id, attempt_no, stamp),
            )
        row = _row(
            self.conn,
            "SELECT id FROM operation_attempts WHERE operation_id = ? AND attempt_no = ?",
            (operation_id, attempt_no),
        )
        assert row is not None
        return int(row["id"])

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        result_state: ResultState,
        error_code: str | None = None,
        error: dict | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        raw_artifact_count: int = 0,
        at: datetime | None = None,
    ) -> None:
        stamp = _iso(at or utc_now())
        with self.write():
            self.conn.execute(
                "UPDATE operation_attempts SET finished_at = ?, result_state = ?, error_code = ?,"
                " error_json = ?, exit_code = ?, duration_ms = ?, raw_artifact_count = ? WHERE id = ?",
                (
                    stamp,
                    result_state.value,
                    error_code,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    exit_code,
                    duration_ms,
                    raw_artifact_count,
                    attempt_id,
                ),
            )
            row = _row(self.conn, "SELECT operation_id FROM operation_attempts WHERE id = ?", (attempt_id,))
            if row is not None:
                self.conn.execute(
                    "UPDATE operations SET status = ?, last_finished_at = ? WHERE id = ?",
                    (result_state.value, stamp, int(row["operation_id"])),
                )

    def attempt_by_number(self, operation_id: int, attempt_no: int) -> dict | None:
        return _row(
            self.conn,
            "SELECT * FROM operation_attempts WHERE operation_id = ? AND attempt_no = ?",
            (operation_id, attempt_no),
        )

    def attempts(self, operation_id: int) -> list[dict]:
        return _row(
            self.conn,
            "SELECT * FROM operation_attempts WHERE operation_id = ? ORDER BY attempt_no",
            (operation_id,),
            one=False,
        )

    def operations(self, run_id: str, *, status: OperationState | None = None) -> list[dict]:
        if status is None:
            return _row(
                self.conn, "SELECT * FROM operations WHERE run_id = ? ORDER BY id", (run_id,), one=False
            )
        return _row(
            self.conn,
            "SELECT * FROM operations WHERE run_id = ? AND status = ? ORDER BY id",
            (run_id, status.value),
            one=False,
        )

    def unfinished_operations(self, run_id: str) -> list[dict]:
        return _row(
            self.conn,
            "SELECT * FROM operations WHERE run_id = ? AND status IN ('pending', 'in_flight')"
            " ORDER BY id",
            (run_id,),
            one=False,
        )

    def record_discovery_edge(
        self,
        run_id: str,
        parent_keyword_id: int,
        child_keyword_id: int,
        *,
        origin: CandidateOrigin,
        rank: int | None = None,
        session_id: str | None = None,
        operation_id: int | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        """只有 observed 才能成为 SUGGESTS 边；generated 记录只是探针线索。"""
        if parent_keyword_id == child_keyword_id:
            raise ValueError("自环不是发现边")
        at = _iso(observed_at or utc_now())
        with self.write():
            self.conn.execute(
                "INSERT OR IGNORE INTO discovery_edges(run_id, parent_keyword_id, child_keyword_id,"
                " rank, session_id, observed_at, origin, operation_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    parent_keyword_id,
                    child_keyword_id,
                    rank,
                    session_id,
                    at,
                    origin.value,
                    operation_id,
                ),
            )
        row = _row(
            self.conn,
            "SELECT id FROM discovery_edges WHERE run_id = ? AND parent_keyword_id = ?"
            " AND child_keyword_id = ? AND origin = ? AND session_id IS ?",
            (run_id, parent_keyword_id, child_keyword_id, origin.value, session_id),
        )
        assert row is not None
        return int(row["id"])

    def discovery_edges(self, run_id: str, *, origin: CandidateOrigin | None = None) -> list[dict]:
        if origin is None:
            return _row(
                self.conn, "SELECT * FROM discovery_edges WHERE run_id = ? ORDER BY id", (run_id,), one=False
            )
        return _row(
            self.conn,
            "SELECT * FROM discovery_edges WHERE run_id = ? AND origin = ? ORDER BY id",
            (run_id, origin.value),
            one=False,
        )


__all__ = ["Repository"]

