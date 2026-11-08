"""P0-25/26：保守可撤销归并、稳定需求 ID 与四类陈述。

钉住的是"归并结论能不能被推翻而不丢历史"，不是归并得准不准：

- 显性簇只有在**问题行能找到可定位证据**时才成为需求（失效引用反例）；
- 同一条签名只在关键属性不冲突时合并，冲突走 `conflict_key` 分裂成另一行；缺失的属性
  记进 `attribute_gaps`，部分兼容不被粉饰成完全兼容；
- `needs.stable_key` 由 `{语句类别, 归并签名}` 决定，重放得到同一个键、不写第二行；
  换 `analysis_version` 时旧行一字不动，新批各写一行（键不含版本，版本是列）；
- 撤销是 `superseded_at` + `auto_merged = 0`，原句、证据、需求行全部留着，重新归并复用同一行；
- 显性问题 / 推断需求 / 方案构想 / 联想意图候选四类分区，后两类永不计入有效需求，
  营销与机构文案里的"自动填报系统"连构想都不登记。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from types import SimpleNamespace

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.clustering import (
    ATTRIBUTE_NAMES,
    MERGE_METHOD,
    REASON_ADVERTISED_TOOL,
    REASON_NO_EVIDENCE,
    REASON_NO_OBJECT,
    ConservativeMerger,
    NeedPlan,
    inferred_text,
    is_solution_idea,
    stable_key,
)
from xhs_discovery.analysis.corpus import load_corpus
from xhs_discovery.analysis.extraction import ProblemExtractor
from xhs_discovery.analysis.store import (
    assignment_row,
    record_clustering,
    record_extraction,
    revoke_cluster_assignment,
)
from xhs_discovery.discovery.ingest import ingest_search
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.enums import OperationType, ResultState, StatementKind
from xhs_discovery.models.evidence import ProblemAttributes
from xhs_discovery.models.source import (
    AuthorRef,
    KeywordText,
    OperationContext,
    PostCard,
    PostSearchResult,
)

from tests.conftest import T0
from tests.test_analysis_extraction import (
    LEXICON,
    MATCHER,
    NORMALIZER,
    at,
    observe_body,
    observe_suggestions,
    observe_titles,
    prepare,
    rows,
)

ZHONGKAO = KeywordText(raw="中考", normalized="中考")
HELP_QUESTION = "上海中考名额分配怎么申请"


def observe_under(
    storage, keyword_id: int, keyword: KeywordText, titles: list[str], *, tag: str, session_id: str
):
    """把同一句问题在**另一个主题词**下再观察一次：`topic` 冲突就是这么来的。"""
    cards = [
        PostCard(
            note_id=f"{tag}-{index}",
            access_url=f"https://www.xiaohongshu.com/explore/{tag}-{index}",
            title=title,
            author=AuthorRef(kind="public_ref", public_ref=f"u-{tag}-{index}"),
            published_at_text="2026-09-01",
            likes_text="12",
            result_rank=index,
            search_keyword=keyword,
        )
        for index, title in enumerate(titles, start=1)
    ]
    operation_id = storage.repo.open_operation(
        storage.run_id, keyword_id, OperationType.search, 0, branch_key="b-analysis"
    )
    attempt_id = storage.repo.start_attempt(operation_id, 1)
    return ingest_search(
        storage.repo,
        storage.run_id,
        PostSearchResult(
            state=ResultState.completed,
            context=OperationContext(
                run_id=storage.run_id,
                keyword_id=keyword_id,
                keyword=keyword,
                operation_type=OperationType.search,
                revisit_seq=0,
                attempt_no=1,
            ),
            session=SessionRef(
                session_id=session_id,
                collected_at=T0,
                source_name="fixture",
                source_version="0.0.1",
                synthetic=True,
            ),
            items=cards,
        ),
        operation_id=operation_id,
        attempt_id=attempt_id,
    )


def cluster(storage, *, analysis_version=ANALYSIS_VERSION, extractor_version=EXTRACTOR_VERSION):
    """抽取 → 入库 → 归并计划 → 落库，一次跑完：归并层只认库里的行。"""
    corpus = load_corpus(storage.conn, storage.run_id)
    extracted = ProblemExtractor(
        matcher=MATCHER,
        normalizer=NORMALIZER,
        lexicon=LEXICON,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
    )
    written = record_extraction(
        storage.repo, storage.run_id, extracted.extract(corpus.units), at=at(5)
    )
    outcome = ConservativeMerger(
        storage.repo,
        storage.run_id,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
        matcher=MATCHER,
    ).plan(corpus)
    write = record_clustering(storage.repo, storage.run_id, outcome, at=at(6))
    return SimpleNamespace(corpus=corpus, extraction=written, outcome=outcome, clustering=write)


def needs_of(storage, statement_kind: str, analysis_version: str = ANALYSIS_VERSION) -> list[dict]:
    return rows(
        storage.conn,
        "SELECT * FROM needs WHERE run_id = ? AND analysis_version = ? AND statement_kind = ?"
        " ORDER BY id",
        (storage.run_id, analysis_version, statement_kind),
    )


def explicit_of(storage, analysis_version: str = ANALYSIS_VERSION) -> list[dict]:
    return needs_of(storage, "explicit_problem", analysis_version)


def payload_of(need: dict) -> dict:
    return json.loads(need["evidence_complete_json"])


# ------------------------------------------------------------------ 显性问题簇


def test_a_problem_without_locatable_evidence_never_becomes_a_need(storage) -> None:
    """失效引用反例：证据行不在，需求行就不许多——看板上不能出现点不开原文的"需求"。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION, "志愿填报怎么改"])
    run = cluster(storage)
    clusters_before = len(explicit_of(storage))
    assert clusters_before == sum(
        1 for plan in run.outcome.plans if plan.statement_kind.value == "explicit_problem"
    )
    assert run.extraction.problems_inserted == 2

    storage.conn.execute("DELETE FROM evidence WHERE run_id = ?", (storage.run_id,))
    storage.conn.commit()
    reproved = ConservativeMerger(storage.repo, storage.run_id, matcher=MATCHER).plan(
        load_corpus(storage.conn, storage.run_id)
    )
    assert reproved.plans == ()
    assert [row["reason"] for row in reproved.skipped] == [REASON_NO_EVIDENCE] * 2
    assert len(explicit_of(storage)) == clusters_before, "复核只报告缺失，不删也不加历史行"


