"""观察入库（P0-14 的前置）：把数据源返回落成 v1 观察层表，重复导入不再新建身份。

为什么必须由这一层统一写：
- 幂等键是 `(operation_id, attempt_id, session_id)`。session_id 由逻辑键派生，
  所以"重放同一次操作"与"崩溃后补导入"都命中已有行，不会把一篇帖子写成两条身份；
- 失败/不支持/跳过同样要留观察行（item_count=0），否则看板上的失败原因无处可查，
  预算已扣减却没有对应记录；
- 联想子词同时写三处：`suggestion_items`（平台原序）、`suggestion_versions`（内容版本，
  证据要能指回"当时看到的那一版"）、`discovery_edges`+`run_keywords`（发现图）。
  少写任何一处，后面的评分、证据定位与跨 run 复用都会静默失真。

`run_keywords` 的父节点只在首次发现时写入（ON CONFLICT 只更新 last_seen_at/status），
多父关系完整保存在 `discovery_edges`——这与 DATA_MODEL 第 4 节一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.enums import CandidateOrigin, OperationType, ResultState
from xhs_discovery.models.source import (
    AuthorPostsResult,
    PostBodyResult,
    PostCard,
    PostSearchResult,
    SuggestionResult,
)
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.normalize import KeywordNormalizer

# 只有这两种状态携带"可解析部分"；failed/unsupported/skipped 的观察行 item_count 恒为 0
ITEM_BEARING_STATES = (ResultState.completed, ResultState.partial)


@dataclass(frozen=True)
class IngestedChild:
    rank: int
    keyword_id: int
    raw: str
    normalized: str
    is_new_in_run: bool


@dataclass(frozen=True)
class SuggestionIngest:
    observation_id: int
    created: bool
    state: ResultState
    children: tuple[IngestedChild, ...]
    self_loop_ranks: tuple[int, ...] = ()

    @property
    def new_children(self) -> tuple[IngestedChild, ...]:
        return tuple(child for child in self.children if child.is_new_in_run)


@dataclass(frozen=True)
class IngestedPost:
    note_id: str
    post_id: int
    result_rank: int
    title: str | None
    title_version_id: int | None
    author_id: int | None
    author_public_ref: str | None
    published_at_text: str | None
    likes_text: str | None
    access_url: str
    is_new_in_run: bool


@dataclass(frozen=True)
class PostIngest:
    observation_id: int | None
    created: bool
    state: ResultState
    posts: tuple[IngestedPost, ...]

    @property
    def stored_post_count(self) -> int:
        return sum(1 for post in self.posts if post.is_new_in_run)


@dataclass(frozen=True)
class BodyIngest:
    observation_id: int
    created: bool
    state: ResultState
    post_id: int
    content_version_id: int | None
    body_is_empty: bool
    run_id: str
    operation_id: int
    attempt_id: int
    session_id: str


def _one(conn, sql: str, params: tuple) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else dict(row)


def _require_keyword(repo: Repository, run_id: str, keyword_id: int | None) -> int:
    if keyword_id is None:
        raise ValueError("入库前必须已取得 keyword_id：预入库身份只用于数据源逻辑键，不能落库")
    if not repo.require_keyword_registered(run_id, keyword_id):
        raise ValueError(f"keyword_id={keyword_id} 不存在于全局身份层")
    return keyword_id


def _visible_title_version(conn, post_id: int, observed_at: str) -> tuple[int | None, str | None]:
    """历史 NULL/失效引用的保守口径：只有观察时刻前**唯一**已捕获版本才可作可解释推断。

    "过去出现过"不等于"这次看见过"。A→B→A 升级后第三次观察复用 A 的旧行（`captured_at`
    停在第一次），观察时刻前同时摆着 A、B 两版，任何"取最新"都只是猜——猜中的 B 会把一次
    A 观察钉成 B 的确定语料/证据/S 会话（C-15 r3）。所以候选多于一个就一律 unknown；不改写
    旧版本 `captured_at`、不伪造引用，留空比编造诚实（DATA_MODEL 第 9.6 节）。
    """
    rows = conn.execute(
        "SELECT id, text, is_missing FROM post_content_versions"
        " WHERE post_id = ? AND field = 'title' AND captured_at <= ?"
        " ORDER BY captured_at, id",
        (post_id, observed_at),
    ).fetchall()
    if len(rows) != 1:
        return None, None
    row = rows[0]
    title = None if int(row["is_missing"]) == 1 else str(row["text"])
    return int(row["id"]), title


def ingest_suggestion(
    repo: Repository,
    run_id: str,
    result: SuggestionResult,
    *,
    normalizer: KeywordNormalizer,
    operation_id: int,
    attempt_id: int,
    branch_key: str | None = None,
) -> SuggestionIngest:
    """写入一次联想观察；重复导入直接返回已有行，不再建第二份身份或第二条边。"""
    if result.context.operation_type is not OperationType.suggest:
        raise ValueError("ingest_suggestion 只接受 suggest 结果")
    conn = repo.conn
    parent_id = _require_keyword(repo, run_id, result.context.keyword_id)
    session = result.session
    observed_at = session.collected_at.isoformat()
    existing = _one(
        conn,
        "SELECT id, state FROM suggestion_observations"
        " WHERE operation_id = ? AND attempt_id = ? AND session_id = ?",
        (operation_id, attempt_id, session.session_id),
    )
    if existing is not None:
        observation_id = int(existing["id"])
        rows = conn.execute(
            "SELECT rank, keyword_id, text_raw, text_normalized FROM suggestion_items"
            " WHERE observation_id = ? ORDER BY rank",
            (observation_id,),
        ).fetchall()
        return SuggestionIngest(
            observation_id=observation_id,
            created=False,
            state=ResultState(existing["state"]),
            children=tuple(
                IngestedChild(
                    rank=int(row["rank"]),
                    keyword_id=int(row["keyword_id"]),
                    raw=str(row["text_raw"]),
                    normalized=str(row["text_normalized"]),
                    is_new_in_run=False,
                )
                for row in rows
            ),
        )

    items = list(result.items) if result.state in ITEM_BEARING_STATES else []
    branch_id = None if branch_key is None else repo.branch_id(run_id, branch_key)
    parent_row = _one(
        conn,
        "SELECT depth FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
        (run_id, parent_id),
    )
    child_depth = (int(parent_row["depth"]) if parent_row else 0) + 1

    with repo.write():
        cursor = conn.execute(
            "INSERT INTO suggestion_observations(run_id, operation_id, attempt_id, session_id,"
            " keyword_id, observed_at, state, item_count, source_name, source_version, synthetic)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                operation_id,
                attempt_id,
                session.session_id,
                parent_id,
                observed_at,
                result.state.value,
                len(items),
                session.source_name,
                session.source_version,
                1 if session.synthetic else 0,
            ),
        )
        observation_id = int(cursor.lastrowid)

    children: list[IngestedChild] = []
    self_loops: list[int] = []
    for item in items:
        norm = normalizer.normalize(item.text)
        keyword_id = repo.ensure_keyword(
            norm.normalized,
            norm.raw,
            seen_at=session.collected_at,
            is_ambiguous_seed=norm.is_ambiguous_seed,
        )
        with repo.write():
            conn.execute(
                "INSERT INTO suggestion_items(observation_id, rank, text_raw, text_normalized,"
                " keyword_id) VALUES (?, ?, ?, ?, ?)",
                (observation_id, item.rank, norm.raw, norm.normalized, keyword_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO suggestion_versions(observation_id, rank, text,"
                " text_sha256, captured_at) VALUES (?, ?, ?, ?, ?)",
                (observation_id, item.rank, norm.raw, sha256_text(norm.raw), observed_at),
            )
        if keyword_id == parent_id:
            # 平台把自己联想回自己：观察原样保留，但不能成为发现边（schema 禁止自环，
            # 且自环会让深度与父链计算陷入死循环）
            self_loops.append(item.rank)
            continue
        repo.record_discovery_edge(
            run_id,
            parent_id,
            keyword_id,
            origin=CandidateOrigin.observed,
            rank=item.rank,
            session_id=session.session_id,
            operation_id=operation_id,
            observed_at=session.collected_at,
        )
        already_known = _one(
            conn,
            "SELECT id FROM run_keywords WHERE run_id = ? AND keyword_id = ?",
            (run_id, keyword_id),
        )
        if already_known is None:
            repo.upsert_run_keyword(
                run_id,
                keyword_id,
                parent_keyword_id=parent_id,
                depth=child_depth,
                branch_id=branch_id,
                origin=CandidateOrigin.observed.value,
                status="queued",
                seen_at=session.collected_at,
            )
        children.append(
            IngestedChild(
                rank=item.rank,
                keyword_id=keyword_id,
                raw=norm.raw,
                normalized=norm.normalized,
                is_new_in_run=already_known is None,
            )
        )
    return SuggestionIngest(
        observation_id=observation_id,
        created=True,
        state=result.state,
        children=tuple(children),
        self_loop_ranks=tuple(self_loops),
    )


def _ingest_cards(
    repo: Repository,
    run_id: str,
    cards: list[PostCard],
    *,
    seen_at: datetime,
    keyword_id: int | None,
    post_observation_id: int | None,
) -> list[IngestedPost]:
    posts: list[IngestedPost] = []
    for card in cards:
        author_id = repo.ensure_author(public_ref=card.author.public_ref, kind=card.author.kind)
        known = repo.post_id(card.note_id) is not None
        post_id = repo.ensure_post(
            card.note_id, card.access_url, seen_at=seen_at, author_id=author_id
        )
        title_version_id = repo.ensure_content_version(
            post_id, "title", card.title, captured_at=seen_at, is_missing=card.title_missing
        )
        published_at_text = None if card.published_at_missing else card.published_at_text
        if post_observation_id is not None and keyword_id is not None:
            with repo.write():
                repo.conn.execute(
                    "INSERT OR IGNORE INTO post_queries(run_id, post_observation_id, post_id,"
                    " keyword_id, result_rank, published_at_text, likes_text,"
                    " title_version_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        post_observation_id,
                        post_id,
                        keyword_id,
                        card.result_rank,
                        published_at_text,
                        card.likes_text,
                        title_version_id,
                    ),
                )
        posts.append(
            IngestedPost(
                note_id=card.note_id,
                post_id=post_id,
                result_rank=card.result_rank,
                title=None if card.title_missing else (card.title or None),
                title_version_id=title_version_id,
                author_id=author_id,
                author_public_ref=card.author.public_ref,
                published_at_text=published_at_text,
                likes_text=card.likes_text,
                access_url=card.access_url,
                is_new_in_run=not known,
            )
        )
    return posts


def _version_title(conn, version_id: int) -> tuple[int | None, str | None]:
    """按显式版本引用读出 (version_id, title)：is_missing 行没有标题文本，但引用本身保留。"""
    row = _one(
        conn,
        "SELECT text, is_missing FROM post_content_versions WHERE id = ? AND field = 'title'",
        (version_id,),
    )
    if row is None:
        return None, None
    title = None if int(row["is_missing"]) == 1 else str(row["text"])
    return version_id, title


def title_version_for_observation(
    conn, post_id: int, observed_at: str, title_version_id: int | None
) -> tuple[int | None, str | None]:
    """一次卡片观察实际对应的标题版本——重放、语料与 S 信号共用的唯一口径。

    有显式引用（v7 起的 `post_queries.title_version_id`）就按引用取：版本表按文本哈希去重，
    "A→B→A"的第三次观察复用 A 的旧行、旧 captured_at，用"观察时刻前最新版本"猜会把 A 读成 B
    （C-15）。引用为 NULL 或引用行不可读的老库历史行不再"取最新"：只有观察时刻前**唯一**已捕获
    版本才可解释性推断，多个候选无法证明这次看见的是哪一版，一律 unknown——绝不把猜测做成确定
    语料/证据/S 会话，也不改写旧版本 captured_at、不伪造指针（DATA_MODEL 第 9.6 节）。
    """
    if title_version_id is not None:
        version_id, title = _version_title(conn, int(title_version_id))
        if version_id is not None:
            return version_id, title
    return _visible_title_version(conn, post_id, observed_at)


def replayed_posts(conn, observation_id: int, observed_at: str) -> tuple[IngestedPost, ...]:
    """从库重建一次卡片观察：可见字段必须和首次入库读回同一份，否则重放与恢复会得到不同事实。

    `published_at_text`/`likes_text` 是页面当时显示的原文：分析层要据此算 F（近期持续性）
    并明确"点赞数只是可见文本，不是搜索量"。它们只落在 `post_queries`（某次关键词结果页里
    这篇帖子显示成什么样），不进全局 `posts` 身份表——同一篇帖子的点赞数每次都可能不同。

    标题走 `title_version_for_observation`：显式观察版本引用，历史 NULL 引用才保守推断（C-15）。
    """
    rows = conn.execute(
        "SELECT pq.post_id, pq.result_rank, pq.published_at_text, pq.likes_text,"
        " pq.title_version_id,"
        " p.note_id, p.access_url, p.author_id, a.public_ref FROM post_queries pq"
        " JOIN posts p ON p.id = pq.post_id LEFT JOIN authors a ON a.id = p.author_id"
        " WHERE pq.post_observation_id = ? ORDER BY pq.result_rank",
        (observation_id,),
    ).fetchall()
    posts: list[IngestedPost] = []
    for row in rows:
        post_id = int(row["post_id"])
        title_version_id, title = title_version_for_observation(
            conn, post_id, observed_at, row["title_version_id"]
        )
        posts.append(
            IngestedPost(
                note_id=str(row["note_id"]),
                post_id=post_id,
                result_rank=int(row["result_rank"]),
                title=title,
                title_version_id=title_version_id,
                author_id=None if row["author_id"] is None else int(row["author_id"]),
                author_public_ref=row["public_ref"],
                published_at_text=row["published_at_text"],
                likes_text=row["likes_text"],
                access_url=str(row["access_url"]),
                is_new_in_run=False,
            )
        )
    return tuple(posts)


def ingest_search(
    repo: Repository,
    run_id: str,
    result: PostSearchResult,
    *,
    operation_id: int,
    attempt_id: int,
) -> PostIngest:
    if result.context.operation_type is not OperationType.search:
        raise ValueError("ingest_search 只接受 search 结果")
    conn = repo.conn
    keyword_id = _require_keyword(repo, run_id, result.context.keyword_id)
    session = result.session
    observed_at = session.collected_at.isoformat()
    existing = _one(
        conn,
        "SELECT id, state, observed_at FROM post_observations"
        " WHERE operation_id = ? AND attempt_id = ? AND session_id = ?",
        (operation_id, attempt_id, session.session_id),
    )
    if existing is not None:
        observation_id = int(existing["id"])
        return PostIngest(
            observation_id=observation_id,
            created=False,
            state=ResultState(existing["state"]),
            posts=replayed_posts(conn, observation_id, str(existing["observed_at"])),
        )
    cards = list(result.items) if result.state in ITEM_BEARING_STATES else []
    with repo.write():
        cursor = conn.execute(
            "INSERT INTO post_observations(run_id, operation_id, attempt_id, session_id,"
            " keyword_id, observed_at, state, card_count, source_name, source_version, synthetic)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                operation_id,
                attempt_id,
                session.session_id,
                keyword_id,
                observed_at,
                result.state.value,
                len(cards),
                session.source_name,
                session.source_version,
                1 if session.synthetic else 0,
            ),
        )
        observation_id = int(cursor.lastrowid)
    posts = _ingest_cards(
        repo,
        run_id,
        cards,
        seen_at=session.collected_at,
        keyword_id=keyword_id,
        post_observation_id=observation_id,
    )
    return PostIngest(
        observation_id=observation_id, created=True, state=result.state, posts=tuple(posts)
    )


def ingest_author_posts(repo: Repository, run_id: str, result: AuthorPostsResult) -> PostIngest:
    """作者公开笔记列表：只落帖子身份与标题版本。

    这里刻意不写 `post_observations`/`post_queries`——那张表表达的是"某关键词的结果页"，
    作者列表不是关键词结果，混写会让"这个词召回了哪些帖子"的统计失真。
    """
    if result.context.operation_type is not OperationType.author_posts:
        raise ValueError("ingest_author_posts 只接受 author_posts 结果")
    cards = list(result.items) if result.state in ITEM_BEARING_STATES else []
    posts = _ingest_cards(
        repo,
        run_id,
        cards,
        seen_at=result.session.collected_at,
        keyword_id=result.context.keyword_id,
        post_observation_id=None,
    )
    return PostIngest(observation_id=None, created=True, state=result.state, posts=tuple(posts))


def ingest_body(
    repo: Repository,
    run_id: str,
    result: PostBodyResult,
    *,
    note_id: str,
    operation_id: int,
    attempt_id: int,
) -> BodyIngest:
    """正文观察：一次读取属于哪个 run、哪次操作、第几次尝试，必须写在行上。

    幂等键与联想/卡片观察同口径，用 `(operation_id, attempt_id, session_id)`（库里
    `idx_body_observation_attempt` 唯一索引兜底）。此前用 `(post_id, captured_at)`：
    帖子身份是全局的，fixture 的时间戳又只由重探序号与尝试号派生，两个 run 抓同一篇帖子
    会得到同一个 captured_at，于是 run B 要么借到 run A 的观察（`created=False`），
    要么在收敛计数里把 A 的正文算成自己的内容验证量（C-09）。

    正文**内容版本**（`post_content_versions`）仍然全局共用，那是同一份文本；
    按 run 隔离的是"我取到过它"这条观察。老库里没有来源的历史行一律不再被这里认领，
    也不计入收敛——归属未知的行宁可留着承认查不到，不能事后编一个来源。
    """
    if result.context.operation_type is not OperationType.body:
        raise ValueError("ingest_body 只接受 body 结果")
    conn = repo.conn
    # 先核对来源事实再写入：跨 run、错类型、attempt 不属于该操作一律拒绝，且不留半行
    repo.require_observation_source(
        operation_id,
        attempt_id,
        run_id=run_id,
        operation_type=OperationType.body,
        session_id=result.session.session_id,
    )
    post_id = repo.post_id(note_id)
    if post_id is None:
        raise ValueError(f"正文入库前帖子必须已登记：{note_id}")
    session = result.session
    captured_at = session.collected_at.isoformat()
    existing = _one(
        conn,
        "SELECT id, state, content_version_id, body_is_empty FROM body_observations"
        " WHERE operation_id = ? AND attempt_id = ? AND session_id = ?",
        (operation_id, attempt_id, session.session_id),
    )
    if existing is not None:
        return BodyIngest(
            observation_id=int(existing["id"]),
            created=False,
            state=ResultState(existing["state"]),
            post_id=post_id,
            content_version_id=(
                None
                if existing["content_version_id"] is None
                else int(existing["content_version_id"])
            ),
            body_is_empty=bool(existing["body_is_empty"]),
            run_id=run_id,
            operation_id=operation_id,
            attempt_id=attempt_id,
            session_id=session.session_id,
        )
    content_version_id: int | None = None
    body_is_empty = False
    if result.state is ResultState.completed:
        if result.body is None:
            if not result.body_is_empty:
                raise ValueError("completed 且无正文必须显式声明 body_is_empty")
            body_is_empty = True
            content_version_id = repo.ensure_content_version(
                post_id, "body", None, captured_at=session.collected_at, is_missing=True
            )
        else:
            text = result.content_version.text if result.content_version else result.body
            content_version_id = repo.ensure_content_version(
                post_id, "body", text, captured_at=session.collected_at
            )
    with repo.write():
        cursor = conn.execute(
            "INSERT INTO body_observations(post_id, state, body_is_empty, content_version_id,"
            " captured_at, run_id, operation_id, attempt_id, session_id) VALUES (?, ?, ?, ?,"
            " ?, ?, ?, ?, ?)",
            (
                post_id,
                result.state.value,
                1 if body_is_empty else None,
                content_version_id,
                captured_at,
                run_id,
                operation_id,
                attempt_id,
                session.session_id,
            ),
        )
        observation_id = int(cursor.lastrowid)
    return BodyIngest(
        observation_id=observation_id,
        created=True,
        state=result.state,
        post_id=post_id,
        content_version_id=content_version_id,
        body_is_empty=body_is_empty,
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        session_id=session.session_id,
    )


__all__ = [
    "BodyIngest",
    "IngestedChild",
    "IngestedPost",
    "ITEM_BEARING_STATES",
    "PostIngest",
    "SuggestionIngest",
    "ingest_author_posts",
    "ingest_body",
    "ingest_search",
    "ingest_suggestion",
    "replayed_posts",
    "title_version_for_observation",
]
