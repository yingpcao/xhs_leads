"""P0-D-C15-r2：观察标题版本不许用 captured_at 猜——重放、语料与 S 信号统一走显式引用（C-15）。

`post_content_versions` 按 `(post_id, field, text_sha256)` 去重是**内容层**事实；
"这次卡片观察实际看见哪一版标题"是**观察层**事实。标题 A→B→A 时第三次观察复用 A 的旧行
（`captured_at` 仍停在第一次），凡按"观察时刻前最新版本"推断的读法都会把第三次读成 B
（Codex 探针两例：递增时间与同时间不同会话）。本文件钉住：

- schema v7 给 `post_queries` 补可空 `title_version_id` 显式引用：升级只补列、历史行留 NULL
  不伪造，回滚真删列、行不丢（升级/回滚对称）；
- `_ingest_cards` 写入当次引用；`replayed_posts`、`corpus.title_units`（走同一个重放）与
  `scoring._title_visible_sources` 统一走 `title_version_for_observation`——不许只修展示层；
- 同帖同文本的第二次观察命中同一版本行：不改写旧版本的 `captured_at` 来迁就新观察；
- 历史 NULL 引用只做保守推断：取观察时刻之前已捕获的版本，绝不借未来值，更早于一切版本的
  观察就是无标题；
- 缺失标题保留引用但不产出文本；跨 run 共享帖子时各 run 的观察各自正确；
- 幂等导入、恢复重放与首次入库逐字一致；A/B/A 的会话/名次信号归给**实际看见的那个版本**
  （C-13 聚合口径不因版本回放错认而反转让不相干的版本蹭会话）。
"""

from __future__ import annotations

from types import SimpleNamespace

from xhs_discovery.analysis.export import EXPORT_FILENAMES
from xhs_discovery.analysis.corpus import load_corpus
from xhs_discovery.discovery.ingest import ingest_search, replayed_posts
from xhs_discovery.models.enums import OperationType, ResultState, SourceField
from xhs_discovery.models.source import AuthorRef, KeywordText, PostCard, PostSearchResult
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage import migrations
from xhs_discovery.storage.repository import Repository

from tests.conftest import T0, build_run_identity
from tests.test_analysis_clustering import HELP_QUESTION, cluster, explicit_of
from tests.test_analysis_extraction import (
    KEYWORD,
    at,
    context,
    open_op,
    prepare,
    rows,
    session,
)
from tests.test_analysis_pipeline import run as run_pipeline
from tests.test_analysis_scoring import json_of, score

NOTE = "review-aba"
EDITED_QUESTION = "志愿填报怎么改"
TITLE_A = "名额分配怎么申请？"
TITLE_B = "校额到校怎么申请？"
URL = f"https://www.xiaohongshu.com/explore/{NOTE}"


def card(index: int, title: str | None, *, missing: bool = False) -> PostCard:
    return PostCard(
        note_id=NOTE,
        access_url=URL,
        title=title,
        title_missing=missing,
        author=AuthorRef(kind="public_ref", public_ref=f"u-{NOTE}"),
        published_at_text="2026-09-01",
        published_at_missing=False,
        likes_text="12",
        result_rank=1,
        search_keyword=KEYWORD,
    )


def observe_versions(env, keyword_id: int, entries: list, *, prefix: str, same_time: bool = False):
    """按 revisit 序号逐次入库卡片观察：entries 为标题文本或 ('missing', None) 形态。

    每观察一个独立 session（同时间不同会话用 same_time 关掉递增）。返回
    (observation_id, collected_at.isoformat(), 期望标题或 None) 列表与最后一次 ingest 结果。
    """
    facts = []
    last = None
    for index, entry in enumerate(entries):
        missing = isinstance(entry, tuple)
        title = None if missing else str(entry)
        operation_id, attempt_id = open_op(env, keyword_id, OperationType.search, revisit=index)
        captured = session(f"{prefix}-{index}", 0 if same_time else index)
        last = ingest_search(
            env.repo,
            env.run_id,
            PostSearchResult(
                state=ResultState.completed,
                context=context(env, keyword_id, OperationType.search, revisit=index),
                session=captured,
                items=[card(index, None if missing else entry, missing=missing)],
            ),
            operation_id=operation_id,
            attempt_id=attempt_id,
        )
        facts.append((int(last.observation_id), captured.collected_at.isoformat(), title, last))
    return facts


