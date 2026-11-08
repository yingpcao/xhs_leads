"""C-12：相对发布时间的解析锚点必须是观察时间，不是分析批次时间。

钉住的事实（docs/verification/2026-10-27-C12-CODEX-REPRO.md）：

- 同一条观察（页面写"3天前"）换 analysis_version、换批次时间重跑提取/归并/评分，
  `latest_published_at` 不许移动；`evidence.created_at` 仍如实记录分析写入时刻；
- 相对日期**逐行**按该证据所属卡片观察的 `observed_at` 解析：不把别的帖子、别的观察
  或批次时间的最大值套在本行上；
- 近期窗口参照时刻与解析锚点分离：显式批次评分时间只移动窗口，不移动发布日期；
  不给参照时退回本簇观察锚点的最大值，跨批次稳定；
- 锚点缺失（v5 之前没有回链的旧证据行）保守 unknown：不拿 `created_at` 凑锚点，
  也不按 (run, post) 的首条 `post_queries` 借用发布时间文本；
- 正文证据的发布时间文本属于它的卡片观察，不是正文抓取时刻；
- 来源按 run 隔离：另一 run 对同一帖子显示"今天"不动本 run 的解析；
- 锚点与窗口参照一路传到 needs.csv / evidence.csv，图上可复核。
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from xhs_discovery.analysis.clustering import ConservativeMerger
from xhs_discovery.analysis.corpus import load_corpus
from xhs_discovery.analysis.export import (
    EVIDENCE_COLUMNS,
    NEED_COLUMNS,
    export_run,
    parse_csv,
)
from xhs_discovery.analysis.scoring import DemandScorer
from xhs_discovery.analysis.store import (
    record_clustering,
    record_extraction,
    record_scoring,
)
from xhs_discovery.discovery.ingest import ingest_body, ingest_search
from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.enums import OperationType, ResultState, SourceField, StatementKind
from xhs_discovery.models.source import (
    AuthorRef,
    ContentVersionRef,
    PostBodyResult,
    PostCard,
    PostSearchResult,
)

from tests.conftest import T0, build_run_identity
from tests.test_analysis_clustering import HELP_QUESTION
from tests.test_analysis_extraction import (
    BRANCH,
    KEYWORD,
    MATCHER,
    context,
    extractor,
    open_op,
    prepare,
    rows,
    session,
)

VERSION_A = "c12-anchor-a"
VERSION_B = "c12-anchor-b"
C12_EXTRACTOR = "c12-rule-extract.1"
DAY = timedelta(days=1)


def observe_post(
    storage,
    keyword_id: int,
    *,
    tag: str,
    published: str | None,
    session_id: str,
    minutes: int = 0,
    revisit: int = 0,
):
    """一篇帖的一次卡片观察：note 用 tag 区分，发布时间原文想写什么写什么。"""
    note = f"c12-{tag}"
    operation_id, attempt_id = open_op(
        storage, keyword_id, OperationType.search, revisit=revisit
    )
    return ingest_search(
        storage.repo,
        storage.run_id,
        PostSearchResult(
            state=ResultState.completed,
            context=context(storage, keyword_id, OperationType.search, revisit=revisit),
            session=session(session_id, minutes),
            items=[
                PostCard(
                    note_id=note,
                    access_url=f"https://www.xiaohongshu.com/explore/{note}",
                    title=HELP_QUESTION,
                    author=AuthorRef(kind="public_ref", public_ref=f"u-{note}"),
                    published_at_text=published,
                    published_at_missing=published is None,
                    likes_text="12",
                    result_rank=1,
                    search_keyword=KEYWORD,
                )
            ],
        ),
        operation_id=operation_id,
        attempt_id=attempt_id,
    )


def observe_body_for(
    storage, keyword_id: int, *, tag: str, body: str, session_id: str, minutes: int
):
    """对 `observe_post` 建的那篇帖抓正文：note 与卡片行一致，正文原文照录。"""
    operation_id, attempt_id = open_op(
        storage, keyword_id, OperationType.body, revisit=1, attempt_no=1
    )
    return ingest_body(
        storage.repo,
        storage.run_id,
        PostBodyResult(
            state=ResultState.completed,
            context=context(storage, keyword_id, OperationType.body, revisit=1),
            session=session(session_id, minutes),
            body=body,
            content_version=ContentVersionRef(
                content_version_id=None, text=body, text_sha256=sha256_text(body)
            ),
        ),
        note_id=f"c12-{tag}",
        operation_id=operation_id,
        attempt_id=attempt_id,
    )


def analyze_at(storage, *, version: str, stamp, reference_at=None):
    """按给定批次时间跑提取→归并→评分：观察层一个字不动，只有"分析发生在何时"在变。"""
    corpus = load_corpus(storage.conn, storage.run_id)
    written = record_extraction(
        storage.repo,
        storage.run_id,
        extractor(
            analysis_version=version, extractor_version=C12_EXTRACTOR
        ).extract(corpus.units),
        at=stamp,
    )
    plan = ConservativeMerger(
        storage.repo,
        storage.run_id,
        analysis_version=version,
        extractor_version=C12_EXTRACTOR,
        matcher=MATCHER,
    ).plan(corpus)
    record_clustering(storage.repo, storage.run_id, plan, at=stamp)
    outcome = DemandScorer(
        storage.repo,
        storage.run_id,
        analysis_version=version,
        quantile_min_sample=1,
        reference_at=reference_at,
    ).score()
    record_scoring(storage.repo, storage.run_id, outcome, at=stamp)
    return outcome, written


def explicit_score(outcome):
    """一批打分里只认显性问题簇：推断需求是同一句的派生表达，不是第二个事实。"""
    items = [
        item for item in outcome.scored
        if item.statement_kind is StatementKind.explicit_problem
    ]
    assert len(items) == 1
    return items[0]


def signals_of(outcome) -> dict:
    return explicit_score(outcome).signals


def created_at_of(storage, version: str) -> str:
    return str(
        rows(
            storage.conn,
            "SELECT created_at FROM evidence WHERE run_id = ? AND analysis_version = ?"
            " ORDER BY id LIMIT 1",
            (storage.run_id, version),
        )[0]["created_at"]
    )


# ------------------------------------------------------------------ 主案：批次时间不移动发布日期


def test_relative_published_date_is_identical_across_analysis_batches(storage) -> None:
    """C-12 主案（REPRO 的 pytest 版）：同一 2026-10-24 观察，"3天前"永远是 2026-10-21。

    批 b 在 200 天后重跑同一库事实：`latest_published_at` 与 F 分解不许变；
    `evidence.created_at` 照常记录各自的分析写入时刻——审计看 created_at，
    解析看观察锚点，两件事从此分家。
    """
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")

    stamp_b = T0 + timedelta(days=200)
    outcome_a, _ = analyze_at(storage, version=VERSION_A, stamp=T0)
    outcome_b, _ = analyze_at(storage, version=VERSION_B, stamp=stamp_b)

    sig_a, sig_b = signals_of(outcome_a), signals_of(outcome_b)
    assert sig_a["latest_published_at"] == "2026-10-21", (T0 - 3 * DAY).date().isoformat()
    assert sig_b["latest_published_at"] == "2026-10-21", "同一观察换批次不许改写发布日期"
    assert sig_b["published_dates_parsed"] == sig_a["published_dates_parsed"] == 1
    assert sig_b["window_reference_at"] == sig_a["window_reference_at"], (
        "缺省窗口参照来自观察锚点，跨批次同样稳定"
    )
    feat_a = explicit_score(outcome_a).score.features["F"]
    feat_b = explicit_score(outcome_b).score.features["F"]
    assert (feat_b.raw, feat_b.normalized) == (feat_a.raw, feat_a.normalized)

    assert created_at_of(storage, VERSION_A) == T0.isoformat()
    assert created_at_of(storage, VERSION_B) == stamp_b.isoformat(), (
        "created_at 仍然是分析时刻——它只是审计，不再是解析锚点"
    )


def test_absolute_published_date_ignores_batch_time_too(storage) -> None:
    """绝对日期在两个批次给出同一个解析结果（连时区派生都只跟着观察走）。"""
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="2026年9月1日", session_id="s-1")
    outcome_a, _ = analyze_at(storage, version=VERSION_A, stamp=T0)
    outcome_b, _ = analyze_at(storage, version=VERSION_B, stamp=T0 + timedelta(days=200))
    assert signals_of(outcome_a)["latest_published_at"] == "2026-09-01"
    assert signals_of(outcome_b)["latest_published_at"] == "2026-09-01"


# ------------------------------------------------------------------ 逐行锚点：别帖/别观察的时间不套本行


def test_each_row_is_parsed_by_its_own_observation_not_the_largest_stamp(storage) -> None:
    """两条观察、两个时刻、两种相对写法：每条按**自己**的观察解析。

    帖 A 在 T0 看到"3天前"→ 2026-10-21；帖 B 在 T0+10天 看到"1天前"→ 2026-11-02。
    旧实现把全体证据的最大时间戳当 seen_at 套在每一行上，latest 会被推到解析批附近。
    """
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    observe_post(
        storage,
        keyword_id,
        tag="p2",
        published="1天前",
        session_id="s-2",
        minutes=10 * 24 * 60,
        revisit=1,
    )
    outcome, _ = analyze_at(storage, version=VERSION_A, stamp=T0 + timedelta(days=30))
    sig = signals_of(outcome)
    assert sig["published_dates_parsed"] == 2
    assert sig["latest_published_at"] == (T0 + 9 * DAY).date().isoformat(), (
        "B 的发布日期只能由 B 自己的观察时刻推出，不能被解析批次或其他行的时间带偏"
    )


def test_second_observation_of_same_version_reuses_the_first_anchor(storage) -> None:
    """同一内容版本多观察：证据行只有一条，锚点跟着第一次登记的那次观察走。

    复用已有行时不重写来源（store.insert_evidence 的既有口径），也不因第二次观察在
    20 天后就把"3天前"改算成 20 天前——那是把同一事实看到两次，不是两条事实。
    """
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    observe_post(
        storage,
        keyword_id,
        tag="p1",
        published="23天前",
        session_id="s-2",
        minutes=20 * 24 * 60,
        revisit=1,
    )
    outcome, _ = analyze_at(storage, version=VERSION_A, stamp=T0 + timedelta(days=20))
    sig = signals_of(outcome)
    assert sig["latest_published_at"] == (T0 - 3 * DAY).date().isoformat()
    evidence = rows(
        storage.conn,
        "SELECT post_observation_id FROM evidence WHERE run_id = ? AND analysis_version = ?"
        " AND source_field = 'title'",
        (storage.run_id, VERSION_A),
    )
    assert len(evidence) == 1, "同一版本同一句话是同一条事实被看到两次"
    first = rows(
        storage.conn,
        "SELECT id FROM post_observations WHERE run_id = ? ORDER BY id LIMIT 1",
        (storage.run_id,),
    )[0]
    assert int(evidence[0]["post_observation_id"]) == int(first["id"])


# ------------------------------------------------------------------ 窗口参照与解析锚点分离


def test_window_reference_moves_the_window_but_never_the_date(storage) -> None:
    """显式批次评分时间只做窗口参照：200 天后评分，发布日期仍是 2026-10-21，
    但那条日期相对窗口已经"不近期"——F 变小说明窗口真的移动了，解析没跟着移动。"""
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    far = T0 + timedelta(days=200)
    outcome, _ = analyze_at(
        storage, version=VERSION_A, stamp=T0, reference_at=far
    )
    sig = signals_of(outcome)
    assert sig["latest_published_at"] == "2026-10-21"
    assert sig["window_reference_at"] == far.isoformat()
    assert sig["window_reference_source"] == "batch_reference_at"
    assert sig["published_dates_parsed"] == 1
    assert explicit_score(outcome).score.features["F"].raw == 0.0, (
        "窗口参照 200 天后，90 天窗口内已无日期"
    )


def test_window_reference_falls_back_to_observation_anchor(storage) -> None:
    """不给参照时退回观察锚点，并在 signals/assumptions 里写明来源。"""
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="昨天", session_id="s-1", minutes=60)
    outcome, _ = analyze_at(storage, version=VERSION_A, stamp=T0)
    sig = signals_of(outcome)
    assert sig["window_reference_source"] == "latest_observed_at"
    assert sig["window_reference_at"] == (T0 + timedelta(hours=1)).isoformat()
    assert sig["latest_published_at"] == (T0 - DAY).date().isoformat()
    payload = rows(
        storage.conn,
        "SELECT score_json FROM needs WHERE run_id = ? AND analysis_version = ?"
        " AND score_json IS NOT NULL LIMIT 1",
        (storage.run_id, VERSION_A),
    )
    assert payload, "分数与分解已落库"
    dumped = json.loads(payload[0]["score_json"])
    assert dumped["assumptions"]["window_reference_at"] is None, (
        "assumptions 记录的是**显式**批次参照；没给时如实为 None，来源在 signals 里"
    )


def test_scorer_rejects_a_naive_window_reference(storage) -> None:
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    try:
        DemandScorer(
            storage.repo,
            storage.run_id,
            analysis_version=VERSION_A,
            reference_at=T0.replace(tzinfo=None),
        )
    except ValueError as error:
        assert "时区" in str(error)
    else:
        raise AssertionError("naive 参照时刻必须被拒绝，否则窗口会整体挪错方向")


# ------------------------------------------------------------------ 锚点缺失：保守 unknown


def test_evidence_without_an_anchor_is_unknown_not_borrowed(storage) -> None:
    """旧行没有回链（v5 前历史库）：不取该帖最早 post_queries 的记录、不用 created_at。

    发布日期只剩 unknown，`published_dates_without_anchor` 把缺口亮出来，
    而不是悄悄给出一个"看起来对"的日期。
    """
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    outcome, _ = analyze_at(storage, version=VERSION_A, stamp=T0)
    assert signals_of(outcome)["latest_published_at"] == "2026-10-21"

    storage.conn.execute(
        "UPDATE evidence SET post_observation_id = NULL WHERE run_id = ?"
        " AND analysis_version = ?",
        (storage.run_id, VERSION_A),
    )
    storage.conn.commit()
    rescored = DemandScorer(
        storage.repo, storage.run_id, analysis_version=VERSION_A, quantile_min_sample=1
    ).score()
    sig = signals_of(rescored)
    assert sig["published_dates_parsed"] == 0
    assert sig["published_dates_without_anchor"] == 1
    assert sig["latest_published_at"] is None
    assert sig["window_reference_at"] is None
    assert sig["window_reference_source"] is None
    assert "F" in explicit_score(rescored).unknowns
    assert explicit_score(rescored).score.features["F"].raw is None


def test_unparseable_or_missing_page_text_stays_unknown(storage) -> None:
    """未知日期与相对写法的旧口径不变：None/"09-01"/"最近"都解析不出，也不补。"""
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published=None, session_id="s-1")
    observe_post(
        storage, keyword_id, tag="p2", published="最近", session_id="s-1", minutes=0, revisit=1
    )
    outcome, _ = analyze_at(storage, version=VERSION_A, stamp=T0)
    sig = signals_of(outcome)
    assert sig["published_dates_parsed"] == 0
    assert sig["published_dates_without_anchor"] == 0, "有锚点但文本解析不出，不是锚点缺口"
    assert sig["published_dates_unparseable"] == sig["evidence_rows"]
    assert "F" in explicit_score(outcome).unknowns


# ------------------------------------------------------------------ 正文与跨 run 隔离


def test_body_evidence_uses_the_card_observation_not_the_capture_time(storage) -> None:
    """正文证据的发布时间文本来自卡片观察：解析锚是卡片时刻，不是正文 captured_at。

    卡片在 T0 写"3天前"；正文 60 天后才抓到。旧口径下正文 TextUnit 的 observed_at 是
    captured_at，卡片字段却借自另一条观察——这里要求两者各归各的。
    """
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    observe_body_for(
        storage,
        keyword_id,
        tag="p1",
        body=HELP_QUESTION,
        session_id="s-3",
        minutes=60 * 24 * 60,
    )
    corpus = load_corpus(storage.conn, storage.run_id)
    body_unit = corpus.by_source(SourceField.body)[0]
    assert body_unit.published_at_text == "3天前"
    assert body_unit.observed_at == T0 + timedelta(days=60), "正文观察时刻仍是抓取时间"
    assert body_unit.card_observation_id is not None
    assert body_unit.card_observed_at == T0, "发布时间文本所属卡片观察要另外保留"

    outcome, _ = analyze_at(storage, version=VERSION_A, stamp=T0 + timedelta(days=60))
    sig = signals_of(outcome)
    assert sig["published_dates_parsed"] == 2, "标题与正文两条证据各自都按卡片观察解析"
    assert sig["latest_published_at"] == (T0 - 3 * DAY).date().isoformat()


def test_another_runs_view_of_the_same_post_does_not_move_this_runs_date(storage) -> None:
    """跨 run 不借来源：另一 run 里同帖页面显示"今天"，本 run 的解析与文本都不受干扰。"""
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    outcome_a, _ = analyze_at(storage, version=VERSION_A, stamp=T0)
    assert signals_of(outcome_a)["latest_published_at"] == "2026-10-21"

    other_id = "run-c12-b"
    storage.repo.create_run(
        build_run_identity(other_id), config_json='{"budget": {}}'
    )
    storage.repo.ensure_branch(other_id, BRANCH, label="C-12 第二个任务")
    storage.repo.upsert_run_keyword(
        other_id, keyword_id, depth=0, origin="seed", status="queued", seen_at=T0
    )
    other = SimpleNamespace(conn=storage.conn, repo=storage.repo, run_id=other_id)
    observe_post(other, keyword_id, tag="p1", published="今天", session_id="s-9")

    outcome_b, _ = analyze_at(storage, version=VERSION_B, stamp=T0 + timedelta(days=1))
    sig = signals_of(outcome_b)
    assert sig["latest_published_at"] == "2026-10-21", (
        "本 run 的证据只认本 run 的观察：别的 run 显示什么都不许套进来"
    )


# ------------------------------------------------------------------ 图 / needs.csv / evidence.csv 可追溯


def file_of(storage, result, filename: str) -> str:
    entry = next(item for item in result.files if item.filename == filename)
    return (storage.paths.root / entry.rel_path).read_text(encoding="utf-8")


def test_anchor_and_window_reference_reach_the_exports(storage) -> None:
    """发布日期的每个数字都能从导出反读到观察行：needs.csv 带窗口参照与缺口计数，
    evidence.csv 带 post_observation_id 和该观察的时刻。"""
    keyword_id = prepare(storage)
    observe_post(storage, keyword_id, tag="p1", published="3天前", session_id="s-1")
    stamp = T0 + timedelta(days=200)
    analyze_at(storage, version=VERSION_A, stamp=stamp, reference_at=stamp)

    result = export_run(
        storage.conn,
        storage.paths,
        storage.run_id,
        analysis_version=VERSION_A,
        extractor_version=C12_EXTRACTOR,
        at=stamp,
        snapshot="c12",
        verify=True,
    )
    header, body = parse_csv(file_of(storage, result, "needs.csv"))
    assert header == list(NEED_COLUMNS)
    need = next(
        dict(zip(header, row))
        for row in body
        if dict(zip(header, row))["statement_kind"] == "explicit_problem"
    )
    assert need["latest_published_at"] == "2026-10-21", "换批次评分，发布日期仍是观察当天的 3 天前"
    assert need["window_reference_at"] == stamp.isoformat()
    assert need["window_reference_source"] == "batch_reference_at"
    assert need["published_dates_without_anchor"] == "0"
    assert need["F_raw"] == "0.0", "窗口参照在 200 天后：解析没动，窗口真的动了"

    header, body = parse_csv(file_of(storage, result, "evidence.csv"))
    assert header == list(EVIDENCE_COLUMNS)
    evidence = [dict(zip(header, row)) for row in body]
    title_rows = [row for row in evidence if row["source_field"] == "title"]
    assert title_rows
    for row in title_rows:
        assert int(row["post_observation_id"]) > 0
        assert row["published_at_observed_at"] == T0.isoformat(), (
            "evidence.csv 里的发布时间文本必须回链到本 run 的那次卡片观察"
        )
        assert row["published_at_text"] == "3天前"
    observation = rows(
        storage.conn,
        "SELECT observed_at FROM post_observations WHERE id = ?",
        (int(title_rows[0]["post_observation_id"]),),
    )[0]
    assert observation["observed_at"] == T0.isoformat()
