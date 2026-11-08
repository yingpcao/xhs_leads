"""v4 卡片可见字段：页面当时显示什么，库里就存什么，重放与恢复读回同一份。

为什么这属于 P0-D 的前置而不是"顺手补的列"：
- F（近期持续性）要按发布日期判断"这个问题最近还在不在被提"，Q/A 要区分独立来源。
  卡片发布时间与点赞此前只在内存里过一遍就丢了，分析层只能凭帖子身份编日期，
  或者把"页面没给"和"分析算不出"混成同一个 NULL——两者都是造假。
- 点赞数是页面可见文本（`"1.2万"` 这种写法），不是搜索量也不是人数。库里存原文，
  展示口径才守得住 DATA_MODEL 第 8 节；要数值化是分析层的事，而且要带着"这是文本"的证据。
- 这些字段属于**某次关键词结果页里的这篇帖子**，不属于全局帖子身份：同篇帖子在两次观察里
  点赞不同，写进 `posts` 就会互相覆盖，所以落在 `post_queries`。
"""

from __future__ import annotations

import sqlite3

import pytest

from xhs_discovery.discovery.ingest import ingest_author_posts, ingest_search, replayed_posts
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.enums import OperationType, ResultState
from xhs_discovery.models.source import (
    AuthorPostsResult,
    AuthorRef,
    KeywordText,
    OperationContext,
    PostCard,
    PostSearchResult,
)

from tests.conftest import T0

WORD = "上海中考"
KEYWORD = KeywordText(raw=WORD, normalized=WORD)


def card_factory(
    rank: int,
    *,
    likes: str | None = "1.2万",
    published: str | None = "2026-09-01",
    title: str | None = "上海中考名额分配怎么申请",
):
    note = f"n-visible-{rank}"
    return PostCard(
        note_id=note,
        access_url=f"https://www.xiaohongshu.com/explore/{note}",
        title=title,
        title_missing=title is None,
        author=AuthorRef(kind="public_ref", public_ref=f"u-{note}"),
        published_at_text=published,
        published_at_missing=published is None,
        likes_text=likes,
        result_rank=rank,
        search_keyword=KEYWORD,
    )


def search_result(
    storage, cards: list[PostCard], *, keyword_id: int, revisit: int = 0, attempt_no: int = 1,
    session_id: str = "sess-v-1",
):
    return PostSearchResult(
        state=ResultState.completed,
        context=OperationContext(
            run_id=storage.run_id,
            keyword_id=keyword_id,
            keyword=KEYWORD,
            operation_type=OperationType.search,
            revisit_seq=revisit,
            attempt_no=attempt_no,
        ),
        session=SessionRef(
            session_id=session_id,
            collected_at=T0,
            source_name="fixture",
            source_version="0.0.1",
            synthetic=True,
        ),
        items=cards,
    )


def observe(
    storage, cards: list[PostCard], *, revisit: int = 0, attempt_no: int = 1,
    session_id: str = "sess-v-1",
):
    keyword_id = storage.repo.ensure_keyword(WORD, WORD, seen_at=T0)
    storage.repo.upsert_run_keyword(
        storage.run_id, keyword_id, depth=0, origin="seed", status="queued", seen_at=T0
    )
    operation_id = storage.repo.open_operation(
        storage.run_id, keyword_id, OperationType.search, revisit, branch_key="b-main"
    )
    attempt_id = storage.repo.start_attempt(operation_id, attempt_no)
    ingest = ingest_search(
        storage.repo,
        storage.run_id,
        search_result(
            storage, cards, keyword_id=keyword_id, revisit=revisit,
            attempt_no=attempt_no, session_id=session_id,
        ),
        operation_id=operation_id,
        attempt_id=attempt_id,
    )
    return ingest, operation_id, attempt_id


def pq_row(storage, note_id: str) -> sqlite3.Row:
    return storage.conn.execute(
        "SELECT pq.published_at_text, pq.likes_text FROM post_queries pq"
        " JOIN posts p ON p.id = pq.post_id WHERE p.note_id = ? AND pq.run_id = ?",
        (note_id, storage.run_id),
    ).fetchone()


def test_visible_fields_land_on_the_query_row_not_the_global_post(storage) -> None:
    """点赞/发布日期按"这次结果页里的这篇帖子"存：同篇帖子两次观察互不覆盖。"""
    observe(storage, [card_factory(1)])
    row = pq_row(storage, "n-visible-1")
    assert (row["published_at_text"], row["likes_text"]) == ("2026-09-01", "1.2万")
    assert "likes_text" not in {
        r["name"] for r in storage.conn.execute("PRAGMA table_info(posts)")
    }, "点赞不是帖子身份的一部分，别把它写进全局表"


