"""P0-27：S/Q/A/F 评分分解、内容家族去重与未知属性。

这些测试钉的是"分数能不能被解释、未知会不会被粉饰"，不是分数排得准不准（那要 P2 标注集）：

- 四个分量按 `PROJECT_SPEC` 第 9 节的 `.35S+.30Q+.20A+.15F` 走，原值与归一化值同时留在
  `score_json`，缺失分量交回 `WeightedScore` 重归一化并留下 `missing_reason`；
- **未知不当零**：发布时间解析不出 → F=unknown；作者引用全缺 → A=unknown。
  缺年份的 `09-01` 不补年、"最近"不当成今天，猜出来的日期会直接进排序；
- **点赞不是搜索量**：把页面点赞数放大一万倍，分数与分解必须逐字不变；
- **Q 按去重帖子数计，与家族抑制联立**（C-14）：同一句话被两篇帖子抄过，`distinct_posts=2`
  而 `content_families=1`，被抑制的份数单独记在 `duplicate_versions_suppressed`；
  同帖多 field 不抬 Q（`tests/test_analysis_scoring_aggregation.py` 钉住完整分解）。
- 没有页面名次时位置信号保持 None，不填中性值——"没采到位置"和"位置一般"不能同分；
- 只有 `explicit_problem` 与 `inferred_need` 打分：方案构想与联想意图候选拿不到名次，
  生成的表达也永远不冒充帖子事实；
- 评分只回写**自己那批** `analysis_version` 的行，历史奖励不改写；重放不写第二代家族。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.scoring import (
    CONSENSUS_CAP,
    FAMILY_METHOD,
    QUANTILE_PROVISIONAL_REASON,
    SCORING_METHOD,
    SCORING_MODEL_VERSION,
    DemandScorer,
    family_key,
    log_scale,
    parse_published_at,
)
from xhs_discovery.analysis.store import record_scoring
from xhs_discovery.discovery.ingest import ingest_search
from xhs_discovery.models.enums import OperationType, ResultState
from xhs_discovery.models.source import AuthorRef, KeywordText, PostCard, PostSearchResult

from tests.conftest import T0
from tests.test_analysis_clustering import HELP_QUESTION, cluster, explicit_of, payload_of
from tests.test_analysis_extraction import (
    KEYWORD,
    at,
    context,
    note_id_for,
    observe_body,
    observe_suggestions,
    observe_titles,
    open_op,
    prepare,
    rows,
    session,
)


@dataclass
class Card:
    """一张卡片要能单独指定页面当时显示成什么样——未知属性的反例全靠这些字段构造。"""

    title: str
    published: str | None = "2026-09-01"
    likes: str | None = "12"
    author: bool = True


def observe_cards(storage, keyword_id: int, cards: list[Card], *, session_id="s-1", minutes=0):
    items = []
    for rank, card in enumerate(cards, start=1):
        note = f"score-{note_id_for(card.title)}-{rank}"
        items.append(
            PostCard(
                note_id=note,
                access_url=f"https://www.xiaohongshu.com/explore/{note}",
                title=card.title,
                author=(
                    AuthorRef(kind="public_ref", public_ref=f"u-{note}")
                    if card.author
                    else AuthorRef(kind="unknown")
                ),
                published_at_text=card.published,
                published_at_missing=card.published is None,
                likes_text=card.likes,
                result_rank=rank,
                search_keyword=KEYWORD,
            )
        )
    operation_id, attempt_id = open_op(storage, keyword_id, OperationType.search)
    return ingest_search(
        storage.repo,
        storage.run_id,
        PostSearchResult(
            state=ResultState.completed,
            context=context(storage, keyword_id, OperationType.search),
            session=session(session_id, minutes),
            items=items,
        ),
        operation_id=operation_id,
        attempt_id=attempt_id,
    )


def score(storage, *, analysis_version=ANALYSIS_VERSION, quantile_min_sample=1):
    """打分并落库：评分层读的是库里的需求行与证据行，不是采集时的内存对象。"""
    outcome = DemandScorer(
        storage.repo,
        storage.run_id,
        analysis_version=analysis_version,
        quantile_min_sample=quantile_min_sample,
    ).score()
    write = record_scoring(storage.repo, storage.run_id, outcome, at=at(7))
    return SimpleNamespace(outcome=outcome, write=write)


def json_of(need: dict) -> dict:
    return json.loads(need["score_json"])


def scored_needs(storage, analysis_version: str = ANALYSIS_VERSION) -> list[dict]:
    return rows(
        storage.conn,
        "SELECT * FROM needs WHERE run_id = ? AND analysis_version = ? ORDER BY id",
        (storage.run_id, analysis_version),
    )


def single_explicit(storage) -> dict:
    explicit = explicit_of(storage)
    assert len(explicit) == 1
    return explicit[0]


# ------------------------------------------------------------------ 纯函数


def test_published_text_without_a_year_or_a_vague_word_is_not_guessed() -> None:
    assert parse_published_at("2026-09-01", seen_at=T0).date().isoformat() == "2026-09-01"
    assert parse_published_at("2026年9月1日", seen_at=T0).date().isoformat() == "2026-09-01"
    assert parse_published_at("3天前", seen_at=T0).date().isoformat() == "2026-10-21"
    assert parse_published_at("昨天", seen_at=T0).date().isoformat() == "2026-10-23"
    assert parse_published_at("09-01", seen_at=T0) is None, "缺年份不补年"
    assert parse_published_at("最近", seen_at=T0) is None, "模糊说法不当成今天"
    assert parse_published_at("2026-02-30", seen_at=T0) is None, "非法日期不强行解释"
    assert parse_published_at(None, seen_at=T0) is None


def test_log_scale_saturates_at_the_anchor() -> None:
    assert log_scale(0, CONSENSUS_CAP) == 0.0
    assert log_scale(CONSENSUS_CAP, CONSENSUS_CAP) == 1.0
    assert log_scale(1000, CONSENSUS_CAP) == 1.0
    assert log_scale(2, CONSENSUS_CAP) < log_scale(4, CONSENSUS_CAP) < log_scale(8, CONSENSUS_CAP)


def test_the_family_key_ignores_punctuation_but_not_the_field() -> None:
    assert family_key("title", f"{HELP_QUESTION}？") == family_key("title", HELP_QUESTION)
    assert family_key("title", HELP_QUESTION) != family_key("body", HELP_QUESTION), (
        "标题与正文是两份内容，不并进同一件"
    )


# ------------------------------------------------------------------ 分量分解


def test_the_four_components_carry_the_spec_weights_and_both_raw_and_normalized_values(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    score(storage)
    need = single_explicit(storage)
    payload = json_of(need)
    assert payload["method"] == SCORING_METHOD
    assert payload["weights"] == {"S": 0.35, "Q": 0.30, "A": 0.20, "F": 0.15}
    features = payload["score"]["features"]
    assert set(features) == {"S", "Q", "A", "F"}
    assert {name: value["baseline_weight"] for name, value in features.items()} == {
        "S": 0.35, "Q": 0.30, "A": 0.20, "F": 0.15
    }
    assert all(value["available"] is True for value in features.values())
    assert payload["score"]["effective_weight_total"] == 1.0
    assert payload["score"]["model_version"] == SCORING_MODEL_VERSION
    assert need["score"] == payload["score"]["score"]
    # 原值留档：复核的人要能看出"这个分数是几篇帖子撑起来的"
    assert features["Q"]["raw"] == payload["signals"]["content_families"]
    assert payload["unknowns"] == []
    assert payload["stable_key"] == need["stable_key"]


def test_an_unparseable_publish_date_leaves_f_unknown_instead_of_zero(storage) -> None:
    """未知日期反例：F 缺失走重归一化，不是把这条需求判成"过时"。"""
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION, published=None)])
    cluster(storage)
    score(storage)
    payload = json_of(single_explicit(storage))
    assert payload["unknowns"] == ["F"]
    assert payload["score"]["features"]["F"]["available"] is False
    assert "无法可靠解析" in payload["score"]["features"]["F"]["missing_reason"]
    assert payload["score"]["effective_weight_total"] == 0.85
    assert payload["score"]["score"] is not None
    assert payload["signals"]["published_dates_parsed"] == 0
    assert payload["signals"]["published_dates_unparseable"] == 1


def test_a_date_that_parses_but_is_old_scores_f_zero_not_unknown(storage) -> None:
    """解析得出但很久没再被提 → F=0 且仍然 available；这与"解析不出"是两种状态。"""
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION, published="2024-01-01")])
    cluster(storage)
    score(storage)
    payload = json_of(single_explicit(storage))
    assert payload["unknowns"] == []
    assert payload["score"]["features"]["F"]["available"] is True
    assert payload["score"]["features"]["F"]["normalized"] == 0.0
    assert payload["signals"]["latest_published_at"] == "2024-01-01"


def test_missing_author_refs_do_not_make_every_post_a_separate_source(storage) -> None:
    """作者引用全缺：A 记 unknown。把每篇当不同作者，一次刷屏就能把 A 顶满。"""
    keyword_id = prepare(storage)
    observe_cards(
        storage, keyword_id, [Card(HELP_QUESTION, author=False), Card(f"{HELP_QUESTION}？", author=False)]
    )
    cluster(storage)
    score(storage)
    payload = json_of(single_explicit(storage))
    assert "A" in payload["unknowns"]
    assert payload["score"]["features"]["A"]["available"] is False
    assert payload["signals"]["distinct_known_authors"] == 0
    assert payload["signals"]["posts_without_author_ref"] == 2
    assert payload["score"]["effective_weight_total"] == 0.80, "只剩 S+Q+F"


def test_likes_text_is_page_text_and_never_a_demand_signal(storage) -> None:
    """把页面点赞数放大一万倍，分数与分解必须逐字不变。"""
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION, likes="12")])
    cluster(storage)
    score(storage)
    before = single_explicit(storage)
    storage.conn.execute(
        "UPDATE post_queries SET likes_text = ? WHERE run_id = ?", ("120000", storage.run_id)
    )
    storage.conn.commit()
    score(storage)
    after = single_explicit(storage)
    assert after["score"] == before["score"]
    assert json_of(after) == json_of(before), "点赞不是搜索量，也不参与 S/Q/A/F"
    payload = json_of(after)
    assert not [key for key in payload["signals"] if "likes" in key], (
        "分数里可以声明点赞不算需求，但不能出现一个由点赞算出来的信号"
    )
    assert payload["assumptions"]["likes_are_not_demand"] is True


def test_without_a_page_position_the_position_signal_stays_unknown(storage) -> None:
    """没有名次就不猜中性值：S 只由共识部分给出，否则"没采到位置"和"位置一般"会同分。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    storage.conn.execute("DELETE FROM post_queries WHERE run_id = ?", (storage.run_id,))
    storage.conn.commit()
    score(storage)
    payload = json_of(single_explicit(storage))
    assert payload["signals"]["best_rank"] is None
    assert payload["signals"]["position_signal"] is None
    assert payload["score"]["features"]["S"]["normalized"] == payload["signals"]["consensus_signal"]