def test_repeated_content_merges_into_one_need_but_keeps_both_quotes(storage) -> None:
    """重复内容：两篇帖子问同一句话只算一个需求，两条证据都留着并各自归属同一簇。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION, f"{HELP_QUESTION}？"])
    run = cluster(storage)
    explicit = explicit_of(storage)
    assert len(explicit) == 1
    payload = payload_of(explicit[0])
    assert len(payload["evidence_ids"]) == len(payload["problem_ids"]) == 2
    assert payload["quotes"] == [HELP_QUESTION, f"{HELP_QUESTION}？"]
    assert run.clustering.assignments_created == 2
    assignments = rows(
        storage.conn, "SELECT * FROM cluster_assignments WHERE run_id = ?", (storage.run_id,)
    )
    assert all(row["auto_merged"] == 1 and row["similarity"] == 1.0 for row in assignments)


def test_conflicting_topic_splits_the_cluster_instead_of_merging(storage) -> None:
    """同一句问题在两个主题词下被看到：属性冲突就分裂，分裂键带着冲突属性。"""
    keyword_id = prepare(storage)
    question = "名额分配怎么申请"
    observe_titles(storage, keyword_id, [question])
    other = storage.repo.ensure_keyword("中考", "中考", seen_at=T0)
    observe_under(storage, other, ZHONGKAO, [question], tag="kw2", session_id="s-kw2")
    run = cluster(storage)
    explicit = explicit_of(storage)
    assert len(explicit) == 2
    assert [split["conflicts"] for split in run.outcome.conflict_splits] == [["topic"]]
    split = [row for row in explicit if "#conflict[" in row["canonical_text"]]
    assert len(split) == 1 and "topic=中考" in split[0]["canonical_text"]
    assert payload_of(split[0])["attributes"]["topic"] == "中考"
    assignment = rows(
        storage.conn, "SELECT * FROM cluster_assignments WHERE need_id = ?", (int(split[0]["id"]),)
    )
    assert assignment[0]["auto_merged"] == 0
    assert assignment[0]["similarity"] is None
    assert json.loads(assignment[0]["attributes_conflict_json"]) == ["topic"]


def test_negation_stays_its_own_cluster_and_missing_attributes_are_recorded(storage) -> None:
    """否定句不被吞进肯定句那一簇；缺的属性写进 `attribute_gaps`，不拿"缺失"当"任何都行"。"""
    keyword_id = prepare(storage)
    observe_titles(
        storage,
        keyword_id,
        ["上海中考复读学校哪个好？", "上海中考复读学校选哪好不踩坑？"],
    )
    cluster(storage)
    explicit = explicit_of(storage)
    assert len(explicit) == 2
    by_polarity = {
        payload_of(row)["attributes"].get("polarity"): row for row in explicit
    }
    assert set(by_polarity) == {None, "negative"}
    assert "year" in payload_of(by_polarity["negative"])["attribute_gaps"]
    assert "polarity" not in payload_of(by_polarity["negative"])["attribute_gaps"]
    assert "polarity" in payload_of(by_polarity[None])["attribute_gaps"], (
        "没判定出极性也要显式记成缺失，别让下一层把空值当兼容"
    )


def test_every_valid_need_row_carries_at_least_one_evidence_id(storage) -> None:
    """P0 退出门槛"所有展示需求有有效 evidence_id"的落库形态。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    observe_body(storage, keyword_id, HELP_QUESTION, "第二批到底怎么排？")
    run = cluster(storage)
    valid = rows(
        storage.conn,
        "SELECT * FROM needs WHERE run_id = ? AND evidence_status IN ('verified', 'inferred')"
        " ORDER BY id",
        (storage.run_id,),
    )
    assert valid, "语料里有求助句，就该有可展示的需求行"
    assert all(payload_of(row)["evidence_ids"] for row in valid)
    assert len(valid) == run.outcome.summary()["valid_needs"]
    assert all(row["analysis_version"] == ANALYSIS_VERSION for row in valid)


