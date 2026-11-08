"""P0-D-C13-r1：评分聚合粒度——S 的观察信号与 Q 的帖子计数（C-13 / C-14）。

钉住两件事：

- **C-13**：证据事实与观察信号是两层。同一 run 里同一事实被第二个会话再次观察，
  证据行仍只有一条（不复制证据/帖子/家族来抬 S），但 S 的会话/路径/名次必须从
  **真实观察记录**聚合，并且只认"当时可见这一内容版本"的观察：别的会话、别的关键词、
  别的内容版本、别的 run 都不许串进来；重复导入同一会话不新增信号。
- **C-14**：Q 是"表达明确问题的去重公开帖子数"，与内容家族抑制联立：
  同帖多 field/多版本不抬 Q，不同帖复制同一内容仍被家族并查抑制；
  原帖子数、家族数、最终 Q 计数与抑制原因在 signals/needs.csv 分开可审计，
  标题与正文两份家族这类**审计事实**必须保留。

C-12 的发布时间解析锚点与评分窗口口径原样延续（见 tests/test_analysis_published_anchor.py）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.corpus import load_corpus
from xhs_discovery.analysis.export import NEED_COLUMNS, export_run, parse_csv
from xhs_discovery.analysis.scoring import DemandScorer
from xhs_discovery.discovery.ingest import ingest_body, ingest_search
from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.enums import OperationType, ResultState, SourceField
from xhs_discovery.models.source import (
    AuthorRef,
    ContentVersionRef,
    KeywordText,
    PostBodyResult,
    PostCard,
    PostSearchResult,
)
from xhs_discovery.storage.repository import Repository

from tests.conftest import T0, build_run_identity
from tests.test_analysis_clustering import HELP_QUESTION, cluster, explicit_of
from tests.test_analysis_extraction import (
    KEYWORD,
    at,
    context,
    note_id_for,
    observe_body,
    observe_titles,
    open_op,
    prepare,
    rows,
    session,
)
from tests.test_analysis_scoring import Card, json_of, observe_cards, score, single_explicit

WORD2 = "上海中考 分数线"
KEYWORD2 = KeywordText(raw=WORD2, normalized=WORD2)
FILLER = "上海中考冲刺资料整理"
EDITED_QUESTION = "志愿填报怎么改"
BODY_QUESTION = "名额分配到底要什么条件？"
BODY_NEEDLE = "名额分配到底要什么条件"


def make_keyword(storage, raw: str, normalized: str) -> int:
    keyword_id = storage.repo.ensure_keyword(normalized, raw, seen_at=T0)
    storage.repo.upsert_run_keyword(
        storage.run_id, keyword_id, depth=1, origin="observed", status="queued", seen_at=T0
    )
    return keyword_id


def observe_page(
    storage, keyword_id: int, entries: list[tuple[str, str]], *,
    keyword: KeywordText = KEYWORD, session_id: str = "s-1", minutes: int = 0,
):
    """自定义一页卡片结果：(note, title) 按给定顺序出名次——版本编辑与名次反例都靠它。"""
    cards = [
        PostCard(
            note_id=note,
            access_url=f"https://www.xiaohongshu.com/explore/{note}",
            title=title,
            author=AuthorRef(kind="public_ref", public_ref=f"u-{note}"),
            published_at_text="2026-09-01",
            published_at_missing=False,
            likes_text="12",
            result_rank=rank,
            search_keyword=keyword,
        )
        for rank, (note, title) in enumerate(entries, start=1)
    ]
    operation_id, attempt_id = open_op(storage, keyword_id, OperationType.search)
    return ingest_search(
        storage.repo,
        storage.run_id,
        PostSearchResult(
            state=ResultState.completed,
            context=context(storage, keyword_id, OperationType.search),
            session=session(session_id, minutes),
            items=cards,
        ),
        operation_id=operation_id,
        attempt_id=attempt_id,
    )


def fetch_body(
    storage, keyword_id: int, note: str, body: str, *,
    session_id: str = "s-2", minutes: int = 1, attempt_no: int = 1,
):
    operation_id, attempt_id = open_op(
        storage, keyword_id, OperationType.body, revisit=1, attempt_no=attempt_no
    )
    result = PostBodyResult(
        state=ResultState.completed,
        context=context(storage, keyword_id, OperationType.body, revisit=1, attempt_no=attempt_no),
        session=session(session_id, minutes),
        body=body,
        content_version=ContentVersionRef(
            content_version_id=None, text=body, text_sha256=sha256_text(body)
        ),
    )
    return ingest_body(
        storage.repo, storage.run_id, result, note_id=note,
        operation_id=operation_id, attempt_id=attempt_id,
    )


def need_by(storage, needle: str) -> dict:
    for need in explicit_of(storage):
        if needle in need["canonical_text"]:
            return need
    raise AssertionError(f"没有找到包含 {needle!r} 的显性需求行")


# ------------------------------------------------------------------ C-13：S 观察信号


def test_the_same_fact_seen_in_two_sessions_lifts_s_without_copying_evidence(storage) -> None:
    """同 run 同词同版本，两次观察来自两个会话：证据 1 行、家族 1 件、会话信号 2。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="review-session-a", minutes=0)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="review-session-b", minutes=5)
    cluster(storage)
    score(storage)
    evidence = rows(storage.conn, "SELECT id, session_id FROM evidence WHERE run_id = ?", (storage.run_id,))
    assert len(evidence) == 1, "同事实第二次观察不复制证据行"
    payload = json_of(single_explicit(storage))
    signals = payload["signals"]
    assert signals["evidence_rows"] == 1
    assert signals["sessions"] == 2, "跨会话稳定性必须看见真实的两次会话"
    assert signals["evidence_row_sessions"] == 1, "事实层仍只有首次观察的会话"
    assert signals["parent_paths"] == 1 == signals["evidence_row_parent_paths"]
    assert signals["content_families"] == 1 and signals["distinct_posts"] == 1
    assert payload["score"]["features"]["S"]["raw"] == 3.0, "sessions+paths = 2+1"
    assert payload["score"]["features"]["Q"]["raw"] == 1.0, "S 涨了 Q 不跟涨"
    before = dict(single_explicit(storage))
    score(storage)
    assert dict(single_explicit(storage)) == before, "观察聚合确定且幂等"