def test_a_top_position_beats_a_deep_one_on_the_same_signal(storage) -> None:
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION)])
    cluster(storage)
    score(storage)
    top = single_explicit(storage)
    storage.conn.execute(
        "UPDATE post_queries SET result_rank = 18 WHERE run_id = ?", (storage.run_id,)
    )
    storage.conn.commit()
    score(storage)
    deep = single_explicit(storage)
    top_payload, deep_payload = json_of(top), json_of(deep)
    assert top_payload["signals"]["best_rank"] == 1 and deep_payload["signals"]["best_rank"] == 18
    assert (
        top_payload["score"]["features"]["S"]["normalized"]
        > deep_payload["score"]["features"]["S"]["normalized"]
    )
    assert top["score"] > deep["score"]


# ------------------------------------------------------------------ 内容家族去重


def test_duplicate_content_counts_once_for_q_and_keeps_both_quotes(storage) -> None:
    """两篇帖子问同一句话：Q 只算一件内容，被抑制的份数单独记账。"""
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION), Card(f"{HELP_QUESTION}？")])
    cluster(storage)
    run = score(storage)
    need = single_explicit(storage)
    assert len(payload_of(need)["evidence_ids"]) == 2
    payload = json_of(need)
    assert payload["signals"]["distinct_posts"] == 2
    assert payload["signals"]["content_families"] == 1
    assert payload["signals"]["duplicate_versions_suppressed"] == 1
    assert payload["score"]["features"]["Q"]["raw"] == 1.0
    assert [family.member_count for family in run.outcome.families] == [2]
    assert run.write.families_inserted == 1 and run.write.members_inserted == 2


