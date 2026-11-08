"""C-09：正文观察必须带可审计的 run/operation/attempt 来源。

帖子身份与正文**内容版本**是全局的（同一段文本不因 run 不同而存两份），但"我这个 run
取到过它"是按 run 发生的事实。旧实现把这两件事混在一起：`body_observations` 没有来源列，
`content_validation_count` 只按全局 `post_id` 连本 run 的 `post_queries`，于是 run A 的
completed 正文会被 run B（只有同篇帖子的标题卡片）算成自己的内容验证量，B 就能借 A 的证据
过收敛门槛。这里钉死：

- A 正文成功、B 只有标题 → B 计数 0；B 正文失败仍 0；B 正文成功才 1；
- 同帖、同 `captured_at`、同 session_id 的两个 run 各写各的观察行（跨 run 不互相吞掉）；
- 重复同 attempt 不再写第二行，库级唯一索引同样拒绝绕过入库层的直写；
- 来源错位（跨 run / 错类型 / attempt 不属于该操作 / 空会话）一律拒绝且不留半行；
- 老库升级后来源为 NULL 的历史行保留原样，但不计入当前 run 的收敛；
- 收敛反例：其余条件全备的 B 在借到 A 正文时仍不得 converged。

用的是项目真实迁移、真实 Repository 与真实 ingest，不是内存替身表。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from xhs_discovery.discovery.ingest import ingest_body, ingest_search
from xhs_discovery.discovery.stopping import (
    content_validation_count,
    convergence_failures,
    evaluate,
)
from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.enums import (
    CandidateOrigin,
    ErrorCategory,
    OperationType,
    ResultState,
    RunStatus,
    TerminalOutcome,
)
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.source import (
    AuthorRef,
    ContentVersionRef,
    KeywordText,
    OperationContext,
    PostBodyResult,
    PostCard,
    PostSearchResult,
)
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage import migrations
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository

from tests.conftest import T0, build_run_identity
from tests.test_stopping import BRANCH, StopEnv, at, budget_settings, session_for

WORD = "上海中考"
TITLE = "上海中考名额分配怎么申请"


def note_id_for(title: str) -> str:
    return f"n-{sha256_text(title)[:10]}"


@pytest.fixture
def pair(tmp_path):
    """同一个文件库里两个 run：共享全局帖子身份，各自有自己的操作与观察。"""
    conn = storage_db.connect(tmp_path / "data.db")
    repo = Repository(conn)
    settings = budget_settings()
    envs: dict[str, StopEnv] = {}
    for run_id in ("run-a", "run-b"):
        repo.create_run(build_run_identity(run_id), config_json='{"budget": {}}')
        repo.ensure_branch(run_id, BRANCH, label="主分支")
        keyword_id = repo.ensure_keyword(WORD, WORD, seen_at=T0)
        repo.upsert_run_keyword(
            run_id,
            keyword_id,
            depth=0,
            origin=CandidateOrigin.observed.value,
            status="explored",
            branch_id=repo.branch_id(run_id, BRANCH),
            seen_at=T0,
        )
        envs[run_id] = StopEnv(
            repo=repo,
            run_id=run_id,
            settings=settings,
            ledger=Ledger.from_settings(conn, run_id, settings),
            keyword_id=keyword_id,
        )
    try:
        yield SimpleNamespace(conn=conn, repo=repo, a=envs["run-a"], b=envs["run-b"])
    finally:
        conn.close()


def add_cards(env: StopEnv, *, title: str = TITLE, minute: int) -> tuple[int, int, str]:
    """一次搜索观察：卡片与标题版本落库，正文一列都没有。返回 (keyword_id, 重探序号, note_id)。"""
    env.observations += 1
    seq = env.observations
    keyword_id = env.register(WORD)
    note = note_id_for(title)
    context = OperationContext(
        run_id=env.run_id,
        keyword_id=keyword_id,
        keyword=KeywordText(raw=WORD, normalized=WORD),
        operation_type=OperationType.search,
        revisit_seq=seq,
        attempt_no=1,
    )
    operation = env.repo.open_operation(
        env.run_id, keyword_id, OperationType.search, seq, branch_key=BRANCH
    )
    attempt = env.repo.start_attempt(operation, 1)
    ingest_search(
        env.repo,
        env.run_id,
        PostSearchResult(
            state=ResultState.completed,
            context=context,
            session=session_for(f"sess-c-{env.run_id}-{seq}", at(minute)),
            items=[
                PostCard(
                    note_id=note,
                    access_url=f"https://www.xiaohongshu.com/explore/{note}",
                    title=title,
                    author=AuthorRef(kind="public_ref", public_ref="u-1"),
                    published_at_text="2026-09-01",
                    likes_text="3",
                    result_rank=1,
                    search_keyword=KeywordText(raw=WORD, normalized=WORD),
                )
            ],
        ),
        operation_id=operation,
        attempt_id=attempt,
    )
    return keyword_id, seq, note


def add_legacy_cards_v2(env: StopEnv, *, minute: int) -> tuple[int, int, str]:
    """按 v2 那一代的写法落一张卡片：库里根本没有正文来源列与卡片可见字段列。

    这里刻意不走 `ingest_search`——现在的入库层写的是 v4 形状的列，在 v2 库上会直接报
    "no such column"。用裸 SQL 手工建行才能构造出真实的历史库状态。
    """
    env.observations += 1
    seq = env.observations
    keyword_id = env.register(WORD)
    note = note_id_for(TITLE)
    operation = env.repo.open_operation(
        env.run_id, keyword_id, OperationType.search, seq, branch_key=BRANCH
    )
    attempt = env.repo.start_attempt(operation, 1)
    seen = at(minute)
    author_id = env.repo.ensure_author(public_ref="u-1", kind="public_ref")
    post_id = env.repo.ensure_post(
        note, f"https://www.xiaohongshu.com/explore/{note}", seen_at=seen, author_id=author_id
    )
    env.repo.ensure_content_version(post_id, "title", TITLE, captured_at=seen)
    with env.repo.write():
        cursor = env.repo.conn.execute(
            "INSERT INTO post_observations(run_id, operation_id, attempt_id, session_id,"
            " keyword_id, observed_at, state, card_count, source_name, source_version, synthetic)"
            " VALUES (?, ?, ?, ?, ?, ?, 'completed', 1, 'fixture', '0.0.1', 1)",
            (env.run_id, operation, attempt, f"sess-c-{env.run_id}-{seq}", keyword_id,
             seen.isoformat()),
        )
        env.repo.conn.execute(
            "INSERT INTO post_queries(run_id, post_observation_id, post_id, keyword_id,"
            " result_rank) VALUES (?, ?, ?, ?, 1)",
            (env.run_id, int(cursor.lastrowid), post_id, keyword_id),
        )
    return keyword_id, seq, note


def add_body(
    env: StopEnv,
    *,
    keyword_id: int,
    seq: int,
    note: str,
    minute: int,
    session_id: str | None = None,
    state: ResultState = ResultState.completed,
    body: str | None = "正文内容",
    body_is_empty: bool = False,
    error: ErrorInfo | None = None,
):
    """一次正文观察。`session_id`/`minute` 由调用方钉死，用来构造跨 run 同时间戳的场景。"""
    keyword_text = KeywordText(raw=WORD, normalized=WORD)
    result = PostBodyResult(
        state=state,
        context=OperationContext(
            run_id=env.run_id,
            keyword_id=keyword_id,
            keyword=keyword_text,
            operation_type=OperationType.body,
            revisit_seq=seq,
            attempt_no=1,
        ),
        session=session_for(session_id or f"sess-b-{env.run_id}-{seq}", at(minute)),
        body=body,
        body_is_empty=body_is_empty,
        content_version=(
            ContentVersionRef(text=body, text_sha256=sha256_text(body)) if body else None
        ),
        error=error,
    )
    operation = env.repo.open_operation(
        env.run_id, keyword_id, OperationType.body, seq, branch_key=BRANCH
    )
    attempt = env.repo.start_attempt(operation, 1)
    return ingest_body(
        env.repo,
        env.run_id,
        result,
        note_id=note,
        operation_id=operation,
        attempt_id=attempt,
    )


def rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, post_id, state, run_id, operation_id, attempt_id, session_id, captured_at"
            " FROM body_observations ORDER BY id"
        )
    ]


# ------------------------------------------------------------------ 验收主链


def test_validation_count_never_borrows_another_runs_body(pair) -> None:
    """Codex 指定的四步：B 依次 0（只有卡片）→ 0（正文失败）→ 1（正文成功）。"""
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    add_body(pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=1)
    assert content_validation_count(pair.conn, "run-a") == 1

    b_kw, b_seq, _ = add_cards(pair.b, minute=0)
    assert content_validation_count(pair.conn, "run-b") == 0, "同篇帖子的标题卡片不算正文验证"

    add_body(
        pair.b,
        keyword_id=b_kw,
        seq=b_seq,
        note=note,
        minute=1,
        state=ResultState.failed,
        body=None,
        error=ErrorInfo(
            code="WORD_PAGE_TIMEOUT",
            message="正文超时",
            category=ErrorCategory.network,
            retryable=True,
            human_action_required=False,
        ),
    )
    assert content_validation_count(pair.conn, "run-b") == 0, "失败的读取不算验证"

    add_body(pair.b, keyword_id=b_kw, seq=b_seq + 100, note=note, minute=2)
    assert content_validation_count(pair.conn, "run-b") == 1
    assert content_validation_count(pair.conn, "run-a") == 1, "B 的读取也不该反过来动 A 的计数"


def test_body_observation_carries_its_own_run_operation_attempt(pair) -> None:
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    ingest = add_body(pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=1)
    assert (ingest.run_id, ingest.created) == ("run-a", True)
    row = rows(pair.conn)[0]
    assert row["run_id"] == "run-a"
    assert int(row["operation_id"]) == ingest.operation_id
    assert int(row["attempt_id"]) == ingest.attempt_id
    assert row["session_id"] == ingest.session_id
    # 观察行挂的操作确实属于这个 run、类型是 body：归属可以逐条回查
    claim = pair.conn.execute(
        "SELECT run_id, operation_type FROM operations WHERE id = ?", (ingest.operation_id,)
    ).fetchone()
    assert (claim["run_id"], claim["operation_type"]) == ("run-a", "body")


def test_same_post_same_timestamp_two_runs_write_two_observations(pair) -> None:
    """跨 run 幂等隔离：同帖、同 captured_at、甚至同 session_id 也不互相吞掉。

    旧的 (post_id, captured_at) 去重键在这里会让 B 直接拿到 A 的行（created=False），
    B 于是永远补不上自己的正文观察。
    """
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    first = add_body(
        pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=5, session_id="sess-shared"
    )
    b_kw, b_seq, _ = add_cards(pair.b, minute=0)
    second = add_body(
        pair.b, keyword_id=b_kw, seq=b_seq, note=note, minute=5, session_id="sess-shared"
    )
    assert second.created is True and second.observation_id != first.observation_id
    stored = rows(pair.conn)
    assert len(stored) == 2
    assert {row["run_id"] for row in stored} == {"run-a", "run-b"}
    assert len({row["captured_at"] for row in stored}) == 1, "同时间戳是刻意构造的"
    assert len({row["session_id"] for row in stored}) == 1, "同 session_id 也不能跨 run 认领"
    assert content_validation_count(pair.conn, "run-a") == 1
    assert content_validation_count(pair.conn, "run-b") == 1


def test_content_version_is_shared_but_observations_are_not(pair) -> None:
    """正文文本全局只存一份（内容版本可跨 run 共用），按 run 隔离的只是观察行。"""
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    first = add_body(pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=1)
    b_kw, b_seq, _ = add_cards(pair.b, minute=0)
    second = add_body(pair.b, keyword_id=b_kw, seq=b_seq, note=note, minute=1)
    assert first.content_version_id == second.content_version_id
    assert first.post_id == second.post_id
    assert first.observation_id != second.observation_id


def test_replaying_the_same_attempt_writes_no_second_observation(pair) -> None:
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    keyword_text = KeywordText(raw=WORD, normalized=WORD)
    operation = pair.repo.open_operation(
        pair.a.run_id, a_kw, OperationType.body, a_seq, branch_key=BRANCH
    )
    attempt = pair.repo.start_attempt(operation, 1)
    result = PostBodyResult(
        state=ResultState.completed,
        context=OperationContext(
            run_id=pair.a.run_id,
            keyword_id=a_kw,
            keyword=keyword_text,
            operation_type=OperationType.body,
            revisit_seq=a_seq,
            attempt_no=1,
        ),
        session=session_for("sess-replay", at(3)),
        body="正文内容",
        content_version=ContentVersionRef(text="正文内容", text_sha256=sha256_text("正文内容")),
    )
    first = ingest_body(
        pair.repo, pair.a.run_id, result, note_id=note, operation_id=operation, attempt_id=attempt
    )
    again = ingest_body(
        pair.repo, pair.a.run_id, result, note_id=note, operation_id=operation, attempt_id=attempt
    )
    assert first.created is True and again.created is False
    assert again.observation_id == first.observation_id
    assert len(rows(pair.conn)) == 1
    assert content_validation_count(pair.conn, "run-a") == 1
    # 库级兜底：绕过入库层直写同一 attempt 也进不去（唯一索引落在三列来源上）
    with pytest.raises(
        sqlite3.IntegrityError,
        match="body_observations.operation_id, body_observations.attempt_id,"
        " body_observations.session_id",
    ):
        with pair.repo.write():
            pair.conn.execute(
                "INSERT INTO body_observations(post_id, state, body_is_empty, content_version_id,"
                " captured_at, run_id, operation_id, attempt_id, session_id)"
                " VALUES (?, 'completed', NULL, ?, ?, 'run-a', ?, ?, 'sess-replay')",
                (
                    first.post_id,
                    first.content_version_id,
                    at(3).isoformat(),
                    operation,
                    attempt,
                ),
            )


# ------------------------------------------------------------------ 归属核对


def test_provenance_mismatch_is_refused_before_any_write(pair) -> None:
    """跨 run、错类型、attempt 不属于该操作、空会话：一律拒绝，且库里一字未动。"""
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    add_body(pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=1)
    b_kw, b_seq, _ = add_cards(pair.b, minute=0)
    before = rows(pair.conn)
    counts = {
        run: content_validation_count(pair.conn, run) for run in ("run-a", "run-b")
    }
    body_operation = int(before[0]["operation_id"])
    body_attempt = int(before[0]["attempt_id"])

    result = PostBodyResult(
        state=ResultState.completed,
        context=OperationContext(
            run_id=pair.b.run_id,
            keyword_id=pair.b.keyword_id,
            keyword=KeywordText(raw=WORD, normalized=WORD),
            operation_type=OperationType.body,
            revisit_seq=1,
            attempt_no=1,
        ),
        session=session_for("sess-x", at(9)),
        body="正文内容",
        content_version=ContentVersionRef(text="正文内容", text_sha256=sha256_text("正文内容")),
    )

    with pytest.raises(ValueError, match="属于 run run-a，观察却要写进 run-b"):
        ingest_body(
            pair.repo,
            pair.b.run_id,
            result,
            note_id=note,
            operation_id=body_operation,
            attempt_id=body_attempt,
        )

    search_operation = int(
        pair.conn.execute(
            "SELECT id FROM operations WHERE run_id = 'run-b' AND operation_type = 'search'"
        ).fetchone()["id"]
    )
    search_attempt = int(
        pair.conn.execute(
            "SELECT id FROM operation_attempts WHERE operation_id = ?", (search_operation,)
        ).fetchone()["id"]
    )
    with pytest.raises(ValueError, match="类型是 search，观察却说是 body"):
        ingest_body(
            pair.repo,
            pair.b.run_id,
            result,
            note_id=note,
            operation_id=search_operation,
            attempt_id=search_attempt,
        )

    # attempt 属于另一个操作：run-b 自己的 body 操作，配 A 那次 body 尝试
    foreign_operation = pair.repo.open_operation(
        pair.b.run_id, b_kw, OperationType.body, b_seq, branch_key=BRANCH
    )
    with pytest.raises(ValueError, match=r"尝试 \d+ 属于操作 \d+，不是操作"):
        ingest_body(
            pair.repo,
            pair.b.run_id,
            result,
            note_id=note,
            operation_id=foreign_operation,
            attempt_id=body_attempt,
        )

    # 空会话：三列幂等键缺一即失效，宁可拒绝也不给出一条无法去重的观察
    foreign_attempt = pair.repo.start_attempt(foreign_operation, 1)
    blank_session = result.model_copy(update={"session": session_for(" ", at(9))})
    with pytest.raises(ValueError, match="非空 session_id"):
        ingest_body(
            pair.repo,
            pair.b.run_id,
            blank_session,
            note_id=note,
            operation_id=foreign_operation,
            attempt_id=foreign_attempt,
        )

    assert rows(pair.conn) == before, "拒绝不得留下半行观察"
    assert {
        run: content_validation_count(pair.conn, run) for run in ("run-a", "run-b")
    } == counts
    assert pair.conn.execute("SELECT COUNT(*) AS n FROM budget_ledger").fetchone()["n"] == 0


def test_unknown_operation_and_attempt_are_refused(pair) -> None:
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    result = PostBodyResult(
        state=ResultState.completed,
        context=OperationContext(
            run_id=pair.a.run_id,
            keyword_id=a_kw,
            keyword=KeywordText(raw=WORD, normalized=WORD),
            operation_type=OperationType.body,
            revisit_seq=a_seq,
            attempt_no=1,
        ),
        session=session_for("sess-x", at(4)),
        body="正文内容",
        content_version=ContentVersionRef(text="正文内容", text_sha256=sha256_text("正文内容")),
    )
    with pytest.raises(ValueError, match="操作不存在：4242"):
        ingest_body(
            pair.repo, pair.a.run_id, result, note_id=note, operation_id=4242, attempt_id=1
        )
    operation = pair.repo.open_operation(
        pair.a.run_id, a_kw, OperationType.body, a_seq, branch_key=BRANCH
    )
    with pytest.raises(ValueError, match="尝试 4242 不存在"):
        ingest_body(
            pair.repo,
            pair.a.run_id,
            result,
            note_id=note,
            operation_id=operation,
            attempt_id=4242,
        )
    assert rows(pair.conn) == []


# ------------------------------------------------------------------ 迁移兼容


def test_legacy_unattributed_rows_survive_upgrade_and_do_not_count(tmp_path) -> None:
    """v2 老库里的正文观察升级后原样保留（来源 NULL），但一律不计入当前 run。"""
    conn = storage_db.connect(tmp_path / "old.db")
    repo = Repository(conn)
    repo.create_run(build_run_identity("run-a"), config_json='{"budget": {}}')
    repo.ensure_branch("run-a", BRANCH, label="主分支")
    settings = budget_settings()
    env = StopEnv(
        repo=repo,
        run_id="run-a",
        settings=settings,
        ledger=Ledger.from_settings(conn, "run-a", settings),
        keyword_id=repo.ensure_keyword(WORD, WORD, seen_at=T0),
    )
    assert migrations.current_version(conn) == migrations.HEAD_VERSION

    # 退到 v2：body_observations 变回没有来源列的形状，post_queries 也还没有卡片可见字段列，
    # 分析层新表 content_family_members 整体不存在。升级回来的层必须与退掉的层一字不差，
    # 否则"历史行保留"只证明了一半 schema。
    assert migrations.rollback(conn, to_version=2) == (8, 7, 6, 5, 4, 3)
    assert "run_id" not in [
        row["name"] for row in conn.execute("PRAGMA table_info(body_observations)")
    ]
    assert "active_revisit_seq" not in [
        row["name"] for row in conn.execute("PRAGMA table_info(frontier)")
    ], "v8 的活动重探序号也要真退掉（升级/回滚对称，C22）"
    assert "published_at_text" not in [
        row["name"] for row in conn.execute("PRAGMA table_info(post_queries)")
    ]
    assert "title_version_id" not in [
        row["name"] for row in conn.execute("PRAGMA table_info(post_queries)")
    ], "v7 的观察标题版本引用也要真退掉（升级/回滚对称，C-15）"
    assert "content_family_members" not in migrations.table_names(conn)
    assert "analysis_version" not in [
        row["name"] for row in conn.execute("PRAGMA table_info(phrases)")
    ], "v5 的短语版本列也要真退掉"
    assert "post_observation_id" not in [
        row["name"] for row in conn.execute("PRAGMA table_info(evidence)")
    ], "v6 的证据观察锚点也要真退掉"

    keyword_id, seq, note = add_legacy_cards_v2(env, minute=0)
    post_id = repo.post_id(note)
    assert post_id is not None
    version_id = repo.ensure_content_version(post_id, "body", "老库正文", captured_at=at(1))
    with repo.write():
        conn.execute(
            "INSERT INTO body_observations(post_id, state, body_is_empty, content_version_id,"
            " captured_at) VALUES (?, 'completed', NULL, ?, ?)",
            (post_id, version_id, at(1).isoformat()),
        )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM post_queries WHERE run_id = 'run-a'"
    ).fetchone()["n"] == 1, "v2 层照样写得出卡片与 post_queries"

    assert migrations.apply_migrations(conn) == (3, 4, 5, 6, 7, 8)
    legacy = rows(conn)
    assert len(legacy) == 1, "历史行不得被迁移删掉"
    assert legacy[0]["run_id"] is None and legacy[0]["operation_id"] is None
    upgraded = conn.execute(
        "SELECT published_at_text, likes_text, title_version_id"
        " FROM post_queries WHERE run_id = 'run-a'"
    ).fetchone()
    assert (upgraded["published_at_text"], upgraded["likes_text"]) == (None, None), (
        "升级只补列，绝不替历史卡片伪造页面当时显示的文本"
    )
    assert upgraded["title_version_id"] is None, (
        "v7 升级不替历史行猜当时看见的是哪一版标题（C-15）"
    )
    assert (
        content_validation_count(conn, "run-a") == 0
    ), "归属无法确定的观察不能事后编一个 run 算进收敛"

    # 本 run 自己重新取一次正文：计数只由新行贡献
    add_body(env, keyword_id=keyword_id, seq=seq + 100, note=note, minute=2)
    assert content_validation_count(conn, "run-a") == 1
    assert len(rows(conn)) == 2


def test_row_with_run_but_mismatched_operation_run_is_not_counted(pair) -> None:
    """两重来源必须自洽：行上写 run-b、操作属于 run-a 的错位数据不计数（也不静默采信）。"""
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    ingest = add_body(pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=1)
    with pair.repo.write():
        pair.conn.execute(
            "UPDATE body_observations SET run_id = 'run-b' WHERE id = ?",
            (ingest.observation_id,),
        )
    assert content_validation_count(pair.conn, "run-a") == 0
    assert content_validation_count(pair.conn, "run-b") == 0


# ------------------------------------------------------------------ 收敛反例


def converge(env: StopEnv, *, with_body: bool, minute: int = 0) -> None:
    """tiny 配置下的完整收敛配方：8 次成功观察 + 停滞 + 队列清空（+ 正文）。"""
    if with_body:
        env.content_validation(minute=minute)
    for index in range(8):
        env.suggest(("上海中考志愿",), minute=index + 1)
    for _ in range(4):
        env.reward(clusters=0, evidence_ids=[])
    frontier_id = env.repo.enqueue(
        env.run_id, env.keyword_id, BRANCH, score=0.1, selection_reason="待办"
    )
    env.repo.set_frontier_status(frontier_id, "done")


def test_run_cannot_converge_on_another_runs_body(pair) -> None:
    """A 取到过正文，B 只有同篇帖子的卡片：B 其余条件全备也不许 converged。"""
    a_kw, a_seq, note = add_cards(pair.a, minute=0)
    add_body(pair.a, keyword_id=a_kw, seq=a_seq, note=note, minute=0)
    assert content_validation_count(pair.conn, "run-a") == 1

    b_kw, b_seq, _ = add_cards(pair.b, minute=0)
    add_body(
        pair.b,
        keyword_id=b_kw,
        seq=b_seq,
        note=note,
        minute=1,
        state=ResultState.failed,
        body=None,
        error=ErrorInfo(
            code="WORD_PAGE_TIMEOUT",
            message="正文超时",
            category=ErrorCategory.network,
            retryable=True,
            human_action_required=False,
        ),
    )
    converge(pair.b, with_body=False, minute=20)
    failures = convergence_failures(pair.b.signals(), pair.b.settings)
    assert any("内容验证" in reason for reason in failures), failures
    decision = evaluate(pair.b.repo, pair.b.run_id, pair.b.settings, pair.b.ledger)
    assert decision.outcome is not TerminalOutcome.converged

    # B 自己取到正文之后，同一配方才允许收敛
    add_body(pair.b, keyword_id=b_kw, seq=b_seq + 100, note=note, minute=30)
    assert content_validation_count(pair.conn, "run-b") == 1
    decision = evaluate(pair.b.repo, pair.b.run_id, pair.b.settings, pair.b.ledger)
    assert decision.status is RunStatus.completed
    assert decision.outcome is TerminalOutcome.converged