def test_page_text_is_stored_verbatim_and_never_invented(storage) -> None:
    """页面没给就是 NULL：缺失标记必须原样落到列上，分析层才分得清"没显示"与"算不出"。"""
    observe(storage, [card_factory(1, likes=None)])
    observe(storage, [card_factory(2, published=None, likes="3")], session_id="sess-v-2", revisit=1)
    first = pq_row(storage, "n-visible-1")
    assert (first["published_at_text"], first["likes_text"]) == ("2026-09-01", None)
    second = pq_row(storage, "n-visible-2")
    assert (second["published_at_text"], second["likes_text"]) == (None, "3")


def test_replay_reads_back_the_same_visible_facts(storage) -> None:
    """同 attempt 重放必须与首次入库逐字相同——恢复路径与共用的 replayed_posts 是同一段代码。"""
    ingest, operation_id, attempt_id = observe(storage, [card_factory(1), card_factory(2)])
    fresh = {post.note_id: (post.published_at_text, post.likes_text) for post in ingest.posts}
    cards = [card_factory(1), card_factory(2)]
    replay = ingest_search(
        storage.repo,
        storage.run_id,
        search_result(storage, cards, keyword_id=1),
        operation_id=operation_id,
        attempt_id=attempt_id,
    )
    assert replay.created is False
    assert {post.note_id: (post.published_at_text, post.likes_text) for post in replay.posts} == fresh
    assert all(post.published_at_text == "2026-09-01" for post in replay.posts), (
        "重放把可见字段读成 None 的话，恢复出来的观察就和当初落库的不是同一件事实"
    )


def test_replayed_posts_orders_by_rank_and_carries_the_versions(storage) -> None:
    observe(storage, [card_factory(1), card_factory(2)])
    observation_id = int(
        storage.conn.execute(
            "SELECT id FROM post_observations WHERE run_id = ? ORDER BY id", (storage.run_id,)
        ).fetchone()["id"]
    )
    posts = replayed_posts(storage.conn, observation_id, T0.isoformat())
    assert [post.result_rank for post in posts] == [1, 2]
    assert [post.published_at_text for post in posts] == ["2026-09-01", "2026-09-01"]
    assert [post.title_version_id for post in posts] == [
        row["id"]
        for row in storage.conn.execute(
            "SELECT id FROM post_content_versions WHERE field = 'title' ORDER BY id"
        )
    ], '证据要能指回"当时看到的那一版标题"'


def test_author_posts_path_stores_no_query_row(storage) -> None:
    """作者列表不是关键词结果页，没有 post_queries 行可挂：可见字段随观察留在内存里即可。"""
    keyword_id = storage.repo.ensure_keyword(WORD, WORD, seen_at=T0)
    storage.repo.upsert_run_keyword(
        storage.run_id, keyword_id, depth=0, origin="seed", status="queued", seen_at=T0
    )
    operation_id = storage.repo.open_operation(
        storage.run_id, keyword_id, OperationType.author_posts, 0, branch_key="b-main"
    )
    storage.repo.start_attempt(operation_id, 1)
    result = AuthorPostsResult(
        state=ResultState.completed,
        context=OperationContext(
            run_id="run-a",
            keyword_id=keyword_id,
            keyword=KEYWORD,
            operation_type=OperationType.author_posts,
            revisit_seq=0,
            attempt_no=1,
        ),
        session=SessionRef(
            session_id="sess-v-author",
            collected_at=T0,
            source_name="fixture",
            source_version="0.0.1",
            synthetic=True,
        ),
        items=[card_factory(1)],
    )
    ingest = ingest_author_posts(storage.repo, storage.run_id, result)
    assert [(p.published_at_text, p.likes_text) for p in ingest.posts] == [("2026-09-01", "1.2万")]
    assert storage.conn.execute(
        "SELECT COUNT(*) AS n FROM post_queries"
    ).fetchone()["n"] == 0


@pytest.mark.parametrize("likes", ["128", "1.2万", "赞", None])
def test_likes_text_keeps_every_page_spelling(storage, likes: str | None) -> None:
    """点赞原文有十种写法：库里保存看到的写法，不做数值猜测。"""
    note = "n-visible-1"
    observe(storage, [card_factory(1, likes=likes)])
    assert pq_row(storage, note)["likes_text"] == likes