def test_a_body_and_its_title_are_two_pieces_of_content(storage) -> None:
    """同一句话作为标题和作为正文是两份内容：字段不同就不并进同一件。

    C-14：两份家族是审计事实必须保留，但它们是**一篇**帖子——Q 按去重帖子数计，
    同帖多 field 不抬 Q（聚合口径见 tests/test_analysis_scoring_aggregation.py）。
    """
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, HELP_QUESTION)
    cluster(storage)
    run = score(storage)
    assert {family.field_name for family in run.outcome.families} == {"title", "body"}
    payload = json_of(single_explicit(storage))
    assert payload["signals"]["content_families"] == 2
    assert payload["signals"]["distinct_posts"] == 1
    assert payload["score"]["features"]["Q"]["raw"] == 1.0, "同帖两个 field 仍是一篇帖子"


# ------------------------------------------------------------------ 小样本与不被打分的类别


def test_a_small_batch_is_marked_provisional_and_keeps_the_raw_counts(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    run = score(storage, quantile_min_sample=30)
    assert run.outcome.provisional is True
    assert run.outcome.summary()["provisional"] is True
    for item in run.outcome.scored:
        assert item.score.provisional is True
        assert item.score.provisional_reason == QUANTILE_PROVISIONAL_REASON
    payload = json_of(single_explicit(storage))
    assert payload["score"]["provisional"] is True
    assert payload["assumptions"]["quantile_min_sample"] == 30
    assert payload["signals"]["sessions"] == 1, "待验证不等于丢掉原值"


def test_solution_ideas_and_intent_candidates_are_never_scored(storage) -> None:
    """构想与联想不参与排行榜：给它们一个可比较的分数等于把它们摆上同一张表。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, "要是有自动填志愿的工具就好了")
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    cluster(storage)
    run = score(storage)
    kinds = {item.statement_kind.value for item in run.outcome.scored}
    assert kinds == {"explicit_problem", "inferred_need"}
    unscored = [need for need in scored_needs(storage) if need["statement_kind"] not in kinds]
    assert {need["statement_kind"] for need in unscored} == {
        "solution_idea",
        "search_intent_candidate",
    }, "语料里确实有构想与候选行"
    assert all(need["score"] is None and need["score_json"] is None for need in unscored), (
        "没打分的行保持空白，不能残留别的类别的分数"
    )


def test_a_need_that_loses_its_evidence_gets_no_score_not_zero(storage) -> None:
    """展示需求必须有有效证据：证据行不在，分数就是 NULL，而不是一个 0 分名次。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    score(storage)
    assert single_explicit(storage)["score"] is not None
    storage.conn.execute("DELETE FROM evidence WHERE run_id = ?", (storage.run_id,))
    storage.conn.commit()
    run = score(storage)
    assert run.write.needs_without_score == len(run.outcome.scored)
    payload = json_of(single_explicit(storage))
    assert single_explicit(storage)["score"] is None
    assert payload["unknowns"] == ["S", "Q", "A", "F"]
    assert payload["score"]["all_missing"] is True and payload["score"]["score"] is None
    events = storage.repo.events(storage.run_id, kind="demand_score_undisplayable")
    assert len(events) == len(run.outcome.scored)


# ------------------------------------------------------------------ 版本、幂等与事件


def test_rescoring_is_idempotent_and_replay_writes_no_second_family(storage) -> None:
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION), Card(f"{HELP_QUESTION}？")])
    cluster(storage)
    first = score(storage)
    before = single_explicit(storage)
    families_before = rows(storage.conn, "SELECT * FROM content_families ORDER BY id")
    second = score(storage)
    assert second.write.families_inserted == 0
    assert second.write.families_reused == len(families_before)
    assert second.write.members_inserted == 0
    assert second.write.scores_updated == first.write.scores_updated
    assert rows(storage.conn, "SELECT * FROM content_families ORDER BY id") == families_before
    assert single_explicit(storage) == before, "同样输入重放：整行逐字不变"