def replay_titles(env, facts) -> list[tuple[int | None, str | None]]:
    return [
        (posts[0].title_version_id, posts[0].title)
        for observation_id, stamp, _expected, _ingest in facts
        for posts in [replayed_posts(env.conn, observation_id, stamp)]
    ]


# ------------------------------------------------------------------ 入库与重放


def test_aba_replay_returns_each_observation_own_version(storage) -> None:
    """A→B→A：第三次当场是 A，重放也必须是 A；版本行不新增、旧 captured_at 不改写。"""
    keyword_id = prepare(storage)
    facts = observe_versions(storage, keyword_id, [TITLE_A, TITLE_B, TITLE_A], prefix="aba")
    assert [ingest.posts[0].title for _o, _s, _t, ingest in facts] == [TITLE_A, TITLE_B, TITLE_A]
    replayed = replay_titles(storage, facts)
    assert [title for _vid, title in replayed] == [TITLE_A, TITLE_B, TITLE_A]
    assert replayed[0][0] == replayed[2][0] != replayed[1][0], (
        "第一次与第三次看见的是同一版（同一版本行），第二次是另一版"
    )
    versions = rows(
        storage.conn,
        "SELECT id, text, captured_at FROM post_content_versions WHERE field = 'title' ORDER BY id",
    )
    assert len(versions) == 2, "复用旧版本行，不再为第三次观察新建第二份 A"
    assert versions[0]["captured_at"] == at(0).isoformat(), (
        "不许把 A 的 captured_at 改成第三次来'修'回放——那会破坏第一次观察"
    )
    stored = rows(
        storage.conn,
        "SELECT title_version_id FROM post_queries WHERE run_id = ? ORDER BY id",
        (storage.run_id,),
    )
    assert [row["title_version_id"] for row in stored] == [
        replayed[0][0],
        replayed[1][0],
        replayed[0][0],
    ], "当次引用的版本 id 必须落库，重放不是现场再猜一次"


def test_same_time_different_sessions_keep_their_own_versions(storage) -> None:
    """三次观察同一时刻：captured_at 全并排，任何"取最新"的猜测都失去依据，只有显式引用可信。"""
    keyword_id = prepare(storage)
    facts = observe_versions(
        storage, keyword_id, [TITLE_A, TITLE_B, TITLE_A], prefix="same", same_time=True
    )
    replayed = replay_titles(storage, facts)
    assert [title for _vid, title in replayed] == [TITLE_A, TITLE_B, TITLE_A]
    assert replayed[0][0] == replayed[2][0] != replayed[1][0]


def test_idempotent_reimport_replays_the_same_facts(storage) -> None:
    """崩溃后补导入：命中幂等键返回已有行，三次观察重放仍是 A、B、A，不写第二行。"""
    keyword_id = prepare(storage)
    facts = observe_versions(storage, keyword_id, [TITLE_A, TITLE_B, TITLE_A], prefix="idem")
    before = rows(
        storage.conn, "SELECT id, title_version_id FROM post_queries ORDER BY id"
    )
    for index, (observation_id, stamp, expected, _ingest) in enumerate(facts):
        operation_id, attempt_id = open_op(storage, keyword_id, OperationType.search, revisit=index)
        again = ingest_search(
            storage.repo,
            storage.run_id,
            PostSearchResult(
                state=ResultState.completed,
                context=context(storage, keyword_id, OperationType.search, revisit=index),
                session=session(f"idem-{index}", index),
                items=[card(index, [TITLE_A, TITLE_B, TITLE_A][index])],
            ),
            operation_id=operation_id,
            attempt_id=attempt_id,
        )
        assert again.created is False
        assert [post.title for post in again.posts] == [expected]
        assert [post.title_version_id for post in again.posts] == [before[index]["title_version_id"]]
        assert again.observation_id == observation_id
    assert rows(
        storage.conn, "SELECT id, title_version_id FROM post_queries ORDER BY id"
    ) == before


