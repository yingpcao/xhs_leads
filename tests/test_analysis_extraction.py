"""P0-23/24：离线规则分析的问题抽取与可定位证据入库。

这里钉住的是"分析结果能不能被复核"这件事，不是抽取得准不准（那是 P2 标注集的事）：

- 语料只从库里重算：标题走 `replayed_posts` 的可见版本规则，正文只认带 run/operation/
  attempt 的观察行（C-09），联想取观察当时的版本行——恢复重放与分析看到同一份事实；
- 每条证据的 quote 必须能在**它所引用的那一行版本文本**里定位，对不上就整批拒绝；
- 八类意图 + 来源类型 + topic/object/intent/pain_point/location/year/polarity 全留在
  `problems` 行上，缺失就是 NULL，不拿主题词填空；
- 营销/机构/经验分享与纯资源标题不建有效簇，但原句照留；
- 同版本重放不写第二行；换 `analysis_version` 才另批，P0-C 的 `p0-preliminary.1`
  行与历史奖励一个字都不动。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.corpus import TextUnit, load_corpus
from xhs_discovery.analysis.extraction import (
    MAX_PROBLEMS_PER_UNIT,
    REASON_NOT_QUESTION,
    ProblemExtractor,
    segments,
)
from xhs_discovery.analysis.lexicon import DomainLexicon
from xhs_discovery.analysis.store import record_extraction
from xhs_discovery.config import load_intent_rules
from xhs_discovery.discovery.extract import extract_problems
from xhs_discovery.discovery.ingest import ingest_body, ingest_search, ingest_suggestion
from xhs_discovery.discovery.merge import PRELIMINARY_ANALYSIS_VERSION, PreliminaryMerger
from xhs_discovery.models.common import SessionRef, sha256_text
from xhs_discovery.models.enums import (
    OperationType,
    PostSourceType,
    ResultState,
    SourceField as SF,
)
from xhs_discovery.models.source import (
    AuthorRef,
    ContentVersionRef,
    KeywordText,
    OperationContext,
    PostBodyResult,
    PostCard,
    PostSearchResult,
    SuggestionItem,
    SuggestionResult,
)
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.normalize import KeywordNormalizer

from tests.conftest import T0, build_run_identity

ROOT = Path(__file__).resolve().parents[1]
MATCHER = IntentMatcher(load_intent_rules(ROOT / "config" / "intent_rules.yaml"))
NORMALIZER = KeywordNormalizer(
    ambiguous_seeds=("上海",), locations=("上海", "北京", "浦东", "闵行", "广州")
)
LEXICON = DomainLexicon(
    ("上海中考", "中考", "志愿", "志愿填报", "名额分配", "分数线", "复读", "民办", "建档线")
)
WORD = "上海中考"
KEYWORD = KeywordText(raw=WORD, normalized=WORD)
BRANCH = "b-analysis"


def at(minutes: int = 0):
    return T0 + timedelta(minutes=minutes)


def note_id_for(text: str) -> str:
    return f"n-{sha256_text(text)[:12]}"


@dataclass
class Case:
    """一份只做过一次采集的库：分析层面对的正是这种"库里已经有观察"的状态。"""

    storage: object
    keyword_id: int


def prepare(storage) -> int:
    keyword_id = storage.repo.ensure_keyword(WORD, WORD, seen_at=T0)
    storage.repo.ensure_branch(storage.run_id, BRANCH, label="分析样本")
    storage.repo.upsert_run_keyword(
        storage.run_id, keyword_id, depth=0, origin="seed", status="queued", seen_at=T0
    )
    return keyword_id


def open_op(storage, keyword_id: int, op_type: OperationType, *, revisit: int = 0, attempt_no: int = 1):
    operation_id = storage.repo.open_operation(
        storage.run_id, keyword_id, op_type, revisit, branch_key=BRANCH
    )
    attempt_id = storage.repo.start_attempt(operation_id, attempt_no)
    return operation_id, attempt_id


def session(sid: str, minutes: int = 0) -> SessionRef:
    return SessionRef(
        session_id=sid,
        collected_at=at(minutes),
        source_name="fixture",
        source_version="0.0.1",
        synthetic=True,
    )


def context(storage, keyword_id: int, op_type: OperationType, *, revisit: int = 0, attempt_no: int = 1):
    return OperationContext(
        run_id=storage.run_id,
        keyword_id=keyword_id,
        keyword=KEYWORD,
        operation_type=op_type,
        revisit_seq=revisit,
        attempt_no=attempt_no,
    )


def observe_titles(storage, keyword_id: int, titles: list[str], *, session_id="s-1", minutes=0):
    cards = []
    for rank, title in enumerate(titles, start=1):
        note = note_id_for(title)
        cards.append(
            PostCard(
                note_id=note,
                access_url=f"https://www.xiaohongshu.com/explore/{note}",
                title=title,
                author=AuthorRef(kind="public_ref", public_ref=f"u-{note}"),
                published_at_text="2026-09-01",
                likes_text="12",
                result_rank=rank,
                search_keyword=KEYWORD,
            )
        )
    operation_id, attempt_id = open_op(storage, keyword_id, OperationType.search)
    result = PostSearchResult(
        state=ResultState.completed,
        context=context(storage, keyword_id, OperationType.search),
        session=session(session_id, minutes),
        items=cards,
    )
    return ingest_search(storage.repo, storage.run_id, result, operation_id=operation_id, attempt_id=attempt_id)


def observe_body(storage, keyword_id: int, title: str, body: str, *, session_id="s-2", minutes=1):
    note = note_id_for(title)
    operation_id, attempt_id = open_op(
        storage, keyword_id, OperationType.body, revisit=1, attempt_no=1
    )
    result = PostBodyResult(
        state=ResultState.completed,
        context=context(storage, keyword_id, OperationType.body, revisit=1),
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


def observe_suggestions(storage, keyword_id: int, texts: list[str], *, session_id="s-0", minutes=0):
    operation_id, attempt_id = open_op(storage, keyword_id, OperationType.suggest)
    result = SuggestionResult(
        state=ResultState.completed,
        context=context(storage, keyword_id, OperationType.suggest),
        session=session(session_id, minutes),
        items=[SuggestionItem(text=t, rank=i + 1) for i, t in enumerate(texts)],
    )
    return ingest_suggestion(
        storage.repo,
        storage.run_id,
        result,
        normalizer=NORMALIZER,
        operation_id=operation_id,
        attempt_id=attempt_id,
        branch_key=BRANCH,
    )


def extractor(**overrides) -> ProblemExtractor:
    payload = dict(
        matcher=MATCHER, normalizer=NORMALIZER, lexicon=LEXICON,
        analysis_version=ANALYSIS_VERSION, extractor_version=EXTRACTOR_VERSION,
    )
    payload.update(overrides)
    return ProblemExtractor(**payload)


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ------------------------------------------------------------------ 语料读取


def test_corpus_reads_titles_through_the_visible_version_rule(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请", "志愿填报怎么改"])
    units = load_corpus(storage.conn, storage.run_id).by_source(SF.title)
    assert [unit.text for unit in units] == ["上海中考名额分配怎么申请", "志愿填报怎么改"]
    assert [unit.content_version_id for unit in units] == [
        row["id"]
        for row in storage.conn.execute(
            "SELECT id FROM post_content_versions WHERE field = 'title' ORDER BY id"
        )
    ]
    assert all(unit.note_id and unit.post_id for unit in units)
    assert all(unit.published_at_text == "2026-09-01" and unit.likes_text == "12" for unit in units), (
        "卡片的页面可见文本要一路带到分析层，F 与『点赞不是搜索量』才有依据"
    )


def test_corpus_units_carry_the_full_provenance_without_the_ingest_objects(storage) -> None:
    """证据只要库里的行就能重建：分析层不需要采集当时的内存对象。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请"])
    unit = load_corpus(storage.conn, storage.run_id).by_source(SF.title)[0]
    provenance = unit.provenance(storage.run_id)
    operation = provenance.operation
    assert operation.run_id == storage.run_id
    assert operation.keyword_id == unit.keyword_id
    assert operation.operation_type is OperationType.search
    assert operation.attempt_no == unit.attempt_no == 1
    assert provenance.session_id == "s-1"
    assert provenance.note_id == unit.note_id
    assert unit.operation_id == int(
        storage.conn.execute(
            "SELECT operation_id FROM post_observations WHERE run_id = ?", (storage.run_id,)
        ).fetchone()["operation_id"]
    )