def test_scoring_only_touches_its_own_analysis_version(storage) -> None:
    """不改写历史奖励：新版本自己一批分数，旧版本的 score 与 score_json 逐字不变。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    score(storage)
    old = rows(
        storage.conn, "SELECT * FROM needs WHERE analysis_version = ? ORDER BY id", (ANALYSIS_VERSION,)
    )
    second_version = "p0-rule-baseline.4"
    cluster(storage, analysis_version=second_version, extractor_version=EXTRACTOR_VERSION)
    run = score(storage, analysis_version=second_version)
    assert rows(
        storage.conn, "SELECT * FROM needs WHERE analysis_version = ? ORDER BY id", (ANALYSIS_VERSION,)
    ) == old, "旧版本行的分数一个字节都不动"
    fresh = rows(
        storage.conn,
        "SELECT * FROM needs WHERE analysis_version = ? AND score IS NOT NULL ORDER BY id",
        (second_version,),
    )
    assert len(fresh) == run.write.scores_updated
    assert all(row["updated_at"] == at(7).isoformat() for row in fresh)


def test_scoring_records_weights_assumptions_and_unknown_components(storage) -> None:
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION, published="09-01", author=False)])
    cluster(storage)
    run = score(storage)
    batch = storage.repo.events(storage.run_id, kind="analysis_scoring")
    assert len(batch) == 1
    detail = json.loads(batch[0]["detail_json"])
    assert detail["method"] == SCORING_METHOD
    assert detail["weights"] == {"S": 0.35, "Q": 0.30, "A": 0.20, "F": 0.15}
    assert detail["assumptions"]["family_method"] == FAMILY_METHOD
    assert detail["assumptions"]["likes_are_not_demand"] is True
    assert detail["assumptions"]["posts_are_not_users"] is True
    assert detail["scored_needs"] == len(run.outcome.scored)
    misses = storage.repo.events(storage.run_id, kind="demand_score_unknown_component")
    assert len(misses) == len(run.outcome.scored), "A/F 双缺：每条打分都要能看出少了哪个分量"
    reasons = json.loads(misses[0]["detail_json"])
    assert sorted(reasons["unknowns"]) == ["A", "F"]
    assert reasons["effective_weight_total"] == 0.65


def test_the_scorer_refuses_an_empty_run_and_another_runs_outcome(storage) -> None:
    with pytest.raises(ValueError, match="必须按 run"):
        DemandScorer(storage.repo, "")
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    outcome = DemandScorer(storage.repo, storage.run_id).score()
    with pytest.raises(ValueError, match="不能写进"):
        record_scoring(storage.repo, "run-other", outcome, at=at(7))


def test_scoring_reads_only_evidence_of_its_own_batch(storage) -> None:
    """另一版本留下的证据不得混进本批计数——`analysis_version` 是口径，不是标签。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    run = score(storage)
    assert json_of(single_explicit(storage))["signals"]["content_families"] == 1
    storage.conn.execute(
        "UPDATE evidence SET analysis_version = ? WHERE run_id = ?",
        ("p0-foreign-batch", storage.run_id),
    )
    storage.conn.commit()
    after = score(storage)
    assert after.outcome.families == ()
    assert [item.score.score for item in after.outcome.scored] == [None] * len(
        after.outcome.scored
    )
    assert after.write.needs_without_score == len(run.outcome.scored)
