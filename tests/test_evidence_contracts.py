"""P0-04：证据、问题、需求与归并契约。不可定位的引用一律拒收。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xhs_discovery.models.enums import (
    CandidateOrigin,
    PostSourceType,
    SourceField,
    StatementKind,
)
from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.evidence import (
    ClusterAssignment,
    ContentFamily,
    Evidence,
    EvidenceProvenance,
    KeywordCandidate,
    NeedStatement,
    ProblemAttributes,
    ProblemCandidate,
)
from xhs_discovery.models.scoring import WeightedScore, feature
from xhs_discovery.models.source import KeywordText

TITLE = "上海中考志愿到底怎么排序，求助有经验的家长"


def test_evidence_locate_computes_offsets_and_verifies_against_text(evidence_factory):
    ev = evidence_factory(TITLE, "志愿到底怎么排序", source_field=SourceField.title)
    assert ev.char_start == TITLE.index("志愿到底怎么排序")
    assert ev.char_end == ev.char_start + len("志愿到底怎么排序")
    assert ev.locates_in(TITLE) is True
    assert ev.container_sha256 == sha256_text(TITLE)
    assert ev.synthetic is True


def test_quote_absent_from_the_versioned_text_is_refused(evidence_factory):
    with pytest.raises(ValueError, match="不存在"):
        evidence_factory(TITLE, "正文里根本没有这句话")


def test_hand_tampered_offsets_are_rejected(evidence_factory):
    ev = evidence_factory(TITLE, "志愿到底怎么排序")
    tampered = ev.model_dump()
    tampered["char_start"] = tampered["char_start"] + 1
    with pytest.raises(ValidationError, match="定位"):
        Evidence.model_validate(tampered)


def test_container_text_change_invalidates_the_evidence(evidence_factory):
    ev = evidence_factory(TITLE, "志愿到底怎么排序")
    assert ev.locates_in(TITLE.replace("求助", "求 助")) is False


def test_occurrence_selects_the_nth_match(evidence_factory):
    text = "怎么排志愿？怎么排才对？"
    second = evidence_factory(text, "怎么排", source_field=SourceField.suggestion, note_id=None, occurrence=1)
    assert second.char_start == text.index("怎么排", 1)
    with pytest.raises(ValueError, match="不存在"):
        evidence_factory(text, "怎么排", source_field=SourceField.suggestion, note_id=None, occurrence=2)


def test_suggestion_evidence_has_no_note_id_but_title_does(evidence_factory):
    ev = evidence_factory(TITLE, "志愿到底怎么排序", source_field=SourceField.suggestion, note_id=None, rank=3)
    assert ev.provenance.note_id is None
    assert ev.provenance.suggestion_rank == 3
    with pytest.raises(ValidationError, match="suggestion 证据不应携带 note_id"):
        evidence_factory(TITLE, "志愿到底怎么排序", source_field=SourceField.suggestion)
    with pytest.raises(ValidationError, match="title/body 证据必须携带 note_id"):
        evidence_factory(TITLE, "志愿到底怎么排序", source_field=SourceField.body, note_id=None)


def test_run_id_mismatch_between_provenance_and_operation_is_rejected(evidence_factory):
    ev = evidence_factory(TITLE, "志愿到底怎么排序")
    payload = ev.model_dump()
    payload["provenance"]["run_id"] = "run-b"
    with pytest.raises(ValidationError, match="run_id 与操作上下文不一致"):
        Evidence.model_validate(payload)
    provenance = EvidenceProvenance.model_validate(ev.model_dump()["provenance"])
    assert provenance.operation.run_id == "run-a"


def attributes(**kw) -> ProblemAttributes:
    base = dict(topic="中考志愿", object="志愿填报", intent="comparison_decision", pain_point="不会排序")
    base.update(kw)
    return ProblemAttributes(**base)


def test_conflicting_location_year_or_polarity_are_detected(evidence_factory):
    sh = ProblemCandidate(
        canonical_text="上海中考志愿怎么排",
        source_type=PostSourceType.user_help,
        attributes=attributes(location="上海", year="2026"),
        evidence=evidence_factory(TITLE, "志愿到底怎么排序"),
    )
    other = attributes(location="北京", year="2026")
    assert sh.attributes.conflicts_with(other) == ["location"]
    assert sh.attributes.conflicts_with(attributes(location="上海", year="2025")) == ["year"]
    assert attributes(polarity="肯定").conflicts_with(attributes(polarity="否定")) == ["polarity"]
    assert sh.attributes.conflicts_with(attributes()) == []
    assert sh.attributes.conflicts_with(attributes(location=None, year=None)) == []
    assert attributes(location="上海", polarity="否定").conflicts_with(
        attributes(location="上海", polarity="否定")
    ) == []


def test_missing_attributes_are_not_treated_as_compatible_for_auto_merge(evidence_factory):
    known = attributes(location="上海")
    unknown = attributes(location=None)
    assert known.conflicts_with(unknown) == []
    p = ProblemCandidate(
        canonical_text="志愿怎么排",
        source_type=PostSourceType.user_help,
        attributes=unknown,
        evidence=evidence_factory(TITLE, "志愿到底怎么排序"),
    )
    assert p.attributes.location is None


def test_marketing_and_institution_posts_do_not_count_as_user_help(evidence_factory):
    for source_type in (PostSourceType.marketing, PostSourceType.institution,
                        PostSourceType.experience_share, PostSourceType.unknown):
        problem = ProblemCandidate(
            canonical_text="机构发布：中考志愿课程上线",
            source_type=source_type,
            attributes=attributes(),
            evidence=evidence_factory(TITLE, "志愿到底怎么排序"),
        )
        assert problem.counts_as_user_help is False
    assert ProblemCandidate(
        canonical_text="求助",
        source_type=PostSourceType.user_help,
        attributes=attributes(),
        evidence=evidence_factory(TITLE, "志愿到底怎么排序"),
    ).counts_as_user_help is True


def test_need_statements_keep_explicit_inferred_and_solution_separate(evidence_factory):
    need = NeedStatement(
        statement_kind=StatementKind.explicit_problem,
        canonical_text="获取上海中考志愿排序方法",
        attributes=attributes(),
        evidence_ids=[11],
        analysis_version="p0-rule-baseline.1",
        synthetic=True,
    )
    assert need.counts_as_valid_need is True
    with pytest.raises(ValidationError, match="至少引用一条 evidence_id"):
        NeedStatement(
            statement_kind=StatementKind.inferred_need,
            canonical_text="无证据推断",
            attributes=attributes(),
            evidence_ids=[],
            analysis_version="v",
            synthetic=True,
        )
    with pytest.raises(ValidationError, match="方案构想不能标记为已验证需求"):
        NeedStatement(
            statement_kind=StatementKind.solution_idea,
            canonical_text="基于成绩与地区的自动排序工具",
            attributes=attributes(),
            evidence_ids=[11],
            analysis_version="v",
            synthetic=True,
            verified=True,
        )
    idea = NeedStatement(
        statement_kind=StatementKind.solution_idea,
        canonical_text="基于成绩与地区的自动排序工具",
        attributes=attributes(),
        evidence_ids=[],
        analysis_version="v",
        synthetic=True,
    )
    assert idea.counts_as_valid_need is False
    with pytest.raises(ValidationError, match="不能冒充帖子事实"):
        NeedStatement(
            statement_kind=StatementKind.search_intent_candidate,
            canonical_text="上海中考志愿排序",
            attributes=attributes(),
            evidence_ids=[12],
            analysis_version="v",
            synthetic=True,
            verified=True,
        )


def test_auto_merge_is_blocked_by_attribute_conflict():
    with pytest.raises(ValidationError, match="禁止自动合并"):
        ClusterAssignment(
            problem_id=1,
            need_id=2,
            method="tfidf",
            similarity=0.91,
            attributes_conflict=["location"],
            auto_merged=True,
            analysis_version="v",
        )
    with pytest.raises(ValidationError, match="必须记录相似度"):
        ClusterAssignment(problem_id=1, need_id=2, method="tfidf", auto_merged=True, analysis_version="v")
    manual = ClusterAssignment(
        problem_id=1, need_id=2, method="tfidf", attributes_conflict=["location"], auto_merged=False,
        analysis_version="v",
    )
    assert manual.auto_merged is False


def test_content_family_requires_representative_hash():
    fam = ContentFamily(
        representative_sha256=sha256_text("近重复文本"), method="minhash", member_count=4,
        analysis_version="v",
    )
    assert fam.member_count == 4
    with pytest.raises(ValidationError):
        ContentFamily(representative_sha256="0" * 63, method="minhash", member_count=1, analysis_version="v")


def kw(text: str) -> KeywordText:
    return KeywordText(raw=text, normalized=text)


def test_generated_candidates_need_provenance_and_observed_need_parents():
    ok = KeywordCandidate(
        keyword=kw("上海中考 志愿排序"),
        origin=CandidateOrigin.observed,
        operator="suggest_vertical",
        parents=[kw("上海中考")],
        branch_id="b1",
        depth=1,
    )
    assert ok.origin is CandidateOrigin.observed
    with pytest.raises(ValidationError, match="至少一个真实父词"):
        KeywordCandidate(
            keyword=kw("上海中考 志愿排序"),
            origin=CandidateOrigin.observed,
            operator="suggest_vertical",
            parents=[],
            branch_id="b1",
            depth=1,
        )
    with pytest.raises(ValidationError, match="必须带来源片段引用"):
        KeywordCandidate(
            keyword=kw("上海中考 志愿怎么排"),
            origin=CandidateOrigin.generated,
            operator="problem_probe",
            branch_id="b1",
            depth=2,
            priority=WeightedScore.compute(
                {"I": feature(1.0, 1.0, 1.0)}, model_version="v"
            ),
        )
    probe = KeywordCandidate(
        keyword=kw("上海中考 志愿怎么排"),
        origin=CandidateOrigin.generated,
        operator="problem_probe",
        branch_id="b1",
        depth=2,
        source_evidence_ids=[7],
        priority=WeightedScore.compute({"I": feature(1.0, 1.0, 1.0)}, model_version="v"),
    )
    assert probe.source_evidence_ids == [7]