def test_missing_title_observation_keeps_reference_without_text(storage) -> None:
    """标题缺失是一次真实观察：引用落在 is_missing 行上，重放读出 None，不借相邻版本的文本。"""
    keyword_id = prepare(storage)
    facts = observe_versions(storage, keyword_id, [TITLE_A, ("missing",), TITLE_B], prefix="miss")
    replayed = replay_titles(storage, facts)
    assert [title for _vid, title in replayed] == [TITLE_A, None, TITLE_B]
    missing_row = rows(
        storage.conn,
        "SELECT v.is_missing FROM post_queries pq JOIN post_content_versions v ON v.id = pq.title_version_id"
        " JOIN post_observations po ON po.id = pq.post_observation_id"
        " WHERE pq.run_id = ? AND po.observed_at = ? AND v.field = 'title' AND v.is_missing = 1",
        (storage.run_id, facts[1][1]),
    )
    assert len(missing_row) == 1, "缺失观察的引用必须指向 is_missing 版本行"


# ------------------------------------------------------------------ 老库兼容（NULL 引用）


def test_v7_upgrade_leaves_legacy_rows_without_a_reference(tmp_path) -> None:
    """v6 库真实升级：只补列、历史行 NULL；回滚删列、行不丢（升级/回滚对称）。"""
    conn = storage_db.connect(tmp_path / "v6.db")
    assert migrations.rollback(conn, to_version=6) == (8, 7), "回滚到 v6 只撤 v7、v8 两层"
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(post_queries)")}
    assert "title_version_id" not in columns
    repo = Repository(conn, migrate=False)
    identity = build_run_identity("run-v6")
    repo.create_run(identity, config_json='{"budget": {}}')
    keyword_id = repo.ensure_keyword(HELP_QUESTION, HELP_QUESTION, seen_at=T0)
    post_id = repo.ensure_post("legacy-post", "https://www.xiaohongshu.com/explore/legacy-post", seen_at=T0)
    version_id = repo.ensure_content_version(post_id, "title", HELP_QUESTION, captured_at=T0)
    operation_id = repo.open_operation("run-v6", keyword_id, OperationType.search, 0)
    attempt_id = repo.start_attempt(operation_id, 1)
    with repo.write():
        cursor = conn.execute(
            "INSERT INTO post_observations(run_id, operation_id, attempt_id, session_id,"
            " keyword_id, observed_at, state, card_count, source_name, source_version, synthetic)"
            " VALUES ('run-v6', ?, ?, 's-legacy', ?, ?, 'completed', 1, 'fixture', '0.0.1', 1)",
            (operation_id, attempt_id, keyword_id, T0.isoformat()),
        )
        observation_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO post_queries(run_id, post_observation_id, post_id, keyword_id, result_rank)"
            " VALUES ('run-v6', ?, ?, ?, 1)",
            (observation_id, post_id, keyword_id),
        )
    conn.commit()

    assert migrations.apply_migrations(conn) == (7, 8)
    upgraded = conn.execute(
        "SELECT title_version_id FROM post_queries WHERE run_id = 'run-v6'"
    ).fetchone()
    assert upgraded["title_version_id"] is None, "历史行不许事后补一个猜出来的引用"
    rolled = migrations.rollback(conn, to_version=6)
    assert rolled == (8, 7)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM post_queries WHERE run_id = 'run-v6'"
    ).fetchone()["n"] == 1, "回滚删列不删行"
    assert migrations.apply_migrations(conn) == (7, 8)
    conn.close()


def test_legacy_null_reference_only_uses_the_past_never_the_future(storage) -> None:
    """老行（引用 NULL）兼容口径：观察时刻前唯一候选才可推断，多候选一律 unknown，绝不借未来值。

    r2 曾把"多候选取最新"当成确定事实——A→B→A 退化成无引用的历史行后，第三次 A 观察会被
    确定地回放成"最新"的 B。r3 修正：只有该时刻前**唯一**已捕获版本才作可解释推断，多个候选
    无法证明这次看见的是哪一版，一律 unknown；corpus/评分见 unknown 即跳过，不作确定语料/证据。
    """
    keyword_id = prepare(storage)
    facts = observe_versions(storage, keyword_id, [TITLE_A, TITLE_B, TITLE_A], prefix="legacy")
    versions = rows(
        storage.conn,
        "SELECT id, text FROM post_content_versions WHERE field = 'title' ORDER BY captured_at, id",
    )
    version_a = next(row["id"] for row in versions if row["text"] == TITLE_A)
    # 把三次观察退化成 v6 时代没有引用的历史行
    storage.conn.execute(
        "UPDATE post_queries SET title_version_id = NULL WHERE run_id = ?", (storage.run_id,)
    )
    storage.conn.commit()
    replayed = replay_titles(storage, facts)
    assert replayed == [(version_a, TITLE_A), (None, None), (None, None)], (
        "t0 只有 A 已捕获 → 唯一候选可推断为 A；t1/t2 时 A、B 都在候选里 → unknown，"
        "不许把第三次 A 观察确定地猜成最新的 B"
    )
    assert replayed[2][1] != TITLE_B, "多候选不许'取最新'冒充当次观察的事实"
    # 早于一切版本捕获的时刻：承认当时没有可见标题，不借未来版本填空
    early = replayed_posts(storage.conn, facts[0][0], T0.isoformat())
    # observed_at == A 的 captured_at（同刻算已捕获），提前一分钟则什么都不该有
    assert early[0].title == TITLE_A
    none_before = replayed_posts(storage.conn, facts[0][0], "2026-10-24T11:59:00+00:00")
    assert none_before[0].title is None and none_before[0].title_version_id is None