def test_repeated_import_of_the_same_session_adds_no_stability(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="s-1", minutes=0)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="s-1", minutes=9)
    cluster(storage)
    run = score(storage)
    signals = json_of(single_explicit(storage))["signals"]
    assert signals["sessions"] == 1, "同会话重复导入是幂等重放，不是第二次会话"
    assert run.outcome and signals["evidence_rows"] == 1


def test_a_second_keyword_path_and_session_both_trace_to_observations(storage) -> None:
    """多父路径：同帖同版本被第二个关键词召回，paths 记 2，且库里两条召回都可回查。"""
    keyword_id = prepare(storage)
    second_keyword = make_keyword(storage, WORD2, WORD2)
    note = note_id_for(HELP_QUESTION)
    observe_page(storage, keyword_id, [(note, HELP_QUESTION)], session_id="s-a", minutes=0)
    observe_page(
        storage, second_keyword, [(note, HELP_QUESTION)],
        keyword=KEYWORD2, session_id="s-b", minutes=10,
    )
    cluster(storage)
    score(storage)
    payload = json_of(single_explicit(storage))
    signals = payload["signals"]
    assert signals["sessions"] == 2
    assert signals["parent_paths"] == 2
    assert signals["evidence_row_parent_paths"] == 1, "事实层仍钉在首次召回的关键词上"
    assert signals["distinct_posts"] == 1 and signals["q_counted_posts"] == 1
    recalled = {
        int(row["keyword_id"])
        for row in rows(
            storage.conn,
            "SELECT DISTINCT keyword_id FROM post_queries WHERE run_id = ? AND post_id = ("
            " SELECT id FROM posts WHERE note_id = ?)",
            (storage.run_id, note),
        )
    }
    assert recalled == {keyword_id, second_keyword}


