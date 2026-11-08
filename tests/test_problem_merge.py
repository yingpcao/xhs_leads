"""P0-18：在线初步归并与奖励更新。

守住的口径（DEVELOPMENT_PLAN 第 4/5.4 节）：
- 原句永远保留：归并只是可撤销的关系，不覆盖 `problems`/`evidence`；
- 只有"用户求助 + 命中求助意图"的标题才建有效簇，营销/机构/经验分享/纯资源标题只留观察；
- 签名相同但关键属性冲突时**分裂成不同簇**，缺失属性不算冲突；
- 同一次 attempt 重放不得写出第二条证据/问题/簇；
- 联想意图候选单独分区，永不 verified、不参与簇计数；
- 奖励只按当次新增有效簇计，重放拿到 0 簇时该分支的 stale_streak 必须上涨。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from xhs_discovery.config import load_intent_rules
from xhs_discovery.discovery.extract import (
    EXTRACTOR_VERSION,
    IntentCandidateRecord,
    ProblemRecord,
    attributes_for,
    core_text,
    extract_problems,
    select_quote,
)
from xhs_discovery.discovery.ingest import PostIngest, ingest_search
from xhs_discovery.discovery.merge import (
    MERGE_METHOD,
    PRELIMINARY_ANALYSIS_VERSION,
    MergeOutcome,
    PreliminaryMerger,
    conflict_key,
)
from xhs_discovery.models.common import SessionRef, sha256_text
from xhs_discovery.models.enums import (
    CandidateOrigin,
    OperationType,
    PostSourceType,
    ResultState,
    SourceField,
    StatementKind,
)
from xhs_discovery.models.evidence import (
    Evidence,
    EvidenceProvenance,
    ProblemAttributes,
    ProblemCandidate,
)
from xhs_discovery.models.run import RewardUpdate
from xhs_discovery.models.source import (
    AuthorRef,
    KeywordText,
    OperationContext,
    PostCard,
    PostSearchResult,
)
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.normalize import KeywordNormalizer

from tests.conftest import T0, build_run_identity

ROOT = Path(__file__).resolve().parents[1]
MATCHER = IntentMatcher(load_intent_rules(ROOT / "config" / "intent_rules.yaml"))
NORMALIZER = KeywordNormalizer(
    ambiguous_seeds=("上海",), locations=("上海", "北京", "浦东", "闵行", "广州")
)

HELP = "上海中考名额分配怎么申请"
HELP_OTHER_NOTE = "上海中考名额分配怎么申请"  # 同文不同帖：应并入同一簇
NEGATIVE = "上海2025中考考不上怎么办"
MARKETING = "上海中考辅导机构一对一报名"
INSTITUTION = "上海教育考试院公告中考时间"
SHARE = "上海中考上岸经验分享"
RESOURCE = "上海中考数学真题资料"
QUERY_ONLY = "上海中考建档线什么时候公布"


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def note_id_for(title: str) -> str:
    """同一标题在多次观察里指向同一帖子身份——重放/换 attempt 才会命中已有证据。"""
    return f"n-{sha256_text(title)[:12]}"


def card(title: str, rank: int, *, keyword: str = "上海中考", note_id: str | None = None) -> PostCard:
    nid = note_id or note_id_for(title)
    return PostCard(
        note_id=nid,
        access_url=f"https://www.xiaohongshu.com/explore/{nid}",
        title=title,
        author=AuthorRef(kind="public_ref", public_ref=f"u-{nid}"),
        published_at_text="2026-09-01",
        likes_text="12",
        result_rank=rank,
        search_keyword=KeywordText(raw=keyword, normalized=keyword),
    )


def blank_card(rank: int) -> PostCard:
    nid = f"n-no-title-{rank}"
    return PostCard(
        note_id=nid,
        access_url=f"https://www.xiaohongshu.com/explore/{nid}",
        title=None,
        title_missing=True,
        author=AuthorRef(kind="unknown"),
        published_at_missing=True,
        likes_text=None,
        result_rank=rank,
        search_keyword=KeywordText(raw="上海中考", normalized="上海中考"),
    )


@dataclass
class MergeEnv:
    repo: Repository
    run_id: str
    keyword_id: int
    keyword: KeywordText
    context: OperationContext
    session: SessionRef
    merger: PreliminaryMerger
    base_operation_id: int
    branch_key: str = "b-main"

    @property
    def conn(self) -> sqlite3.Connection:
        return self.repo.conn

    # -------------------------------------------------------------- 真实链路

    def observe(
        self,
        titles: list[str],
        *,
        keyword_text: str = "上海中考",
        revisit: int = 0,
        attempt_no: int = 1,
        session_id: str | None = None,
        cards: list[PostCard] | None = None,
    ) -> tuple[list[ProblemRecord], MergeOutcome, PostIngest]:
        """搜索观察 → 入库 → 抽取 → 归并，一次走完整链路。"""
        norm = NORMALIZER.normalize(keyword_text)
        keyword_id = self.repo.ensure_keyword(
            norm.normalized, keyword_text, seen_at=self.session.collected_at
        )
        self.repo.upsert_run_keyword(
            self.run_id,
            keyword_id,
            depth=0,
            origin=CandidateOrigin.observed.value,
            status="queued",
            seen_at=self.session.collected_at,
        )
        context = OperationContext(
            run_id=self.run_id,
            keyword_id=keyword_id,
            keyword=KeywordText(raw=keyword_text, normalized=norm.normalized),
            operation_type=OperationType.search,
            revisit_seq=revisit,
            attempt_no=attempt_no,
        )
        session = self.session
        if session_id is not None:
            session = self.session.model_copy(update={"session_id": session_id})
        items = cards if cards is not None else [
            card(title, rank + 1, keyword=keyword_text) for rank, title in enumerate(titles)
        ]
        result = PostSearchResult(
            state=ResultState.completed, context=context, session=session, items=items
        )
        operation_id = self.repo.open_operation(
            self.run_id, keyword_id, OperationType.search, revisit, branch_key=self.branch_key
        )
        attempt_id = self.repo.start_attempt(operation_id, attempt_no)
        ingest = ingest_search(
            self.repo, self.run_id, result, operation_id=operation_id, attempt_id=attempt_id
        )
        records = extract_problems(
            self.run_id,
            result,
            list(ingest.posts),
            keyword_id=keyword_id,
            operation_id=operation_id,
            attempt_no=attempt_no,
            matcher=MATCHER,
            normalizer=NORMALIZER,
            analysis_version=self.merger.analysis_version,
        )
        outcome = self.merger.merge(records, seen_at=self.session.collected_at)
        return records, outcome, ingest

    def replay(self, records: list[ProblemRecord], *, at_offset_minutes: int = 5) -> MergeOutcome:
        """同一次抽取再归并一遍（模拟崩溃后补导入）。"""
        return self.merger.merge(
            records, seen_at=self.session.collected_at + timedelta(minutes=at_offset_minutes)
        )

    # -------------------------------------------------------------- 手工记录

    def seed_post(self, note_id: str, title: str) -> tuple[int, int]:
        author_id = self.repo.ensure_author(public_ref=f"u-{note_id}", kind="public_ref")
        post_id = self.repo.ensure_post(
            note_id, f"https://www.xiaohongshu.com/explore/{note_id}", seen_at=T0, author_id=author_id
        )
        version_id = self.repo.ensure_content_version(post_id, "title", title, captured_at=T0)
        return post_id, version_id

    def manual_record(
        self,
        *,
        container: str,
        quote: str,
        canonical: str,
        note_id: str,
        post_id: int,
        version_id: int,
        attributes: ProblemAttributes,
        source_type: PostSourceType = PostSourceType.user_help,
        keyword_id: int | None = None,
        operation_id: int | None = None,
        attempt_no: int = 1,
        counts_as_valid: bool = True,
    ) -> ProblemRecord:
        provenance = EvidenceProvenance(
            run_id=self.run_id,
            operation=self.context,
            session_id=self.session.session_id,
            keyword=self.keyword,
            note_id=note_id,
        )
        evidence = Evidence.locate(
            container_text=container,
            quote=quote,
            source_field=SourceField.title,
            provenance=provenance,
            extractor_version=EXTRACTOR_VERSION,
            analysis_version=self.merger.analysis_version,
            synthetic=True,
            content_version_id=version_id,
        )
        return ProblemRecord(
            candidate=ProblemCandidate(
                canonical_text=canonical,
                source_type=source_type,
                attributes=attributes,
                evidence=evidence,
            ),
            keyword_id=keyword_id or self.keyword_id,
            post_id=post_id,
            operation_id=operation_id or self.base_operation_id,
            attempt_no=attempt_no,
            counts_as_valid=counts_as_valid,
        )

    def help_attributes(self, **overrides) -> ProblemAttributes:
        payload = dict(topic="上海中考", intent="method_guidance", pain_point="怎么")
        payload.update(overrides)
        return ProblemAttributes(**payload)

    def need(self, need_id: int) -> dict:
        row = rows(self.conn, "SELECT * FROM needs WHERE id = ?", (need_id,))
        assert row
        return row[0]

    def detail(self, kind: str) -> dict:
        events = self.repo.events(self.run_id, kind=kind)
        assert events, f"缺少 {kind} 事件"
        return json.loads(events[-1]["detail_json"])


@pytest.fixture
def env(storage) -> MergeEnv:
    repo = storage.repo
    repo.ensure_branch(storage.run_id, "b-main", label="主分支")
    norm = NORMALIZER.normalize("上海中考")
    keyword_id = repo.ensure_keyword(norm.normalized, "上海中考", seen_at=T0)
    repo.upsert_run_keyword(
        storage.run_id,
        keyword_id,
        depth=0,
        origin=CandidateOrigin.observed.value,
        status="queued",
        seen_at=T0,
    )
    context = OperationContext(
        run_id=storage.run_id,
        keyword_id=keyword_id,
        keyword=KeywordText(raw="上海中考", normalized=norm.normalized),
        operation_type=OperationType.search,
        revisit_seq=0,
        attempt_no=1,
    )
    session = SessionRef(
        session_id="sess-merge-1",
        collected_at=T0,
        source_name="fixture",
        source_version="0.0.1+test",
        synthetic=True,
    )
    operation_id = repo.open_operation(
        storage.run_id, keyword_id, OperationType.search, 0, branch_key="b-main"
    )
    return MergeEnv(
        repo=repo,
        run_id=storage.run_id,
        keyword_id=keyword_id,
        keyword=context.keyword,
        context=context,
        session=session,
        merger=PreliminaryMerger(repo, storage.run_id, extractor_version=EXTRACTOR_VERSION),
        base_operation_id=operation_id,
    )


# ---------------------------------------------------------------------- 建簇


def test_help_title_creates_verified_cluster(env: MergeEnv) -> None:
    records, outcome, _ = env.observe([HELP])
    assert len(records) == 1
    assert outcome.new_valid_clusters == 1
    assert outcome.valid_evidence_ids == outcome.evidence_ids
    need = env.need(outcome.need_ids[0])
    assert need["statement_kind"] == StatementKind.explicit_problem.value
    assert need["evidence_status"] == "verified"
    assert need["canonical_text"] == HELP
    assert need["analysis_version"] == PRELIMINARY_ANALYSIS_VERSION
    payload = json.loads(need["evidence_complete_json"])
    assert payload["evidence_ids"] == list(outcome.evidence_ids)
    assert payload["attributes"]["location"] == "上海"
    assert payload["attributes"]["intent"] == "method_guidance"
    assignment = rows(
        env.conn, "SELECT * FROM cluster_assignments WHERE need_id = ?", (need["id"],)
    )[0]
    assert assignment["method"] == MERGE_METHOD
    # 新建簇不是"自动并入已有簇"，因此不写相似度
    assert assignment["auto_merged"] == 0
    assert assignment["similarity"] is None
    assert assignment["attributes_conflict_json"] is None


def test_quote_selects_the_asking_clause_and_stays_locatable(env: MergeEnv) -> None:
    title = "上海中考临近，名额分配怎么申请？"
    assert select_quote(title, MATCHER) == "名额分配怎么申请"
    assert core_text("名额分配怎么申请？") == "名额分配怎么申请"
    records, outcome, _ = env.observe([title])
    assert len(records) == 1
    evidence = rows(env.conn, "SELECT * FROM evidence WHERE id = ?", (outcome.evidence_ids[0],))[0]
    assert evidence["quote"] == "名额分配怎么申请"
    assert evidence["char_start"] > 0
    title_text = str(evidence["quote"])
    container = env.conn.execute(
        "SELECT text FROM post_content_versions WHERE id = ?", (evidence["content_version_id"],)
    ).fetchone()["text"]
    assert container[evidence["char_start"] : evidence["char_end"]] == title_text


def test_non_help_source_types_stay_observations_only(env: MergeEnv) -> None:
    _, outcome, _ = env.observe([MARKETING, INSTITUTION, SHARE, RESOURCE])
    assert outcome.new_valid_clusters == 0
    assert outcome.need_ids == ()
    assert outcome.valid_evidence_ids == ()
    assert len(outcome.problem_ids) == 4
    stored = rows(env.conn, "SELECT source_type, counts_as_valid_problem_cluster FROM problems")
    assert {row["source_type"] for row in stored} == {
        PostSourceType.marketing.value,
        PostSourceType.institution.value,
        PostSourceType.experience_share.value,
        PostSourceType.unknown.value,
    }
    assert all(int(row["counts_as_valid_problem_cluster"]) == 0 for row in stored)
    assert env.merger.valid_cluster_count() == 0


def test_intent_pattern_without_help_cue_is_not_counted(env: MergeEnv) -> None:
    """"什么时候"命中信息查询，但标题没有任何求助线索：宁缺毋滥，不建簇。"""
    records, outcome, _ = env.observe([QUERY_ONLY])
    assert len(records) == 1
    assert records[0].candidate.attributes.intent == "information_query"
    assert records[0].counts_as_valid is False
    assert outcome.new_valid_clusters == 0
    assert env.merger.valid_cluster_count() == 0


def test_negative_polarity_and_year_are_kept_as_attributes(env: MergeEnv) -> None:
    records, outcome, _ = env.observe([NEGATIVE])
    attributes = records[0].candidate.attributes
    assert attributes.year == "2025"
    assert attributes.polarity == "negative"
    need = env.need(outcome.need_ids[0])
    assert json.loads(need["evidence_complete_json"])["attributes"]["year"] == "2025"


def test_card_without_title_produces_no_record(env: MergeEnv) -> None:
    records, outcome, ingest = env.observe([], cards=[card(HELP, 1), blank_card(2)])
    assert len(ingest.posts) == 2
    assert len(records) == 1  # 无标题卡片不可定位，不产生问题候选
    assert outcome.new_valid_clusters == 1


def test_attributes_for_marks_generated_topic_from_context(env: MergeEnv) -> None:
    attributes, intent = attributes_for(
        HELP, topic="上海中考", matcher=MATCHER, normalizer=NORMALIZER
    )
    assert intent == "method_guidance"
    assert attributes.topic == "上海中考"
    assert attributes.pain_point == "怎么"


# ---------------------------------------------------------------------- 幂等


def test_replay_of_same_records_writes_nothing_new(env: MergeEnv) -> None:
    records, first, _ = env.observe([HELP, NEGATIVE])
    before = (
        count(env.conn, "SELECT COUNT(*) FROM evidence"),
        count(env.conn, "SELECT COUNT(*) FROM problems"),
        count(env.conn, "SELECT COUNT(*) FROM needs"),
        count(env.conn, "SELECT COUNT(*) FROM cluster_assignments"),
    )
    second = env.replay(records)
    after = (
        count(env.conn, "SELECT COUNT(*) FROM evidence"),
        count(env.conn, "SELECT COUNT(*) FROM problems"),
        count(env.conn, "SELECT COUNT(*) FROM needs"),
        count(env.conn, "SELECT COUNT(*) FROM cluster_assignments"),
    )
    assert after == before
    assert second.reused == len(records)
    assert second.new_valid_clusters == 0
    assert second.need_ids == first.need_ids
    assert second.problem_ids == first.problem_ids
    assert env.merger.valid_cluster_count() == 2


def test_repeat_observation_merges_into_existing_cluster(env: MergeEnv) -> None:
    env.observe([HELP])
    _, outcome, ingest = env.observe([HELP], revisit=1, attempt_no=1)
    assert ingest.created is True  # 新的操作与观察行
    assert outcome.reused == 1  # 但证据/问题按自然键复用
    assert outcome.new_valid_clusters == 0
    assert outcome.merged_into_existing == 1
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == 1
    assert count(env.conn, "SELECT COUNT(*) FROM post_observations") == 2


def test_same_title_on_two_notes_merges_into_one_need(env: MergeEnv) -> None:
    cards = [
        card(HELP_OTHER_NOTE, 1, note_id="n-dup-a"),
        card(HELP_OTHER_NOTE, 2, note_id="n-dup-b"),
    ]
    _, outcome, _ = env.observe([], cards=cards)
    assert len(outcome.evidence_ids) == 2
    assert len(outcome.problem_ids) == 2
    assert outcome.new_valid_clusters == 1
    need = env.need(outcome.need_ids[0])
    payload = json.loads(need["evidence_complete_json"])
    assert len(payload["evidence_ids"]) == 2
    assert len(payload["problem_ids"]) == 2
    assignments = rows(
        env.conn, "SELECT * FROM cluster_assignments WHERE need_id = ?", (need["id"],)
    )
    assert len(assignments) == 2
    # 第二条才是"自动并入已有簇"，必须记相似度供复核阈值
    auto = [row for row in assignments if int(row["auto_merged"]) == 1]
    assert len(auto) == 1
    assert auto[0]["similarity"] == 1.0


def test_merge_rolls_back_when_a_record_violates_a_constraint(env: MergeEnv) -> None:
    post_id, version_id = env.seed_post("n-atomic", HELP)
    good = env.manual_record(
        container=HELP,
        quote=HELP,
        canonical=HELP,
        note_id="n-atomic",
        post_id=post_id,
        version_id=version_id,
        attributes=env.help_attributes(location="上海"),
    )
    bad = env.manual_record(
        container=HELP,
        quote=HELP,
        canonical="另一条问题",
        note_id="n-atomic",
        post_id=post_id,
        version_id=999999,  # 不存在的内容版本：FK 必须拒绝
        attributes=env.help_attributes(),
    )
    before = count(env.conn, "SELECT COUNT(*) FROM needs")
    with pytest.raises(sqlite3.IntegrityError):
        env.merger.merge([good, bad], seen_at=T0)
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == before
    assert count(env.conn, "SELECT COUNT(*) FROM evidence") == 0
    assert count(env.conn, "SELECT COUNT(*) FROM problems") == 0


# ---------------------------------------------------------------------- 冲突


def test_attribute_conflict_splits_into_a_new_cluster(env: MergeEnv) -> None:
    a_id, a_version = env.seed_post("n-conflict-a", "上海中考名额分配怎么申请")
    b_id, b_version = env.seed_post("n-conflict-b", "北京中考名额分配怎么申请")
    canonical = "中考名额分配怎么申请"
    first = env.manual_record(
        container="上海中考名额分配怎么申请",
        quote="上海中考名额分配怎么申请",
        canonical=canonical,
        note_id="n-conflict-a",
        post_id=a_id,
        version_id=a_version,
        attributes=env.help_attributes(location="上海"),
    )
    second = env.manual_record(
        container="北京中考名额分配怎么申请",
        quote="北京中考名额分配怎么申请",
        canonical=canonical,
        note_id="n-conflict-b",
        post_id=b_id,
        version_id=b_version,
        attributes=env.help_attributes(location="北京"),
    )
    env.merger.merge([first], seen_at=T0)
    outcome = env.merger.merge([second], seen_at=T0)
    assert outcome.new_valid_clusters == 1
    assert len(outcome.conflicts) == 1
    assert outcome.conflicts[0]["conflicts"] == ["location"]
    split = env.need(outcome.need_ids[0])
    assert split["canonical_text"] == conflict_key(canonical, ["location"], second.candidate.attributes)
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == 2
    assignment = rows(
        env.conn, "SELECT * FROM cluster_assignments WHERE need_id = ?", (split["id"],)
    )[0]
    assert int(assignment["auto_merged"]) == 0
    assert assignment["similarity"] is None
    assert json.loads(assignment["attributes_conflict_json"]) == ["location"]
    # 原句永远保留：冲突分裂不删除任何一条问题
    assert count(env.conn, "SELECT COUNT(*) FROM problems") == 2


def test_second_conflicting_value_reuses_the_same_split_cluster(env: MergeEnv) -> None:
    post_id, version_a = env.seed_post("n-split-a", "北京中考名额分配怎么申请")
    _, version_b = env.seed_post("n-split-b", "广州中考名额分配怎么申请")
    _, version_sh = env.seed_post("n-split-c", "上海中考名额分配怎么申请")
    canonical = "中考名额分配怎么申请"

    def rec(version: int, note: str, location: str) -> ProblemRecord:
        return env.manual_record(
            container=f"{location}中考名额分配怎么申请",
            quote=f"{location}中考名额分配怎么申请",
            canonical=canonical,
            note_id=note,
            post_id=post_id,
            version_id=version,
            attributes=env.help_attributes(location=location),
        )

    env.merger.merge([rec(version_sh, "n-split-c", "上海")], seen_at=T0)
    first = env.merger.merge([rec(version_a, "n-split-a", "北京")], seen_at=T0)
    second = env.merger.merge([rec(version_b, "n-split-b", "广州")], seen_at=T0)
    assert len(first.need_ids) == 1
    assert len(second.need_ids) == 1
    # 三个不同地域 = 三个簇：分裂键按取值区分，不会把广州并进北京
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == 3
    assert second.new_valid_clusters == 1


def test_missing_attribute_is_not_a_conflict(env: MergeEnv) -> None:
    post_id, version_full = env.seed_post("n-miss-a", HELP)
    _, version_partial = env.seed_post("n-miss-b", HELP)
    full = env.manual_record(
        container=HELP,
        quote=HELP,
        canonical=HELP,
        note_id="n-miss-a",
        post_id=post_id,
        version_id=version_full,
        attributes=env.help_attributes(location="上海", year="2025"),
    )
    partial = env.manual_record(
        container=HELP,
        quote=HELP,
        canonical=HELP,
        note_id="n-miss-b",
        post_id=post_id,
        version_id=version_partial,
        attributes=env.help_attributes(location="上海"),
    )
    env.merger.merge([full], seen_at=T0)
    outcome = env.merger.merge([partial], seen_at=T0)
    assert outcome.conflicts == ()
    assert outcome.new_valid_clusters == 0
    assert outcome.merged_into_existing == 1
    attributes = json.loads(env.need(outcome.need_ids[0])["evidence_complete_json"])["attributes"]
    assert attributes["year"] == "2025"  # 已有值不被"缺失"抹掉


def test_topic_conflict_when_same_note_seen_under_two_keywords(env: MergeEnv) -> None:
    """同一篇帖子在两个关键词下被召回：文本一致但 topic 冲突，因此分裂成两簇。"""
    env.observe([HELP])
    _, outcome, _ = env.observe([HELP], keyword_text="上海中职")
    assert len(outcome.conflicts) == 1
    assert outcome.conflicts[0]["conflicts"] == ["topic"]
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == 2
    # 归并关系受 UNIQUE(run, problem, analysis_version) 约束：同一问题在一个分析版本里只有一条归属
    assert count(env.conn, "SELECT COUNT(*) FROM cluster_assignments") == 1


def test_conflict_key_is_deterministic_and_sorted() -> None:
    attributes = ProblemAttributes(
        topic="上海中考", location="上海", year="2025", intent="method_guidance"
    )
    first = conflict_key("怎么申请", ["year", "location"], attributes)
    second = conflict_key("怎么申请", ["location", "year"], attributes)
    assert first == second == "怎么申请#conflict[location=上海,year=2025]"


# ------------------------------------------------------------------ 意图候选


def test_intent_candidates_are_never_verified_or_counted(env: MergeEnv) -> None:
    records = [
        IntentCandidateRecord(
            text="上海中考怎么志愿排序",
            canonical_text="上海中考怎么志愿排序",
            rank=1,
            observation_id=7,
            keyword_id=env.keyword_id,
        )
    ]
    created = env.merger.record_intent_candidates(records, seen_at=T0, synthetic=True)
    assert created == 1
    need = rows(
        env.conn,
        "SELECT * FROM needs WHERE statement_kind = ?",
        (StatementKind.search_intent_candidate.value,),
    )[0]
    assert need["evidence_status"] == "unverified_candidate"
    assert need["canonical_text"] == "上海中考怎么志愿排序"
    assert json.loads(need["evidence_complete_json"])["evidence_ids"] == []
    assert env.merger.valid_cluster_count() == 0
    assert env.detail("intent_candidates_recorded") == {"count": 1, "synthetic": True}


def test_intent_candidate_replay_does_not_duplicate(env: MergeEnv) -> None:
    record = IntentCandidateRecord(
        text="上海中考志愿怎么排序",
        canonical_text="上海中考志愿怎么排序",
        rank=2,
        observation_id=7,
        keyword_id=env.keyword_id,
    )
    assert env.merger.record_intent_candidates([record], seen_at=T0, synthetic=True) == 1
    assert env.merger.record_intent_candidates([record], seen_at=T0, synthetic=True) == 0
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == 1
    # 没有新增时不重复记事件
    assert len(env.repo.events(env.run_id, kind="intent_candidates_recorded")) == 1


def test_intent_candidate_does_not_collide_with_explicit_problem(env: MergeEnv) -> None:
    env.observe([HELP])
    same_text = [
        IntentCandidateRecord(
            text=HELP, canonical_text=HELP, rank=1, observation_id=9, keyword_id=env.keyword_id
        )
    ]
    assert env.merger.record_intent_candidates(same_text, seen_at=T0, synthetic=True) == 1
    kinds = {
        str(row["statement_kind"])
        for row in rows(env.conn, "SELECT statement_kind FROM needs WHERE canonical_text = ?", (HELP,))
    }
    assert kinds == {StatementKind.explicit_problem.value, StatementKind.search_intent_candidate.value}
    assert env.merger.valid_cluster_count() == 1


# -------------------------------------------------------------------- 事件


def test_merge_event_reports_the_accounting(env: MergeEnv) -> None:
    _, outcome, _ = env.observe([HELP, NEGATIVE])
    detail = env.detail("preliminary_merge")
    assert detail["records"] == 2
    assert detail["new_valid_clusters"] == 2
    assert detail["merged_into_existing"] == 0
    assert detail["conflicts"] == 0
    assert detail["reused"] == 0
    assert detail["analysis_version"] == PRELIMINARY_ANALYSIS_VERSION
    replay = env.replay([])
    assert env.detail("preliminary_merge")["records"] == 0
    assert replay.evidence_ids == ()


def test_statistics_helpers(env: MergeEnv) -> None:
    env.observe([HELP, NEGATIVE, MARKETING])
    assert env.merger.valid_cluster_count() == 2
    assert env.merger.distinct_problem_quotes() == 3
    assert env.merger.source_type_breakdown() == {
        PostSourceType.marketing.value: 1,
        PostSourceType.user_help.value: 2,
    }


# ---------------------------------------------------------------------- 奖励


def reward_for(outcome: MergeOutcome, *, keywords: int = 0, cost: float = 1.0) -> RewardUpdate:
    return RewardUpdate(
        run_id="run-a",
        keyword_id=1,
        operation_type=OperationType.search,
        revisit_seq=0,
        attempt_no=1,
        attributed_branch_id="b-main",
        new_valid_problem_clusters=outcome.new_valid_clusters,
        new_related_keywords=keywords,
        normalized_cost=cost,
        cluster_keyword_weight=0.2,
        result_state=ResultState.completed,
        valid_cluster_evidence_ids=list(outcome.valid_evidence_ids),
    )


def test_reward_counts_only_new_clusters(env: MergeEnv) -> None:
    _, first, _ = env.observe([HELP, NEGATIVE])
    stat = env.repo.apply_reward(reward_for(first, keywords=3), at=T0)
    assert stat.trials == 1
    assert stat.total_reward == pytest.approx((2 + 0.2 * 3) / 1.0)
    assert stat.stale_streak == 0

    replay = env.replay([])
    assert replay.new_valid_clusters == 0
    idle = env.repo.apply_reward(
        RewardUpdate(
            run_id="run-a",
            keyword_id=env.keyword_id,
            operation_type=OperationType.search,
            revisit_seq=1,
            attempt_no=1,
            attributed_branch_id="b-main",
            new_valid_problem_clusters=0,
            new_related_keywords=0,
            normalized_cost=1.0,
            cluster_keyword_weight=0.2,
            result_state=ResultState.completed,
            valid_cluster_evidence_ids=[],
        ),
        at=T0,
    )
    assert idle.trials == 2
    assert idle.total_reward == pytest.approx(stat.total_reward)
    assert idle.stale_streak == 1


def test_clusters_require_locatable_evidence(env: MergeEnv) -> None:
    with pytest.raises(ValueError, match="可定位的显性问题证据"):
        RewardUpdate(
            run_id="run-a",
            keyword_id=env.keyword_id,
            operation_type=OperationType.search,
            revisit_seq=0,
            attempt_no=1,
            attributed_branch_id="b-main",
            new_valid_problem_clusters=1,
            new_related_keywords=0,
            normalized_cost=1.0,
            cluster_keyword_weight=0.2,
            result_state=ResultState.completed,
            valid_cluster_evidence_ids=[],
        )


def test_suggest_operation_cannot_claim_content_clusters(env: MergeEnv) -> None:
    with pytest.raises(ValueError, match="只做联想的操作"):
        RewardUpdate(
            run_id="run-a",
            keyword_id=env.keyword_id,
            operation_type=OperationType.suggest,
            revisit_seq=0,
            attempt_no=1,
            attributed_branch_id="b-main",
            new_valid_problem_clusters=1,
            new_related_keywords=2,
            normalized_cost=1.0,
            cluster_keyword_weight=0.2,
            result_state=ResultState.completed,
            valid_cluster_evidence_ids=[1],
        )


def test_failed_operation_grants_no_new_reward(env: MergeEnv) -> None:
    with pytest.raises(ValueError, match="失败操作不得记新增奖励"):
        RewardUpdate(
            run_id="run-a",
            keyword_id=env.keyword_id,
            operation_type=OperationType.search,
            revisit_seq=0,
            attempt_no=1,
            attributed_branch_id="b-main",
            new_valid_problem_clusters=1,
            new_related_keywords=0,
            normalized_cost=1.0,
            cluster_keyword_weight=0.2,
            result_state=ResultState.failed,
            valid_cluster_evidence_ids=[1],
        )


# ------------------------------------------------------------------- run 隔离


def test_clusters_are_scoped_per_run(env: MergeEnv) -> None:
    """簇身份是 (run, 签名)：换 run 必须重新建自己的簇，不复用上一个 run 的结论。"""
    _, first, _ = env.observe([HELP])
    other = build_run_identity("run-b")
    env.repo.create_run(other, config_json='{"budget": {}}')
    env.repo.ensure_branch("run-b", "b-main", label="主分支")
    keyword_id = env.repo.ensure_keyword(NORMALIZER.normalize("上海中考").normalized, "上海中考", seen_at=T0)
    env.repo.upsert_run_keyword(
        "run-b", keyword_id, depth=0, origin=CandidateOrigin.observed.value, status="queued", seen_at=T0
    )
    operation_id = env.repo.open_operation(
        "run-b", keyword_id, OperationType.search, 0, branch_key="b-main"
    )
    attempt_id = env.repo.start_attempt(operation_id, 1)
    context = OperationContext(
        run_id="run-b",
        keyword_id=keyword_id,
        keyword=KeywordText(raw="上海中考", normalized="上海中考"),
        operation_type=OperationType.search,
        revisit_seq=0,
        attempt_no=1,
    )
    result = PostSearchResult(
        state=ResultState.completed,
        context=context,
        session=env.session,
        items=[card(HELP, 1)],
    )
    ingest = ingest_search(env.repo, "run-b", result, operation_id=operation_id, attempt_id=attempt_id)
    merger_b = PreliminaryMerger(env.repo, "run-b", extractor_version=EXTRACTOR_VERSION)
    records = extract_problems(
        "run-b",
        result,
        list(ingest.posts),
        keyword_id=keyword_id,
        operation_id=operation_id,
        attempt_no=1,
        matcher=MATCHER,
        normalizer=NORMALIZER,
        analysis_version=merger_b.analysis_version,
    )
    second = merger_b.merge(records, seen_at=T0)
    assert second.new_valid_clusters == 1
    assert set(second.need_ids).isdisjoint(set(first.need_ids))
    assert count(env.conn, "SELECT COUNT(*) FROM needs") == 2
    assert merger_b.valid_cluster_count() == 1
    # 帖子身份是全局的，跨 run 不重复建
    assert count(env.conn, "SELECT COUNT(*) FROM posts WHERE note_id = ?", (note_id_for(HELP),)) == 1