def test_legacy_upgrade_aba_unknown_reference_never_becomes_a_false_corpus_or_session(storage) -> None:
    """真实升级反例：NULL 引用的 A/B/A 历史观察，第三次不得进入语料、也不得给 B 蹭会话。

    钉住 Codex `2026-10-27-c15-legacy-codex.py` 的语义：历史不确定引用不能做成确定语料/证据/S 会话。
    """
    keyword_id = prepare(storage)
    facts = observe_versions(
        storage, keyword_id, [HELP_QUESTION, EDITED_QUESTION, HELP_QUESTION], prefix="legacy-aba"
    )
    storage.conn.execute(
        "UPDATE post_queries SET title_version_id = NULL WHERE run_id = ?", (storage.run_id,)
    )
    storage.conn.commit()
    # 语料：只有第一次观察（唯一候选 HELP）成标题语料；后两次多候选 → unknown，不落别帖事实
    corpus = load_corpus(storage.conn, storage.run_id)
    title_units = corpus.by_source(SourceField.title)
    assert [unit.text for unit in title_units] == [HELP_QUESTION], (
        "无唯一引用的历史观察不产出确定标题语料，第三次 A 不得被回放成 B 的编辑问题"
    )
    assert all(unit.text != EDITED_QUESTION for unit in title_units), (
        "EDITED 版只在第二次观察被'最新'猜中过，来源不确定时不得成为语料事实"
    )
    cluster(storage)
    score(storage)
    # 评分：第三次 A 观察 unknown → 不认领，B 会话不因此膨胀到 2；HELP 会话也不得被错记到 EDITED
    signals = {
        text: payload["signals"]
        for text, payload in (
            (need["canonical_text"], json_of(need)) for need in explicit_of(storage)
        )
    }
    help_sessions = next(
        sig["sessions"] for text, sig in signals.items() if HELP_QUESTION in text
    )
    assert help_sessions == 1, "第一次观察唯一 → HELP 会话 1；第三次不确定，不复制证据来抬计数"


# ------------------------------------------------------------------ 跨 run 与语料/评分统一


def test_cross_run_shared_post_keeps_each_runs_observations_correct(storage) -> None:
    """两个 run 观察同一篇帖子：身份与版本行全局共用，各 run 的重放各自回各自看见的版本。"""
    keyword_id = prepare(storage)
    facts_a = observe_versions(storage, keyword_id, [TITLE_A, TITLE_B, TITLE_A], prefix="run-a")

    other_repo = Repository(storage.conn, migrate=False)
    identity = build_run_identity("run-c15-other")
    other_repo.create_run(identity, config_json='{"budget": {}}')
    other = SimpleNamespace(conn=storage.conn, repo=other_repo, run_id=identity.run_id)
    other_keyword = other_repo.ensure_keyword(
        KEYWORD.normalized, KEYWORD.raw, seen_at=T0
    )
    other_repo.upsert_run_keyword(
        identity.run_id, other_keyword, depth=0, origin="seed", status="queued", seen_at=T0
    )
    facts_b = observe_versions(other, other_keyword, [TITLE_B, TITLE_A], prefix="run-b")

    assert [title for _vid, title in replay_titles(storage, facts_a)] == [TITLE_A, TITLE_B, TITLE_A]
    assert [title for _vid, title in replay_titles(other, facts_b)] == [TITLE_B, TITLE_A]
    assert (
        len(rows(storage.conn, "SELECT id FROM posts WHERE note_id = ?", (NOTE,))) == 1
    ), "帖子身份全局只有一份"
    assert (
        len(rows(storage.conn, "SELECT id FROM post_content_versions WHERE field = 'title'")) == 2
    ), "标题版本按文本去重也是全局一份"