def test_suggestion_units_use_the_observed_version_text_and_rank(storage) -> None:
    keyword_id = prepare(storage)
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填", "上海中考分数线"])
    units = load_corpus(storage.conn, storage.run_id).by_source(SF.suggestion)
    assert [unit.text for unit in units] == ["上海中考志愿怎么填", "上海中考分数线"]
    assert [unit.result_rank for unit in units] == [1, 2]
    assert all(unit.note_id is None and unit.post_id is None for unit in units)
    assert all(unit.suggestion_version_id is not None for unit in units)
    assert all(unit.content_version_id is None for unit in units)


def test_body_units_only_claim_attributed_observations(storage) -> None:
    """正文内容版本是全局的，"读到过"才是按 run 的事实：无来源的历史行不进语料（C-09）。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请"])
    observe_body(storage, keyword_id, "上海中考名额分配怎么申请", "第一段说明。\n第二批到底怎么排？")
    units = load_corpus(storage.conn, storage.run_id).by_source(SF.body)
    assert [unit.text for unit in units] == ["第一段说明。\n第二批到底怎么排？"]
    assert units[0].content_version_id is not None
    assert units[0].note_id == note_id_for("上海中考名额分配怎么申请")

    storage.conn.execute(
        "INSERT INTO body_observations(post_id, state, body_is_empty, content_version_id, captured_at)"
        " VALUES ((SELECT id FROM posts LIMIT 1), 'completed', 0, ?, ?)",
        (units[0].content_version_id, at(9).isoformat()),
    )
    storage.conn.commit()
    assert len(load_corpus(storage.conn, storage.run_id).by_source(SF.body)) == 1, (
        "来源为 NULL 的历史行不认领、不进口径"
    )


def test_corpus_is_deterministic_and_isolated_per_run(storage) -> None:
    first = prepare(storage)
    observe_titles(storage, first, ["志愿填报怎么改", "上海中考名额分配怎么申请"], session_id="s-a")
    corpus = load_corpus(storage.conn, storage.run_id)
    assert load_corpus(storage.conn, storage.run_id).units == corpus.units
    assert len(corpus.units) == 2

    storage.repo.create_run(build_run_identity("run-b"), config_json='{"budget": {}}')
    assert load_corpus(storage.conn, "run-b").units == (), (
        "run A 的观察不会自动变成 run B 的语料：帖子身份是全局的，读取不是"
    )


def _identity(run_id: str):
    return build_run_identity(run_id)


# ------------------------------------------------------------------ 抽取


def test_extraction_keeps_the_eight_intent_attributes_and_never_invents_them() -> None:
    cases = {
        "上海中考名额分配怎么申请": ("method_guidance", "名额分配", "上海", None, None),
        "2025年中考分数线什么时候公布": ("information_query", "分数线", None, "2025", None),
        "民办高中学费多少钱一年": ("cost_constraint", "民办", None, None, None),
        "上海中考志愿到底怎么排序": ("comparison_decision", "志愿", "上海", None, None),
        "中考复读哪个好": ("comparison_decision", "复读", None, None, None),
    }
    # 属性一律按**该分句**判定，所以这里不出现"值不值"这类含"不"的写法：
    # P0-13 的否定词表把"值不值"也算成否定，那是沿用在线层的既有口径，
    # 在分析层单独改掉会让两版 polarity 没法对照，只能连同它的假阳性一起继承。
    for text, (intent, obj, location, year, polarity) in cases.items():
        unit = _unit(text)
        attributes, _ = extractor().attributes_for(segments(text)[0] if segments(text) else text, unit)
        assert attributes.intent == intent, text
        assert attributes.object_ == obj, text
        assert attributes.location == location, text
        assert attributes.year == year, text
        assert attributes.polarity == polarity, text
        assert attributes.topic == WORD, text


def _unit(text: str, *, source_field=SF.title, note="n-x"):
    return TextUnit(
        source_field=source_field,
        run_id="run-a",
        text=text,
        content_version_id=1,
        suggestion_version_id=None,
        observation_id=1,
        operation_id=1,
        operation_type=OperationType.search,
        revisit_seq=0,
        attempt_no=1,
        session_id="s-1",
        keyword_id=1,
        keyword=KEYWORD,
        note_id=None if source_field is SF.suggestion else note,
        post_id=1 if source_field is not SF.suggestion else None,
        result_rank=1,
        published_at_text="2026-09-01",
        likes_text="12",
        observed_at=T0,
        synthetic=True,
    )


def test_marketing_negation_and_share_titles_keep_their_quote_but_are_not_valid() -> None:
    texts = [
        "上海中考辅导机构一对一报名，怎么收费？",  # 营销：含求助线索也不算用户求助
        "中考没考上的我，不想复读该怎么办？",  # 否定 + 对象都在疑问分句里
        "上海中考上岸经验分享，志愿怎么填的我不再说",  # 经验分享：没有疑问形式就不算求助
        "上海中考名额分配怎么申请",  # 正常求助
    ]
    outcome = extractor().extract([_unit(t, note=f"n-{i}") for i, t in enumerate(texts)])
    by_text = {problem.unit.text: problem for problem in outcome.problems}
    assert set(by_text) == set(texts)
    assert by_text[texts[0]].candidate.source_type is PostSourceType.marketing
    assert by_text[texts[0]].counts_as_valid is False
    assert by_text[texts[2]].candidate.source_type is PostSourceType.experience_share
    attributes = by_text[texts[1]].candidate.attributes
    assert attributes.polarity == "negative"
    assert attributes.object_ == "复读"
    assert by_text[texts[3]].counts_as_valid is True
    assert by_text[texts[3]].candidate.attributes.location == "上海"
    assert outcome.summary()["by_source_type"] == {
        "experience_share": 1,
        "marketing": 1,
        "user_help": 2,
    }
    # 「不想复读该怎么办」同时命中 method_guidance(怎么) 与 experience_validation(复读)，
    # 同分时 `IntentMatcher` 按意图名排序取前者，而它不在求助意图集合里，所以这句不计入簇。
    # 这是沿用 P0-C 的意图打分与定序规则：改它等于让两版结果对不上，只能连着它的偏向一起继承。
    assert attributes.intent == "experience_validation"
    assert by_text[texts[1]].counts_as_valid is False


def test_quotes_locate_in_the_container_and_keep_full_width_punctuation() -> None:
    text = "上海中考志愿怎么改？？先查分数线吗"
    outcome = extractor().extract([_unit(text)])
    assert outcome.problems
    for problem in outcome.problems:
        evidence = problem.candidate.evidence
        assert text[evidence.char_start : evidence.char_end] == evidence.quote
        assert "？" in text  # 原文未被 NFKC 折叠，quote 才是原文子串
    assert all("？" not in p.candidate.canonical_text for p in outcome.problems)


def test_one_body_can_yield_several_questions_without_double_counting_repeats() -> None:
    body = "上海中考志愿怎么改？志愿怎么改？名额分配什么时候申请？复读要不要考虑？建档线多少？"
    outcome = extractor().extract([_unit(body, source_field=SF.body)])
    texts = [p.candidate.canonical_text for p in outcome.problems]
    assert texts.count("志愿怎么改") == 1, "同一篇里重复出现的同一问句只算一个问题"
    assert len(texts) == MAX_PROBLEMS_PER_UNIT
    assert outcome.truncated_documents == 1


def test_documents_without_a_question_are_reported_not_silently_dropped() -> None:
    outcome = extractor().extract(
        [_unit("上海中考数学真题资料"), _unit("上海中考时间", source_field=SF.suggestion)]
    )
    assert outcome.documents == 1, "联想文本不参与 problems 的文档计数"
    assert outcome.problems == ()
    assert [row["reason"] for row in outcome.no_problem_documents] == [REASON_NOT_QUESTION]
    assert outcome.summary()["no_problem_documents"] == 1


def test_suggestion_units_never_become_post_problems() -> None:
    outcome = extractor().extract([_unit("上海中考志愿怎么填", source_field=SF.suggestion)])
    assert outcome.problems == ()
    assert outcome.documents == 0, "联想不是帖子事实，连『看过一篇』都不算"
    assert outcome.no_problem_documents == (), "联想走搜索意图候选（P0-26），不进问题审计"


# ------------------------------------------------------------------ 入库


def test_evidence_and_problem_rows_keep_versions_and_operation_refs(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请"])
    observe_body(storage, keyword_id, "上海中考名额分配怎么申请", "第二批到底怎么排？")
    corpus = load_corpus(storage.conn, storage.run_id)
    outcome = extractor().extract(corpus.units)
    write = record_extraction(storage.repo, storage.run_id, outcome, at=at(5))

    evidence = rows(storage.conn, "SELECT * FROM evidence WHERE run_id = ? ORDER BY id", (storage.run_id,))
    problems = rows(storage.conn, "SELECT * FROM problems WHERE run_id = ? ORDER BY id", (storage.run_id,))
    assert len(evidence) == len(problems) == write.problems_inserted
    for row in evidence:
        assert row["operation_id"] is not None and row["attempt_no"] == 1
        assert row["session_id"] in {"s-1", "s-2"}
        assert row["content_version_id"] is not None and row["suggestion_version_id"] is None
        assert row["extractor_version"] == EXTRACTOR_VERSION
        assert row["analysis_version"] == ANALYSIS_VERSION
        assert row["synthetic"] == 1
        container = storage.conn.execute(
            "SELECT text FROM post_content_versions WHERE id = ?", (row["content_version_id"],)
        ).fetchone()["text"]
        assert container[row["char_start"] : row["char_end"]] == row["quote"]
    by_field = {row["source_field"]: row for row in problems}
    assert set(by_field) == {"title", "body"}
    assert by_field["title"]["object"] == "名额分配"
    assert by_field["body"]["object"] is None, "句内没有词典词就留 NULL，不拿主题词填空"
    assert all(row["extraction_method"] == "rule_intent_segment_extract" for row in problems)
    assert all(row["topic"] == WORD for row in problems)
    assert all(row["intent"] for row in problems)


def test_replay_writes_no_second_row_and_reuses_ids(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请"])
    corpus = load_corpus(storage.conn, storage.run_id)
    first = record_extraction(storage.repo, storage.run_id, extractor().extract(corpus.units), at=at(5))
    second = record_extraction(storage.repo, storage.run_id, extractor().extract(corpus.units), at=at(6))
    assert second.problems_inserted == 0
    assert second.evidence_reused == first.evidence_inserted
    assert second.problem_ids == first.problem_ids
    assert second.evidence_ids == first.evidence_ids
    assert int(storage.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]) == len(first.evidence_ids)


def test_a_quote_that_no_longer_matches_its_version_row_is_refused(storage) -> None:
    """失效引用：证据指向的版本行必须真的含有那句话，否则整批拒绝且不写半行。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请"])
    corpus = load_corpus(storage.conn, storage.run_id)
    outcome = extractor().extract(corpus.units)
    version_id = outcome.problems[0].candidate.evidence.content_version_id
    storage.conn.execute(
        "UPDATE post_content_versions SET text = ? WHERE id = ?",
        ("帖子被人改过了，这里没有那句话", version_id),
    )
    storage.conn.commit()
    before = int(storage.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
    with pytest.raises(ValueError, match="拒绝入库"):
        record_extraction(storage.repo, storage.run_id, outcome, at=at(5))
    assert int(storage.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]) == before
    assert int(storage.conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]) == 0