def test_observations_of_other_content_versions_never_lift_sessions_or_rank(storage) -> None:
    """帖子改版反例：v1 的会话与名次不借 v2 的观察，v2 也不借 v1 的名次。"""
    keyword_id = prepare(storage)
    note = note_id_for(HELP_QUESTION)
    observe_page(
        storage, keyword_id,
        [(note_id_for(FILLER), FILLER), (f"pad-{note}", "志愿填报经验贴"), (note, HELP_QUESTION)],
        session_id="s-a", minutes=0,
    )
    edited_note = note
    observe_page(
        storage, keyword_id,
        [(edited_note, EDITED_QUESTION), (note_id_for(FILLER), FILLER)],
        session_id="s-b", minutes=30,
    )
    cluster(storage)
    score(storage)
    old = json_of(need_by(storage, HELP_QUESTION))["signals"]
    new = json_of(need_by(storage, EDITED_QUESTION))["signals"]
    assert old["sessions"] == 1 and old["evidence_rows"] == 1
    assert old["best_rank"] == 3, "改版后的第 1 名不属于 v1 的观察"
    assert new["sessions"] == 1 and new["best_rank"] == 1


def test_another_run_observations_are_not_borrowed(storage) -> None:
    """跨 run 不串线：run B 观察同一帖子同一版本，run A 的会话信号与证据行数不动。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="s-a", minutes=0)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="s-b", minutes=5)
    cluster(storage)
    score(storage)
    assert json_of(single_explicit(storage))["signals"]["sessions"] == 2

    other_conn = storage.conn
    other_repo = Repository(other_conn, migrate=False)
    identity = build_run_identity("run-c13-other")
    other_repo.create_run(identity, config_json='{"budget": {}}')
    other = SimpleNamespace(conn=other_conn, repo=other_repo, run_id=identity.run_id)
    other_keyword = prepare(other)
    observe_titles(other, other_keyword, [HELP_QUESTION], session_id="s-c", minutes=30)
    cluster(other)
    score(other)

    assert json_of(single_explicit(other))["signals"]["sessions"] == 1
    score(storage)
    a_signals = json_of(single_explicit(storage))["signals"]
    assert a_signals["sessions"] == 2, "s-c 是别的 run 的观察，不进本 run 的稳定性信号"
    assert len(rows(other_conn, "SELECT id FROM evidence WHERE run_id = ?", (storage.run_id,))) == 1


def test_body_observations_count_sessions_and_position_stays_conservative(storage) -> None:
    """正文-only 需求：两次读取会话都算；卡片早于正文捕获时位置保持 unknown，不猜。"""
    keyword_id = prepare(storage)
    note = f"body-{note_id_for(BODY_QUESTION)}"
    observe_page(storage, keyword_id, [(note, FILLER)], session_id="s-card", minutes=0)
    fetch_body(storage, keyword_id, note, BODY_QUESTION, session_id="s-read-a", minutes=10)
    fetch_body(
        storage, keyword_id, note, BODY_QUESTION, session_id="s-read-b", minutes=20, attempt_no=2
    )
    cluster(storage)
    score(storage)
    signals = json_of(need_by(storage, BODY_NEEDLE))["signals"]
    assert signals["evidence_rows"] == 1
    assert signals["sessions"] == 2, "两次正文读取来自两个会话"
    assert signals["evidence_row_sessions"] == 1
    assert signals["best_rank"] is None and signals["position_signal"] is None
    assert signals["distinct_posts"] == 1 and signals["q_counted_posts"] == 1
    # 正文版本捕获**之后**的卡片观察才允许带名次进来
    observe_page(
        storage, keyword_id,
        [("*pad*", "踩坑记录"), (note, FILLER)],
        session_id="s-card-late", minutes=30,
    )
    cluster(storage)
    score(storage)
    late = json_of(need_by(storage, BODY_NEEDLE))["signals"]
    assert late["best_rank"] == 2 and late["sessions"] == 2, "迟到卡片给出名次，会话数不再涨"


def test_body_source_aggregation_requires_run_and_attempt_self_consistency(storage) -> None:
    """C-15 r3：正文来源聚合必须 run/operation/attempt 自洽，别 run 借来的操作与挂错尝试都不认领。

    只按 `bo.run_id` 过滤 + `JOIN op.id` 取关键词会漏掉两条归属约束：别的 run 的操作借本 run 的
    run_id 写一行、或 attempt 实际挂在另一个操作上——都会被错记成本 run 的会话/路径信号。语料正文
    来源与评分正文来源共用同一自洽口径。
    """
    keyword_id = prepare(storage)
    note = f"prov-{note_id_for(BODY_QUESTION)}"
    observe_page(storage, keyword_id, [(note, FILLER)], session_id="s-card", minutes=0)
    valid = fetch_body(storage, keyword_id, note, BODY_QUESTION, session_id="s-valid", minutes=10)
    content_version_id = valid.content_version_id
    post_id = storage.repo.post_id(note)
    cluster(storage)

    other_repo = Repository(storage.conn, migrate=False)
    other_identity = build_run_identity("run-c15-prov-other")
    other_repo.create_run(other_identity, config_json='{"budget": {}}')
    foreign_op = other_repo.open_operation(other_identity.run_id, keyword_id, OperationType.body, 0)
    foreign_att = other_repo.start_attempt(foreign_op, 1)

    op_y, _att_y = open_op(storage, keyword_id, OperationType.body, revisit=2, attempt_no=1)

    with storage.repo.write():
        storage.conn.execute(
            "INSERT INTO body_observations(post_id, state, content_version_id, captured_at,"
            " run_id, operation_id, attempt_id, session_id)"
            " VALUES (?, 'completed', ?, ?, ?, ?, ?, 's-borrowed')",
            (post_id, content_version_id, at(15).isoformat(), storage.run_id, foreign_op, foreign_att),
        )
        storage.conn.execute(
            "INSERT INTO body_observations(post_id, state, content_version_id, captured_at,"
            " run_id, operation_id, attempt_id, session_id)"
            " VALUES (?, 'completed', ?, ?, ?, ?, ?, 's-mismatch')",
            (post_id, content_version_id, at(16).isoformat(), storage.run_id, op_y, foreign_att),
        )

    sources = DemandScorer(storage.repo, storage.run_id)._body_observation_sources(content_version_id)
    assert {sid for sid, _kw in sources} == {"s-valid"}, (
        "评分正文来源只认领自洽的本 run 观察，借来的操作/挂错的尝试都不进 S"
    )
    corpus = load_corpus(storage.conn, storage.run_id)
    body_sessions = {
        unit.session_id
        for unit in corpus.by_source(SourceField.body)
        if unit.content_version_id == content_version_id
    }
    assert body_sessions == {"s-valid"}, "语料正文来源同口径，不落成别 run/错尝试的事实"


# ------------------------------------------------------------------ C-14：Q 帖子计数


def test_same_post_title_and_body_keep_two_families_but_q_counts_one_post(storage) -> None:
    """同帖标题+正文都问同一句：两份家族是审计事实必须保留，Q 仍按一篇帖子计。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, HELP_QUESTION)
    cluster(storage)
    run = score(storage)
    payload = json_of(single_explicit(storage))
    signals = payload["signals"]
    assert signals["evidence_rows"] == 2, "两个 field 的原文证据都保留"
    assert {row["source_field"] for row in rows(
        storage.conn, "SELECT source_field FROM evidence WHERE run_id = ?", (storage.run_id,)
    )} == {"title", "body"}
    assert {family.field_name for family in run.outcome.families} == {"title", "body"}
    assert signals["distinct_posts"] == 1
    assert signals["content_families"] == 2, "家族审计不许被改写成帖子数"
    assert signals["q_counted_posts"] == 1
    assert signals["q_post_absorbed_families"] == 1
    assert signals["q_family_suppressed_posts"] == 0
    assert signals["duplicate_versions_suppressed"] == 0
    assert signals["q_suppression_reasons"] == ["same_post_multiple_fields"]
    assert payload["score"]["features"]["Q"]["raw"] == 1.0
    inferred = json_of(
        rows(storage.conn, "SELECT * FROM needs WHERE run_id = ? AND statement_kind = 'inferred_need'"
                           " AND analysis_version = ? ORDER BY id", (storage.run_id, ANALYSIS_VERSION))[0]
    )
    assert inferred["signals"]["q_counted_posts"] == 1