def test_corpus_title_units_point_at_the_version_actually_seen(storage) -> None:
    """语料层与重放同口径：三条标题语料是 A、B、A，第一、三条指回同一版本行。"""
    keyword_id = prepare(storage)
    observe_versions(storage, keyword_id, [TITLE_A, TITLE_B, TITLE_A], prefix="corpus")
    corpus = load_corpus(storage.conn, storage.run_id)
    units = corpus.by_source(SourceField.title)
    assert [unit.text for unit in units] == [TITLE_A, TITLE_B, TITLE_A]
    assert units[0].content_version_id == units[2].content_version_id
    assert units[1].content_version_id != units[0].content_version_id
    assert len({unit.observation_id for unit in units}) == 3


def test_scoring_sessions_attribute_to_the_version_actually_seen(storage) -> None:
    """S 信号不许版本错认：第三次 A 观察给 A 的需求添会话，不给 B 蹭——旧推断会整体反转。"""
    keyword_id = prepare(storage)
    observe_versions(storage, keyword_id, [HELP_QUESTION, EDITED_QUESTION, HELP_QUESTION], prefix="score")
    cluster(storage)
    score(storage)
    needs = {need["canonical_text"]: json_of(need)["signals"] for need in explicit_of(storage)}
    original = next(signals for text, signals in needs.items() if HELP_QUESTION in text)
    edited = next(signals for text, signals in needs.items() if EDITED_QUESTION in text)
    assert original["sessions"] == 2, "第一次与第三次观察都看见 HELP 版：两个会话都算它的"
    assert edited["sessions"] == 1, "改版只被第二次观察看见，不借第三次 A 观察的会话"
    evidence = rows(
        storage.conn,
        "SELECT source_field, content_version_id, COUNT(*) AS n FROM evidence"
        " WHERE run_id = ? AND source_field = 'title' GROUP BY content_version_id",
        (storage.run_id,),
    )
    assert [row["n"] for row in evidence] == [1, 1], (
        "同一版本的第二次观察不复制证据行（C-13 边界不因引用改造而松动）"
    )


def test_full_analysis_graph_and_export_on_an_aba_library(storage) -> None:
    """整链验证：真实 ingest → replay/corpus → 抽取/评分 → 双图 → 导出，A/B/A 全程各自正确。"""
    keyword_id = prepare(storage)
    observe_versions(storage, keyword_id, [HELP_QUESTION, EDITED_QUESTION, HELP_QUESTION], prefix="pipe")
    outcome = run_pipeline(storage)
    assert len(outcome.export.files) == len(EXPORT_FILENAMES)
    assert outcome.demand_graph is not None and outcome.integrity is not None
    titles = {
        str(row["text"])
        for row in rows(
            storage.conn,
            "SELECT v.text FROM evidence e JOIN post_content_versions v ON v.id = e.content_version_id"
            " WHERE e.run_id = ? AND e.source_field = 'title'",
            (storage.run_id,),
        )
    }
    assert titles == {HELP_QUESTION, EDITED_QUESTION}, "证据钉在被实际看见的那个版本上"
    score_jsons = [json_of(need) for need in explicit_of(storage)]
    signals = {
        ("help" if HELP_QUESTION in need["canonical_text"] else "edited"): payload["signals"]
        for need, payload in zip(explicit_of(storage), score_jsons)
    }
    assert signals["help"]["sessions"] == 2 and signals["edited"]["sessions"] == 1
    # 幂等重放：同版本再来一次分析，业务行不新增（P0-C 历史与预算不回归由全套钉住）
    counts_before = len(rows(storage.conn, "SELECT id FROM evidence WHERE run_id = ?", (storage.run_id,)))
    again = run_pipeline(storage, snapshot="c15-r2-replay")
    assert again is not outcome
    assert (
        len(rows(storage.conn, "SELECT id FROM evidence WHERE run_id = ?", (storage.run_id,)))
        == counts_before
    )