def test_events_record_the_batch_and_every_no_problem_document(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考数学真题资料"])
    outcome = extractor().extract(load_corpus(storage.conn, storage.run_id).units)
    record_extraction(storage.repo, storage.run_id, outcome, at=at(5))
    batch = storage.repo.events(storage.run_id, kind="analysis_extraction")
    assert len(batch) == 1
    detail = batch[0]["detail_json"]
    assert '"documents"' in detail and '"no_problem_documents"' in detail
    misses = storage.repo.events(storage.run_id, kind="analysis_no_problem")
    assert len(misses) == 1 and REASON_NOT_QUESTION in misses[0]["detail_json"]


def test_analysis_does_not_rewrite_the_preliminary_rows_or_rewards(storage) -> None:
    """同一批观察，两套版本并存：P0-C 的行与簇计数保持不动，精修只新增。"""
    keyword_id = prepare(storage)
    ingest = observe_titles(storage, keyword_id, ["上海中考名额分配怎么申请"])
    records = extract_problems(
        storage.run_id,
        _search_result(storage, keyword_id, ["上海中考名额分配怎么申请"]),
        list(ingest.posts),
        keyword_id=keyword_id,
        operation_id=int(
            storage.conn.execute(
                "SELECT operation_id FROM post_observations WHERE run_id = ?", (storage.run_id,)
            ).fetchone()["operation_id"]
        ),
        attempt_no=1,
        matcher=MATCHER,
        normalizer=NORMALIZER,
        analysis_version=PRELIMINARY_ANALYSIS_VERSION,
    )
    merger = PreliminaryMerger(storage.repo, storage.run_id)
    preliminary = merger.merge(records, seen_at=at(1))
    snapshot = rows(
        storage.conn, "SELECT * FROM evidence WHERE analysis_version = ? ORDER BY id",
        (PRELIMINARY_ANALYSIS_VERSION,),
    )
    branch_before = rows(storage.conn, "SELECT * FROM branch_stats WHERE run_id = ?", (storage.run_id,))

    record_extraction(
        storage.repo, storage.run_id, extractor().extract(load_corpus(storage.conn, storage.run_id).units), at=at(5)
    )

    assert rows(
        storage.conn, "SELECT * FROM evidence WHERE analysis_version = ? ORDER BY id",
        (PRELIMINARY_ANALYSIS_VERSION,),
    ) == snapshot, "历史证据一字不改"
    assert rows(storage.conn, "SELECT * FROM branch_stats WHERE run_id = ?", (storage.run_id,)) == branch_before
    assert len(snapshot) == len(preliminary.evidence_ids)
    assert int(
        storage.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE analysis_version = ?", (ANALYSIS_VERSION,)
        ).fetchone()[0]
    ) > 0, "新版本自己有一份可核对的行"


def _search_result(storage, keyword_id: int, titles: list[str]) -> PostSearchResult:
    return PostSearchResult(
        state=ResultState.completed,
        context=context(storage, keyword_id, OperationType.search),
        session=session("s-1"),
        items=[
            PostCard(
                note_id=note_id_for(t),
                access_url=f"https://www.xiaohongshu.com/explore/{note_id_for(t)}",
                title=t,
                author=AuthorRef(kind="public_ref", public_ref=note_id_for(t)),
                published_at_text="2026-09-01",
                likes_text="12",
                result_rank=rank,
                search_keyword=KEYWORD,
            )
            for rank, t in enumerate(titles, start=1)
        ],
    )


def test_extractor_rejects_a_zero_cap_instead_of_producing_an_empty_run() -> None:
    with pytest.raises(ValueError):
        extractor(max_problems_per_unit=0)