def test_cross_post_copies_are_suppressed_by_family_and_audited(storage) -> None:
    """不同帖复制同一内容：仍受家族抑制，帖子数与抑制份数分开记账。"""
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION), Card(f"{HELP_QUESTION}？")])
    cluster(storage)
    score(storage)
    payload = json_of(single_explicit(storage))
    signals = payload["signals"]
    assert signals["distinct_posts"] == 2 and signals["content_families"] == 1
    assert signals["q_counted_posts"] == 1
    assert signals["q_family_suppressed_posts"] == 1
    assert signals["duplicate_versions_suppressed"] == 1
    assert signals["q_suppression_reasons"] == ["cross_post_duplicate_content"]
    assert payload["score"]["features"]["Q"]["raw"] == 1.0


def test_multiple_versions_of_one_post_do_not_raise_q(storage) -> None:
    """同帖正文两个版本（两个家族、同一 field）：Q 仍 1 篇，原因记多版本。"""
    keyword_id = prepare(storage)
    note = f"ver-{note_id_for(HELP_QUESTION)}"
    observe_page(storage, keyword_id, [(note, FILLER)], session_id="s-card", minutes=0)
    fetch_body(storage, keyword_id, note, HELP_QUESTION, session_id="s-read-a", minutes=10)
    fetch_body(
        storage, keyword_id, note, f"{HELP_QUESTION}，急死人了",
        session_id="s-read-b", minutes=20, attempt_no=2,
    )
    cluster(storage)
    score(storage)
    signals = json_of(need_by(storage, HELP_QUESTION))["signals"]
    assert signals["evidence_rows"] == 2, "两个正文版本的原文证据都保留"
    assert signals["distinct_posts"] == 1
    assert signals["content_families"] == 2
    assert signals["q_counted_posts"] == 1
    assert "same_post_multiple_versions" in signals["q_suppression_reasons"]