# ------------------------------------------------------------------ 推断需求


def test_inferred_need_is_a_conservative_template_reusing_only_known_attributes(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    inferred = needs_of(storage, "inferred_need")
    assert len(inferred) == 1
    assert inferred[0]["canonical_text"] == "获取上海中考名额分配的方法"
    assert inferred[0]["evidence_status"] == "inferred"
    payload = payload_of(inferred[0])
    assert payload["generated_expression"] is True
    explicit = explicit_of(storage)[0]
    assert payload["derived_from"] == explicit["canonical_text"]
    assert payload["evidence_ids"] == payload_of(explicit)["evidence_ids"], (
        "推断需求引用的是同一批原句证据，只有措辞是模板生成的"
    )
    assert rows(
        storage.conn, "SELECT * FROM cluster_assignments WHERE need_id = ?", (int(inferred[0]["id"]),)
    ) == [], "推断表达不抢同一个问题的第二个归属"


def test_inference_is_skipped_when_the_object_is_missing(storage) -> None:
    """抽不出对象就不生成推断需求：拿主题词填空会造出一堆假需求。"""
    assert inferred_text(ProblemAttributes(intent="method_guidance")) is None
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, "第二批到底怎么排？")
    run = cluster(storage)
    assert len(explicit_of(storage)) == 2
    skipped = [row for row in run.outcome.skipped if row["reason"] == REASON_NO_OBJECT]
    assert len(skipped) == 1 and skipped[0]["object"] is None
    inferred = needs_of(storage, "inferred_need")
    assert [row["canonical_text"] for row in inferred] == ["获取上海中考名额分配的方法"]


# ------------------------------------------------------------------ 方案构想与意图候选


def test_solution_idea_needs_both_an_artifact_and_automation_and_is_never_verified(storage) -> None:
    assert is_solution_idea("要是有自动填志愿的工具就好了")
    assert not is_solution_idea("志愿怎么填")
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, "要是有自动填志愿的工具就好了")
    cluster(storage)
    ideas = needs_of(storage, "solution_idea")
    assert [row["canonical_text"] for row in ideas] == ["要是有自动填志愿的工具就好了"]
    assert ideas[0]["evidence_status"] == "unverified_candidate"
    payload = payload_of(ideas[0])
    assert payload["counts_as_valid_need"] is False
    assert payload["suggestion_version_id"] is None and payload["content_version_id"] is not None
    assert ideas[0]["canonical_text"] not in {row["canonical_text"] for row in explicit_of(storage)}, (
        "构想句不是疑问句，不能顺手又变成一个显性问题簇"
    )


def test_a_tool_mentioned_by_an_ad_is_not_a_user_solution_idea(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考志愿填报机构，自动排志愿系统报名"])
    run = cluster(storage)
    assert needs_of(storage, "solution_idea") == []
    assert REASON_ADVERTISED_TOOL in {row["reason"] for row in run.outcome.skipped}
    assert explicit_of(storage) == []


def test_suggestion_text_becomes_an_intent_candidate_not_a_post_fact(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填", "上海中考学校排名"])
    cluster(storage)
    candidates = needs_of(storage, "search_intent_candidate")
    assert [row["canonical_text"] for row in candidates] == ["上海中考志愿怎么填"]
    payload = payload_of(candidates[0])
    assert candidates[0]["evidence_status"] == "unverified_candidate"
    assert payload["post_fact"] is False
    assert payload["evidence_ids"] == [], "联想不能冒充帖子事实：它没有证据引用"
    assert payload["suggestion_version_id"] is not None
    assert payload["keyword_id"] is not None
    assert rows(
        storage.conn,
        "SELECT ca.* FROM cluster_assignments ca JOIN needs n ON n.id = ca.need_id"
        " WHERE n.statement_kind = 'search_intent_candidate'",
    ) == []


# ------------------------------------------------------------------ 稳定 ID、幂等与撤销


def test_stable_key_is_the_merge_conclusion_not_the_post_identity(storage) -> None:
    """键只由 {语句类别, 归并签名} 决定：同签名同键，版本差异留在列上。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    first = cluster(storage)
    plans = {plan.statement_kind.value: plan for plan in first.outcome.plans}
    assert explicit_of(storage)[0]["stable_key"] == stable_key(
        plans["explicit_problem"].statement_kind, plans["explicit_problem"].signature
    )
    again = cluster(storage)
    assert again.clustering.needs_inserted == 0
    assert again.clustering.needs_reused == len(first.outcome.plans)
    assert again.clustering.assignments_created == 0
    assert again.clustering.assignments_reused == 1
    assert int(storage.conn.execute("SELECT COUNT(*) FROM needs").fetchone()[0]) == len(
        first.outcome.plans
    )
    assert int(storage.conn.execute("SELECT COUNT(*) FROM cluster_assignments").fetchone()[0]) == 1


def test_revocation_keeps_the_quote_and_remerge_reuses_the_same_row(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    need = explicit_of(storage)[0]
    payload = payload_of(need)
    problem_id = payload["problem_ids"][0]
    evidence_id = payload["evidence_ids"][0]

    assert revoke_cluster_assignment(
        storage.repo,
        storage.run_id,
        problem_id=problem_id,
        analysis_version=ANALYSIS_VERSION,
        reason="人工判定与主簇不是同一件需求",
        at=at(9),
    )
    revoked = assignment_row(storage.conn, storage.run_id, problem_id, ANALYSIS_VERSION)
    assert revoked["superseded_at"] is not None and revoked["auto_merged"] == 0
    assert assignment_row(
        storage.conn, storage.run_id, problem_id, ANALYSIS_VERSION, active_only=True
    ) is None
    assert len(rows(storage.conn, "SELECT * FROM evidence WHERE id = ?", (evidence_id,))) == 1, (
        "撤销只动关系：原句与证据永远留着"
    )
    events = storage.repo.events(storage.run_id, kind="cluster_revoked")
    assert "人工判定与主簇不是同一件需求" in events[0]["detail_json"]

    again = cluster(storage)
    assert again.clustering.assignments_revoked == 1
    current = assignment_row(storage.conn, storage.run_id, problem_id, ANALYSIS_VERSION)
    assert current["id"] == revoked["id"] and current["superseded_at"] is None
    assert int(current["need_id"]) == int(need["id"])
    assert int(storage.conn.execute("SELECT COUNT(*) FROM cluster_assignments").fetchone()[0]) == 1


def test_a_new_analysis_version_writes_its_own_rows_and_leaves_history(storage) -> None:
    """精修不改写历史：新版本自己一批需求与归属，旧行逐字不变。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    old_needs = rows(
        storage.conn, "SELECT * FROM needs WHERE analysis_version = ? ORDER BY id", (ANALYSIS_VERSION,)
    )
    old_key = explicit_of(storage)[0]["stable_key"]

    second = cluster(storage, analysis_version="p0-rule-baseline.4")
    assert rows(
        storage.conn, "SELECT * FROM needs WHERE analysis_version = ? ORDER BY id", (ANALYSIS_VERSION,)
    ) == old_needs, "旧版本的需求行一个字节都不动"
    new_explicit = explicit_of(storage, "p0-rule-baseline.4")
    assert len(new_explicit) == 1
    assert new_explicit[0]["stable_key"] == old_key, (
        "同一个结论在两个版本下键相同：版本由列区分，键不必每版重算一套"
    )
    assert second.clustering.assignments_created == len(new_explicit)
    assert int(storage.conn.execute(
        "SELECT COUNT(*) FROM cluster_assignments WHERE analysis_version = ?", ("p0-rule-baseline.4",)
    ).fetchone()[0]) == 1
    assert int(storage.conn.execute(
        "SELECT COUNT(*) FROM cluster_assignments"
    ).fetchone()[0]) == 2, "两个版本各一条归属，历史不会被后一次跑覆盖"


def test_clustering_records_one_batch_event_and_every_skip(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["上海中考数学真题资料"])
    run = cluster(storage)
    batch = storage.repo.events(storage.run_id, kind="analysis_clustering")
    assert len(batch) == 1
    detail = json.loads(batch[0]["detail_json"])
    assert detail["merge_method"] == MERGE_METHOD
    assert detail["analysis_version"] == ANALYSIS_VERSION
    assert detail["needs_inserted"] == len(run.outcome.plans)
    skips = storage.repo.events(storage.run_id, kind="cluster_skipped")
    assert len(skips) == len(run.outcome.skipped)


# ------------------------------------------------------------------ C-11：缺失属性 ≠ 任意兼容

C11_QUESTION = "名额分配怎么申请？"
C11_CANONICAL = "名额分配怎么申请"
C11_SHANGHAI = "上海，名额分配怎么申请？"
C11_BEIJING = "北京，名额分配怎么申请？"


def observe_c11(storage, titles: list[str]):
    """真实迁移库 + 真实 ingest + 真实抽取 + 真实入库，只把"该条已知地域"标回候选属性。

    当前规则抽取器对同一 canonical 必然给出同一套属性（地域就写在句子里），所以
    "同句、一方缺地域、一方已知地域"这一形状只能由受控的属性差异在**入库层**构造——
    它是 P2/P3 变体抽取器与人工修正才会出现的形态。差异经由真实的
    `record_extraction`（含"用库里的版本文本复核 quote 定位"）落库，归并层面对的
    就是普通库行，不碰任何内存捷径。
    """
    prepare(storage)
    other = storage.repo.ensure_keyword("中考", "中考", seen_at=T0)
    observe_under(storage, other, ZHONGKAO, titles, tag="c11", session_id="s-c11")
    corpus = load_corpus(storage.conn, storage.run_id)
    extracted = ProblemExtractor(
        matcher=MATCHER, normalizer=NORMALIZER, lexicon=LEXICON
    ).extract(corpus.units)
    problems = []
    for problem in extracted.problems:
        if problem.candidate.canonical_text != C11_CANONICAL:
            continue
        location = next(
            (
                known
                for known in ("上海", "北京")
                if problem.unit.text.startswith(known)
            ),
            None,
        )
        if location is not None:
            attributes = problem.candidate.attributes.model_copy(update={"location": location})
            problem = replace(
                problem, candidate=problem.candidate.model_copy(update={"attributes": attributes})
            )
        problems.append(problem)
    assert problems and all(problem.counts_as_valid for problem in problems), (
        "C-11 样本必须都是有效求助问题，否则测不到归并层"
    )
    record_extraction(
        storage.repo, storage.run_id, replace(extracted, problems=tuple(problems)), at=at(5)
    )
    merger = ConservativeMerger(storage.repo, storage.run_id, matcher=MATCHER)
    return merger, merger.plan(corpus)


def explicit_plans(outcome) -> list:
    return sorted(
        (plan for plan in outcome.plans if plan.statement_kind.value == "explicit_problem"),
        key=lambda plan: plan.signature,
    )


def plan_fingerprint(merger, rows_list) -> str:
    """把归并结果压成一个可逐字比较的字符串：置换稳定性就看它变不变。"""
    plans, splits, skipped = merger.plan_explicit_needs(list(rows_list))
    return json.dumps(
        {
            "plans": [
                {
                    "signature": plan.signature,
                    "stable_key": plan.stable_key,
                    "problem_ids": list(plan.problem_ids),
                    "attributes": plan.detail["attributes"],
                    "attribute_gaps": plan.detail["attribute_gaps"],
                    "members": plan.detail["members"],
                }
                for plan in plans
            ],
            "splits": splits,
            "skipped": skipped,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_missing_location_is_not_absorbed_by_the_known_location_cluster(storage) -> None:
    """C-11 主案：None/已知两个成员不再合成一个"上海"簇，缺口逐成员可追溯。"""
    merger, outcome = observe_c11(storage, [C11_QUESTION, C11_SHANGHAI])
    base, split = explicit_plans(outcome)
    located_id = int(
        rows(
            storage.conn,
            "SELECT id FROM problems WHERE run_id = ? AND location = ? ORDER BY id",
            (storage.run_id, "上海"),
        )[0]["id"]
    )
    unknown_id = int(
        rows(
            storage.conn,
            "SELECT id FROM problems WHERE run_id = ? AND location IS NULL ORDER BY id",
            (storage.run_id,),
        )[0]["id"]
    )

    assert base.signature == C11_CANONICAL
    assert base.problem_ids == (unknown_id,)
    assert "location" not in base.detail["attributes"], "缺地域的簇不许披上别组成员的已知地域"
    assert "location" in base.detail["attribute_gaps"]
    assert split.signature == f"{C11_CANONICAL}#conflict[location=上海]"
    assert split.problem_ids == (located_id,)
    assert split.detail["attributes"]["location"] == "上海"
    assert split.detail["conflicts"] == ["location"]
    assert split.detail["gap_attributes"] == ["location"], "已知对缺失要按缺口分裂记，而不是值冲突"
    assert base.detail["equivalence"] == "exact_attribute_profile"
    assert split.detail["equivalence"] == "non_equivalent_attribute_split"
    assert split.detail["split_from_signature"] == C11_CANONICAL
    assert [s["relation"] for s in outcome.conflict_splits] == ["non_equivalent_attribute_split"]
    assert outcome.conflict_splits[0]["attribute_gaps"] == split.detail["attribute_gaps"]
    assert "location" not in outcome.conflict_splits[0]["attribute_gaps"]
    assert "location" in base.detail["attribute_gaps"] != split.detail["attribute_gaps"]

    record_clustering(storage.repo, storage.run_id, outcome, at=at(6))
    by_canon = {str(row["canonical_text"]): row for row in explicit_of(storage)}
    assert set(by_canon) == {C11_CANONICAL, f"{C11_CANONICAL}#conflict[location=上海]"}
    payload = payload_of(by_canon[C11_CANONICAL])
    assert "location" not in payload["attributes"] and "location" in payload["attribute_gaps"]
    assert [member["problem_id"] for member in payload["members"]] == [unknown_id]
    split_payload = payload_of(by_canon[f"{C11_CANONICAL}#conflict[location=上海]"])
    assert [member["problem_id"] for member in split_payload["members"]] == [located_id]
    assignments = {
        int(row["problem_id"]): row
        for row in rows(
            storage.conn, "SELECT * FROM cluster_assignments WHERE run_id = ?", (storage.run_id,)
        )
    }
    assert assignments[unknown_id]["auto_merged"] == 1, "同轮廓合并仍是无损自动合并"
    assert assignments[located_id]["auto_merged"] == 0
    assert json.loads(assignments[located_id]["attributes_conflict_json"]) == ["location"]

    # 撤销与稳定 ID 在分裂行上照常有效：原句/证据/需求行都留着，重归并复用同一行。
    stable_before = by_canon[f"{C11_CANONICAL}#conflict[location=上海]"]["stable_key"]
    assert revoke_cluster_assignment(
        storage.repo,
        storage.run_id,
        problem_id=located_id,
        analysis_version=ANALYSIS_VERSION,
        reason="缺地域的那条不能算问的是上海",
        at=at(9),
    )
    assert assignment_row(
        storage.conn, storage.run_id, located_id, ANALYSIS_VERSION, active_only=True
    ) is None
    again = ConservativeMerger(storage.repo, storage.run_id, matcher=MATCHER).plan(
        load_corpus(storage.conn, storage.run_id)
    )
    write = record_clustering(storage.repo, storage.run_id, again, at=at(10))
    assert write.assignments_revoked == 1 and write.needs_inserted == 0
    after = {str(row["canonical_text"]): row for row in explicit_of(storage)}
    assert after[f"{C11_CANONICAL}#conflict[location=上海]"]["stable_key"] == stable_before


def test_none_known_and_conflicting_members_are_permutation_stable(storage) -> None:
    """None/已知/冲突三成员：任意输入顺序给出逐字相同的结果（C-11 验收二）。"""
    merger, outcome = observe_c11(storage, [C11_QUESTION, C11_SHANGHAI, C11_BEIJING])
    plans = explicit_plans(outcome)
    assert len(plans) == 3
    assert {plan.signature for plan in plans} == {
        C11_CANONICAL,
        f"{C11_CANONICAL}#conflict[location=上海]",
        f"{C11_CANONICAL}#conflict[location=北京]",
    }
    base = next(plan for plan in plans if plan.signature == C11_CANONICAL)
    assert "location" not in base.detail["attributes"] and "location" in base.detail["attribute_gaps"]
    by_member = {
        member["problem_id"]: member
        for plan in plans
        for member in plan.detail["members"]
    }
    locations = {
        problem_id: row["location"]
        for problem_id, row in enumerate(
            rows(
                storage.conn,
                "SELECT location FROM problems WHERE run_id = ? ORDER BY id",
                (storage.run_id,),
            ),
            start=1,
        )
    }
    for problem_id, member in by_member.items():
        assert member["attributes"].get("location") == locations[problem_id], (
            "逐成员：簇里写着的已知地域必须就是该成员自己知道的"
        )
        assert ("location" in member["attribute_gaps"]) == (locations[problem_id] is None)

    database_rows = merger.problems()
    reference = plan_fingerprint(merger, database_rows)
    for permutation in itertools.permutations(database_rows):
        assert plan_fingerprint(merger, list(permutation)) == reference, (
            "输入顺序置换不能改变任何簇的签名、成员、属性或缺口记账"
        )


def test_no_need_row_claims_an_attribute_its_members_do_not_all_share(storage) -> None:
    """真实路径回归（不手改任何属性）：每条需求行的属性恰好是全体成员共同已知的那份。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION, f"{HELP_QUESTION}？"])
    other = storage.repo.ensure_keyword("中考", "中考", seen_at=T0)
    observe_under(storage, other, ZHONGKAO, [HELP_QUESTION], tag="inv", session_id="s-inv")
    cluster(storage)
    problems = {
        int(row["id"]): row
        for row in rows(storage.conn, "SELECT * FROM problems WHERE run_id = ?", (storage.run_id,))
    }
    assert len(problems) == 3, "同句问题在两个主题词下各落一行、第三行是同帖重复观察"
    for need in explicit_of(storage):
        payload = payload_of(need)
        members = [problems[int(problem_id)] for problem_id in payload["problem_ids"]]
        assert members, "显性簇至少有一个成员"
        shared: dict[str, str] = {}
        for column, name in (("object", "object_"),) + tuple(
            (name, name) for name in ("topic", "intent", "pain_point", "location", "year", "polarity")
        ):
            values = {row[column] for row in members}
            if len(values) == 1 and next(iter(values)) is not None:
                shared[name] = next(iter(values))
        assert payload["attributes"] == shared, (
            "需求行的属性集必须等于全体成员共同已知的交集：多一个是伪装，少一个是记账错误"
        )
        assert set(payload["attribute_gaps"]) == set(ATTRIBUTE_NAMES) - set(shared)


# ------------------------------------------------------------------ C-11-r2：派生需求不得按文字碰撞丢组

C11R2_INFERRED = "获取上海中考名额分配的方法"
C11R2_METHOD = ProblemAttributes(topic="上海中考", object_="名额分配", intent="method_guidance")


def merger_method_level() -> ConservativeMerger:
    """`plan_inferred_needs` 不碰数据库：方法级回归用裸实例，输入是**人工构造**的父簇。

    这组测试钉的是派生层的分组、键与证据归属，不能冒充端到端；真实链上
    topic=上海中考 的自然碰撞由 tests/test_analysis_pipeline.py 的 e2e 回归钉住。
    """
    return object.__new__(ConservativeMerger)


def c11r2_parent(tag: str, problem_id: int, **attribute_updates) -> NeedPlan:
    attributes = C11R2_METHOD.model_copy(update=attribute_updates)
    signature = f"名额分配怎么申请#{tag}"
    return NeedPlan(
        statement_kind=StatementKind.explicit_problem,
        signature=signature,
        canonical_text=signature,
        stable_key=stable_key(StatementKind.explicit_problem, signature),
        attributes=attributes,
        evidence_ids=(problem_id,),
        problem_ids=(problem_id,),
        counts_as_valid_need=True,
    )


def inferred_fingerprint(merger, parents) -> str:
    result, skipped = merger.plan_inferred_needs(list(parents))
    return json.dumps(
        {
            "plans": [
                {
                    "signature": plan.signature,
                    "stable_key": plan.stable_key,
                    "evidence_ids": list(plan.evidence_ids),
                    "attributes": plan.detail["attributes"],
                    "attribute_gaps": plan.detail["attribute_gaps"],
                    "conflicts": plan.detail["conflicts"],
                    "gap_attributes": plan.detail["gap_attributes"],
                    "equivalence": plan.detail["equivalence"],
                    "split_from_signature": plan.detail["split_from_signature"],
                    "derived_from": plan.detail["derived_from"],
                    "derived_from_signatures": plan.detail["derived_from_signatures"],
                    "derived_from_stable_keys": plan.detail["derived_from_stable_keys"],
                }
                for plan in result
            ],
            "skipped": skipped,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_inferred_text_collision_keeps_each_attribute_profile_as_its_own_need(storage) -> None:
    """C-11-r2 直接反例（方法级人工构造）：location None/上海 两簇同文字，不再丢第二组。

    Codex 复核给出的形状：topic=上海中考、object_=名额分配、intent=method_guidance，
    location 分别 None/上海，evidence_ids 分别 (1,)/(2,)。scope_of 把已被 topic 包含的
    "上海"去掉，两句推断文字相同——相同展示文本不能当归并身份。
    """
    merger = merger_method_level()
    plans, skipped = merger.plan_inferred_needs(
        [c11r2_parent("none", 1), c11r2_parent("sh", 2, location="上海")]
    )
    assert skipped == []
    assert len(plans) == 2, "同文字不同属性轮廓的派生需求必须各自成行"
    by_signature = {plan.signature: plan for plan in plans}
    assert set(by_signature) == {C11R2_INFERRED, f"{C11R2_INFERRED}#conflict[location=上海]"}
    assert len({plan.stable_key for plan in plans}) == 2, "稳定键必须能区分派生分组"

    base = by_signature[C11R2_INFERRED]
    assert base.evidence_ids == (1,)
    assert "location" not in base.detail["attributes"], "缺地域的派生行不得披上别组的地域"
    assert "location" in base.detail["attribute_gaps"]
    assert base.detail["conflicts"] == []
    assert base.detail["equivalence"] == "exact_attribute_profile"
    assert base.detail["split_from_signature"] is None
    assert base.detail["derived_from_signatures"] == ["名额分配怎么申请#none"]

    split = by_signature[f"{C11R2_INFERRED}#conflict[location=上海]"]
    assert split.evidence_ids == (2,), "第二组证据不能被 seen(signature) 静默略过"
    assert split.detail["attributes"]["location"] == "上海"
    assert split.detail["conflicts"] == ["location"]
    assert split.detail["gap_attributes"] == ["location"]
    assert split.detail["equivalence"] == "non_equivalent_attribute_split"
    assert split.detail["split_from_signature"] == C11R2_INFERRED
    assert split.detail["derived_from"] == "名额分配怎么申请#sh", "派生关系逐组指向自己的父簇"
    assert split.detail["template"] == "获取{scope}{object}的方法"
    assert split.detail["generated_expression"] is True
    # 模板事实不被改写：基线文字仍是那句话，分裂只在签名后缀上区分。
    assert base.canonical_text == C11R2_INFERRED


def test_inferred_groups_are_permutation_stable_and_union_same_profile_evidence(storage) -> None:
    """输入顺序置换逐字稳定；同轮廓的两个父簇并成一组时证据取并集、父引用全列。"""
    merger = merger_method_level()
    parents = [
        c11r2_parent("none", 1),
        c11r2_parent("sh-a", 2, location="上海"),
        c11r2_parent("sh-b", 3, location="上海"),
    ]
    reference = inferred_fingerprint(merger, parents)
    for permutation in itertools.permutations(parents):
        assert inferred_fingerprint(merger, list(permutation)) == reference, (
            "置换不能改变派生分组的签名、键、证据或归属记账"
        )

    plans, _ = merger.plan_inferred_needs(parents)
    assert len(plans) == 2
    split = next(plan for plan in plans if "#conflict[" in plan.signature)
    assert split.evidence_ids == (2, 3), "同轮廓合并是无损合并：两组证据都不许丢"
    assert split.detail["derived_from_signatures"] == [
        "名额分配怎么申请#sh-a",
        "名额分配怎么申请#sh-b",
    ]
    assert len(split.detail["derived_from_stable_keys"]) == 2


def test_inferred_collision_covers_pain_point_polarity_and_year_gaps(storage) -> None:
    """year/polarity/pain_point 不进（或不全进）模板时的同类碰撞：known-known 冲突与缺失都分裂。"""
    merger = merger_method_level()
    # pain_point：已知对已知、已知对缺失，文字都相同（模板不含 pain_point）。
    plans, _ = merger.plan_inferred_needs(
        [
            c11r2_parent("pain-a", 1, pain_point="太麻烦"),
            c11r2_parent("pain-b", 2, pain_point="怕踩坑"),
            c11r2_parent("pain-none", 3),
        ]
    )
    by_signature = {plan.signature: plan for plan in plans}
    assert len(plans) == 3
    assert by_signature[C11R2_INFERRED].detail["attributes"]["pain_point"] == "太麻烦"
    known = by_signature[f"{C11R2_INFERRED}#conflict[pain_point=怕踩坑]"]
    assert known.evidence_ids == (2,)
    assert known.detail["conflicts"] == ["pain_point"]
    assert known.detail["gap_attributes"] == [], "双方都已知而不同是值冲突，不是缺口"
    missing = by_signature[f"{C11R2_INFERRED}#conflict[pain_point=None]"]
    assert missing.evidence_ids == (3,)
    assert missing.detail["gap_attributes"] == ["pain_point"]
    assert "pain_point" not in missing.detail["attributes"]
    assert "pain_point" in missing.detail["attribute_gaps"]

    # polarity：模板不含极性，肯定/否定同文字也必须两行，否定行不被肯定行吞掉。
    plans, _ = merger.plan_inferred_needs(
        [c11r2_parent("pol-none", 1), c11r2_parent("pol-neg", 2, polarity="negative")]
    )
    assert {plan.signature for plan in plans} == {C11R2_INFERRED, f"{C11R2_INFERRED}#conflict[polarity=negative]"}
    neg = next(plan for plan in plans if "#conflict[" in plan.signature)
    assert neg.evidence_ids == (2,)
    assert neg.detail["gap_attributes"] == ["polarity"]

    # year：2026 已被 topic"2026上海中考"包含，scope_of 去重后同文字，年份缺失同样要分裂。
    dated = C11R2_METHOD.model_copy(update={"topic": "2026上海中考"})
    def parent(tag: str, problem_id: int, **updates) -> NeedPlan:
        signature = f"名额分配怎么申请#{tag}"
        return NeedPlan(
            statement_kind=StatementKind.explicit_problem,
            signature=signature,
            canonical_text=signature,
            stable_key=stable_key(StatementKind.explicit_problem, signature),
            attributes=dated.model_copy(update=updates),
            evidence_ids=(problem_id,),
            problem_ids=(problem_id,),
            counts_as_valid_need=True,
        )

    plans, _ = merger.plan_inferred_needs(
        [parent("year-none", 1), parent("year-2026", 2, year="2026")]
    )
    dated_text = "获取2026上海中考名额分配的方法"
    assert {plan.signature for plan in plans} == {dated_text, f"{dated_text}#conflict[year=2026]"}
    bare = next(plan for plan in plans if "#conflict[" not in plan.signature)
    assert bare.evidence_ids == (1,)
    assert "year" in bare.detail["attribute_gaps"], "缺年份的派生行不能借别组的 2026 装作已知"
    dated_split = next(plan for plan in plans if "#conflict[" in plan.signature)
    assert dated_split.evidence_ids == (2,)
    assert dated_split.detail["attributes"]["year"] == "2026"