def test_multi_field_multi_post_overlap_resolves_to_one_component(storage) -> None:
    """同帖多 field × 另一帖标题复制同一内容：并查后只剩 1 件内容、1 篇计数的 Q。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, HELP_QUESTION)
    observe_page(storage, keyword_id, [("copy-post", HELP_QUESTION)], session_id="s-copy", minutes=10)
    cluster(storage)
    score(storage)
    payload = json_of(single_explicit(storage))
    signals = payload["signals"]
    assert signals["distinct_posts"] == 2
    assert signals["content_families"] == 2
    assert signals["q_counted_posts"] == 1
    assert signals["q_family_suppressed_posts"] == 1
    assert signals["q_post_absorbed_families"] == 1
    assert signals["duplicate_versions_suppressed"] == 1
    assert signals["q_suppression_reasons"] == [
        "cross_post_duplicate_content",
        "same_post_multiple_fields",
    ]
    assert payload["score"]["features"]["Q"]["raw"] == 1.0
    assert signals["distinct_known_authors"] == 2, "A 仍按已知作者数，不被 Q 的并查改动"


# ------------------------------------------------------------------ 导出可审计


def test_needs_csv_and_score_json_carry_the_split_counts(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="s-a", minutes=0)
    observe_titles(storage, keyword_id, [HELP_QUESTION], session_id="s-b", minutes=5)
    observe_body(storage, keyword_id, HELP_QUESTION, HELP_QUESTION)
    cluster(storage)
    score(storage)
    result = export_run(
        storage.conn, storage.paths, storage.run_id,
        analysis_version=ANALYSIS_VERSION, extractor_version=EXTRACTOR_VERSION, at=at(20),
    )
    entry = next(item for item in result.files if item.filename == "needs.csv")
    text = (storage.paths.root / entry.rel_path).read_text(encoding="utf-8")
    header, body = parse_csv(text)
    assert header == list(NEED_COLUMNS)
    row = next(item for item in body if HELP_QUESTION in item[header.index("canonical_text")])
    cells = dict(zip(header, row))
    assert cells["sessions"] == "3", "两次标题会话 + 一次正文读取会话都可审计"
    assert cells["evidence_row_sessions"] == "2", "事实层只有两条证据行各自的会话"
    assert cells["evidence_row_parent_paths"] == "1"
    assert cells["distinct_posts"] == "1"
    assert cells["content_families"] == "2"
    assert cells["q_counted_posts"] == "1"
    assert "same_post_multiple_fields" in cells["q_suppression_reasons"]
    embedded = json.loads(cells["score_json"])
    assert embedded["signals"]["q_counted_posts"] == 1
    assert embedded["signals"]["sessions"] == 3
