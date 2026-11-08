"""P0-D 分析编排：一次调用跑完整条离线分析链，并且只写新版本行。

流水线层钉的是**衔接**，各层算法在 P0-22～29 的专项里已经分别钉过：

- 顺序与落库：短语发现 → 抽取 → 归并 → 评分 → 双图 → 导出，后一层读的是前一层写进库的行；
- 一批一个时间戳，五类产物齐、反读校验过；
- 重放不写第二份业务行；而库里的事件确实变多了，所以同一分析版本的导出必须显式换 `snapshot`；
- 换 `analysis_version` 各写一批，P0-C 的 `p0-preliminary.1` 与 `branch_stats` 历史奖励一字不动；
- 分析不花平台预算：`budget_ledger` 与 `budget_reservations` 一行都不许多；
- 重跑按原文重落证据，被手改过的旧引用只留 `orphan_evidence` 审计，不会继续挂在需求上；
- 空语料照样出货，并在库里留下"语料不足"的记录而不是"发现零个短语"的假象；
- nlp 阈值、评分权重与分位门槛来自配置快照，不是模块默认值。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.export import EXPORT_FILENAMES, ExportConflict, parse_csv
from xhs_discovery.analysis.lexicon import DomainLexicon
from xhs_discovery.analysis.pipeline import AnalysisRules, run_analysis
from xhs_discovery.config import DemandWeights, load_settings
from xhs_discovery.discovery.extract import EXTRACTOR_VERSION as ONLINE_EXTRACTOR_VERSION
from xhs_discovery.discovery.merge import PRELIMINARY_ANALYSIS_VERSION

from tests.test_analysis_clustering import (
    C11_CANONICAL,
    C11_QUESTION,
    C11_SHANGHAI,
    HELP_QUESTION,
    explicit_of,
    needs_of,
    observe_c11,
    payload_of,
)
from tests.test_analysis_extraction import (
    LEXICON,
    NORMALIZER,
    at,
    observe_body,
    observe_suggestions,
    observe_titles,
    prepare,
    rows,
)
from tests.test_analysis_export import file_of
from tests.test_engine_worker import Harness, engine_settings

SECOND_VERSION = "p0-rule-baseline.4"
ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(ROOT / "config" / "offline-demo.yaml")
BODY = "名额分配到底要什么条件？"
SUGGESTION = "上海中考录取分数线"
BATCH_EVENTS = (
    "phrase_mining",
    "analysis_extraction",
    "analysis_clustering",
    "analysis_scoring",
    "analysis_export",
)


def collect(storage, keyword_id: int) -> None:
    """标题 + 正文 + 联想各一条：语料三种来源都到场，编排的口径才测得全。"""
    observe_titles(storage, keyword_id, [HELP_QUESTION, "志愿填报怎么改"])
    observe_body(storage, keyword_id, HELP_QUESTION, BODY)
    observe_suggestions(storage, keyword_id, [SUGGESTION])


def rules(**overrides) -> AnalysisRules:
    payload = dict(settings=SETTINGS, lexicon=LEXICON, normalizer=NORMALIZER)
    payload.update(overrides)
    return AnalysisRules(**payload)


def run(storage, **overrides):
    kwargs = dict(at=at(30), rules=rules())
    kwargs.update(overrides)
    settings = kwargs.pop("settings", SETTINGS)
    return run_analysis(storage.conn, storage.paths, storage.run_id, settings, **kwargs)


def counts(storage, analysis_version: str = ANALYSIS_VERSION) -> dict[str, int]:
    """分析层写下的业务行数：重放与换版本的对照面。

    只数按 `analysis_version` 分区的表——`problems` 挂的是 `extractor_version`，同一个抽取器
    版本下换分析版本不会新增问题行，把它放进来会把"该相等"和"本来就复用"混成一谈。
    """
    return {
        table: len(
            rows(
                storage.conn,
                f"SELECT id FROM {table} WHERE run_id = ? AND analysis_version = ?",
                (storage.run_id, analysis_version),
            )
        )
        for table in ("phrases", "evidence", "needs", "cluster_assignments")
    }


# ------------------------------------------------------------------ 一次跑通


def test_one_call_runs_every_layer_and_lands_five_artifacts(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    result = run(storage)

    assert [item.filename for item in result.export.files] == list(EXPORT_FILENAMES)
    assert result.export.verification is not None and result.export.verification.ok
    assert result.phrases[0] >= 1, "语料里真有可统计的文本时，短语发现不该空手而归"
    assert result.extraction_write.problems_inserted >= 1
    assert counts(storage)["needs"] >= 1
    assert result.scoring_write.scores_updated >= 1
    assert result.scoring.scored, "库里明明有需求却没算分，说明编排断在评分之前"
    assert result.demand_graph.by_kind("need")
    assert json.dumps(result.summary(), ensure_ascii=False)


def test_suggestions_reach_the_graph_but_never_masquerade_as_post_facts(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    run(storage)

    fields = {
        str(row["source_field"])
        for row in rows(storage.conn, "SELECT source_field FROM evidence WHERE run_id = ?", (storage.run_id,))
    }
    assert fields == {"title", "body"}, "联想文本不产生帖子证据行"
    result = run(storage, snapshot="sugg")
    header, body = parse_csv(file_of(storage, result.export, "needs.csv"))
    assert "search_intent_candidate" not in {row[header.index("statement_kind")] for row in body}


def test_the_whole_batch_carries_one_timestamp(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    run(storage)

    marks = rows(
        storage.conn,
        "SELECT DISTINCT at FROM run_events WHERE run_id = ? AND kind IN (%s)"
        % ",".join("?" for _ in BATCH_EVENTS),
        (storage.run_id, *BATCH_EVENTS),
    )
    assert len(marks) == 1, "一次分析＝一个批次时刻：跨分钟的批次没法解释成同一条结论"


# ------------------------------------------------------------------ 不改写历史


def test_replay_writes_no_second_business_row_but_needs_an_explicit_snapshot(storage) -> None:
    """重放的两种后果要分开：库里业务行不许多，导出内容却因新事件而变了。"""
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    first = run(storage)
    before = counts(storage)

    with pytest.raises(ExportConflict, match="snapshot"):
        run(storage)

    assert counts(storage) == before
    second = run(storage, snapshot="replay")
    assert second.phrases == (0, first.phrases[0])
    assert second.extraction_write.problems_inserted == 0
    assert second.extraction_write.evidence_reused == first.extraction_write.evidence_inserted
    assert second.clustering_write.needs_inserted == 0
    assert second.clustering_write.needs_reused == before["needs"]
    assert second.scoring_write.scores_updated == first.scoring_write.scores_updated
    assert second.export.export_dir != first.export.export_dir
    assert counts(storage) == before


def test_a_second_analysis_version_writes_its_own_batch(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    first = run(storage)
    baseline = counts(storage)

    second = run(storage, analysis_version=SECOND_VERSION, at=at(31))

    assert second.analysis_version == SECOND_VERSION
    assert counts(storage, SECOND_VERSION) == baseline
    assert counts(storage) == baseline, "新版本不得动旧版本的行"
    assert first.export.export_dir != second.export.export_dir
    kinds = {
        str(row["statement_kind"])
        for row in rows(
            storage.conn,
            "SELECT DISTINCT statement_kind FROM needs WHERE run_id = ? AND analysis_version = ?",
            (storage.run_id, SECOND_VERSION),
        )
    }
    assert "explicit_problem" in kinds


def test_historical_preliminary_rows_and_branch_rewards_stay_put(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    rewards = rows(
        storage.conn, "SELECT id, trials, total_reward, stale_streak FROM branch_stats WHERE run_id = ?",
        (storage.run_id,),
    )
    before = rows(storage.conn, "SELECT * FROM needs ORDER BY id")

    run(storage)

    assert rows(
        storage.conn,
        "SELECT id, trials, total_reward, stale_streak FROM branch_stats WHERE run_id = ?",
        (storage.run_id,),
    ) == rewards, "分析层碰 branch_stats 就等于改写历史奖励"
    assert rows(storage.conn, "SELECT * FROM needs ORDER BY id")[: len(before)] == before
    assert rows(
        storage.conn, "SELECT id FROM needs WHERE analysis_version = ?", (PRELIMINARY_ANALYSIS_VERSION,)
    ) == []
    for table in ("evidence", "problems"):
        assert rows(
            storage.conn,
            f"SELECT id FROM {table} WHERE extractor_version = ?",
            (ONLINE_EXTRACTOR_VERSION,),
        ) == [], "离线行不得挂在线抽取器的版本，否则两张图分不清哪层写的"


def test_analysis_spends_no_platform_budget(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    run(storage)

    assert rows(storage.conn, "SELECT id FROM budget_ledger WHERE run_id = ?", (storage.run_id,)) == []
    assert (
        rows(storage.conn, "SELECT id FROM budget_reservations WHERE run_id = ?", (storage.run_id,)) == []
    ), "分析是本地计算，记进预算账本会把跑一次分析混成花掉一格额度"


@pytest.mark.parametrize(
    "overrides",
    [{"analysis_version": PRELIMINARY_ANALYSIS_VERSION}, {"extractor_version": ONLINE_EXTRACTOR_VERSION}],
    ids=["analysis_version", "extractor_version"],
)
def test_the_online_p0_c_versions_are_refused(storage, overrides) -> None:
    prepare(storage)

    with pytest.raises(ValueError, match="在线"):
        run(storage, **overrides)

    marks = ",".join("?" for _ in BATCH_EVENTS)
    assert (
        rows(
            storage.conn,
            f"SELECT id FROM run_events WHERE run_id = ? AND kind IN ({marks})",
            (storage.run_id, *BATCH_EVENTS),
        )
        == []
    ), "守卫要在任何一层动手之前拦住，否则库里已经留下一批挂错版本的结论"
    assert rows(storage.conn, "SELECT id FROM needs WHERE run_id = ?", (storage.run_id,)) == []


def test_a_run_that_is_not_in_the_database_is_refused(storage) -> None:
    with pytest.raises(ValueError, match="run 不存在"):
        run_analysis(storage.conn, storage.paths, "no-such-run", SETTINGS, rules=rules(), at=at(30))


# ------------------------------------------------------------------ 空语料与失效引用


def test_an_empty_run_still_exports_and_says_why_there_is_nothing(storage) -> None:
    result = run(storage)

    assert result.corpus.units == ()
    assert result.phrase_set.corpus_below_min_docs is True
    assert result.phrases == (0, 0)
    assert result.demand_graph.nodes == ()
    for filename in ("keywords.csv", "needs.csv", "evidence.csv"):
        _header, body = parse_csv(file_of(storage, result.export, filename))
        assert body == [], f"{filename} 该只有表头"
    assert result.export.verification.ok
    report = json.loads(file_of(storage, result.export, "report.json"))
    assert report["statistics"]["analysis"]["needs_total"] == 0
    assert report["analysis"]["exclusions"]["no_score"] == 0


def test_a_hand_edited_quote_is_quarantined_not_silently_repaired(storage) -> None:
    """分析之后有人手改了库里的引用：重跑按原文重新落一条，错的那行必须留下审计而不是消失。"""
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    run(storage)
    payload = json.loads(
        rows(
            storage.conn,
            "SELECT evidence_complete_json FROM needs WHERE run_id = ? AND analysis_version = ?"
            " AND statement_kind = 'explicit_problem' ORDER BY id LIMIT 1",
            (storage.run_id, ANALYSIS_VERSION),
        )[0]["evidence_complete_json"]
    )
    stale_id = int(payload["evidence_ids"][0])
    with storage.repo.write():
        storage.conn.execute(
            "UPDATE evidence SET quote = ? WHERE id = ?", ("这句被改过的原句不在容器里", stale_id)
        )

    result = run(storage, snapshot="tampered")

    assert "orphan_evidence" in result.integrity.codes()
    assert result.integrity.checked["evidence_not_on_a_need"] == 1
    referenced = {
        int(value)
        for row in rows(
            storage.conn,
            "SELECT evidence_complete_json AS p FROM needs WHERE run_id = ? AND analysis_version = ?",
            (storage.run_id, ANALYSIS_VERSION),
        )
        for value in json.loads(row["p"])["evidence_ids"]
    }
    assert stale_id not in referenced, "坏引用只留审计，不许还被需求挂着"
    _header, body = parse_csv(file_of(storage, result.export, "needs.csv"))
    assert body, "真正可定位的证据重落成功后，需求该回到排行榜"
    assert result.export.verification.ok


# ------------------------------------------------------------------ 参数来自配置


def test_scoring_and_nlp_parameters_come_from_the_settings_snapshot(storage) -> None:
    keyword_id = prepare(storage)
    collect(storage, keyword_id)
    even = SETTINGS.model_copy(
        update={
            "scoring": SETTINGS.scoring.model_copy(
                update={
                    "weights": DemandWeights(s=0.25, q=0.25, a=0.25, f=0.25),
                    "quantile_min_sample": 1,
                }
            ),
            "nlp": SETTINGS.nlp.model_copy(update={"char_ngram_min": 5, "char_ngram_max": 6}),
        }
    )

    result = run(storage, settings=even, rules=rules(settings=even), at=at(32))

    assert result.scoring.weights == {"S": 0.25, "Q": 0.25, "A": 0.25, "F": 0.25}
    assert result.scoring.provisional is False, "分位门槛提到配置里，跑法就不能照旧默认 30"
    miner = rules(settings=even).miner(extractor_version=EXTRACTOR_VERSION)
    assert (miner.min_n, miner.max_n) == (5, 6)
    assert (miner.min_phrase_freq, miner.min_pmi) == (
        even.nlp.min_phrase_freq,
        even.nlp.min_pmi,
    ), "阈值必须来自配置快照，不是 PhraseMiner 的默认值"
    grams = rows(
        storage.conn,
        "SELECT text FROM phrases WHERE run_id = ? AND kind = 'ngram' AND analysis_version = ?",
        (storage.run_id, ANALYSIS_VERSION),
    )
    assert all(len(row["text"]) >= 5 for row in grams), "n-gram 窗口没传给发现器"
    need = rows(
        storage.conn,
        "SELECT score_json FROM needs WHERE run_id = ? AND score IS NOT NULL",
        (storage.run_id,),
    )[0]
    assert json.loads(need["score_json"])["weights"] == {"S": 0.25, "Q": 0.25, "A": 0.25, "F": 0.25}


def test_rules_default_to_the_paths_declared_in_settings() -> None:
    parts = AnalysisRules(SETTINGS)

    assert parts.lexicon.is_term("上海中考"), "配置声明的词典必须真的进到分词器"
    assert not parts.lexicon.is_term("不存在的词")
    assert parts.matcher.has_help_marker(HELP_QUESTION), "意图规则要真的从配置路径加载"
    assert parts.normalizer.locations == (), "默认词表为空：数据集专属口径得由调用方显式传"


# ------------------------------------------------------------------ 跨层闭环


@pytest.fixture
def collected(tmp_path: Path):
    """P0-C 引擎 + fixture 数据源跑出来的真库：不是测试 helper 手工拼的观察。"""
    harness = Harness(tmp_path / "e2e", engine_settings(), "run-e2e")
    yield harness
    harness.close()


def test_a_real_fixture_run_analyzes_into_displayable_needs(collected) -> None:
    harness = collected
    run_report = harness.guarded_run(max_slots=8)
    assert run_report.slots

    charges = len(harness.rows("SELECT id FROM budget_ledger"))
    result = run_analysis(
        harness.conn,
        harness.paths,
        harness.run_id,
        harness.settings,
        rules=AnalysisRules(
            harness.settings,
            lexicon=DomainLexicon(harness.engine.domain_terms),
            normalizer=harness.engine.normalizer,
        ),
        at=at(40),
    )

    _header, body = parse_csv(file_of(harness, result.export, "needs.csv"))
    assert body, "采集→分析→导出接上了却一条需求都没有，离线闭环就是断的"
    assert result.export.verification.ok
    assert result.demand_graph.by_kind("evidence")
    assert len(harness.rows("SELECT id FROM budget_ledger")) == charges, "分析不得新增一笔账"
    assert result.extraction_write.evidence_inserted >= 1
    assert all(int(row["synthetic"]) == 1 for row in harness.rows("SELECT synthetic FROM evidence"))


# ------------------------------------------------------------------ C-11 传播：缺失不被伪装成已知


def test_a_missing_attribute_is_never_disguised_downstream_of_the_pipeline(storage) -> None:
    """C-11 修复要一路传到评分、needs.csv 与需求图：分裂行各自成行，缺口不披上别组的已知值。"""
    observe_c11(storage, [C11_QUESTION, C11_SHANGHAI])
    result = run(storage)

    header, body = parse_csv(file_of(storage, result.export, "needs.csv"))
    canon = header.index("canonical_text")
    kind = header.index("statement_kind")
    key = header.index("stable_key")
    derived = header.index("derived_from_stable_key")
    explicit = [row for row in body if row[kind] == "explicit_problem"]
    canons = {row[canon] for row in explicit}
    assert C11_CANONICAL in canons, "缺地域的那句该留在自己的簇里"
    assert f"{C11_CANONICAL}#conflict[location=上海]" in canons, "已知地域必须自成一簇"
    base = next(row for row in explicit if row[canon] == C11_CANONICAL)
    assert "上海" not in base[canon], "缺地域的簇不能借用别组成员的地域"

    parent_text = {row[key]: row[canon] for row in explicit}
    inferred = [row for row in body if row[kind] == "inferred_need"]
    assert inferred
    for row in inferred:
        parent = parent_text.get(row[derived])
        known = parent is not None and "上海" in parent
        assert ("上海" in row[canon]) == known, (
            "推断模板里的地域只能来自它自己父簇已知的值：缺地域的父簇不得被填上上海"
        )

    assert result.scoring_write.scores_updated >= 2, "两条需求各算各的分，不该被并成一簇只算一次"
    need_labels = {node.label for node in result.demand_graph.by_kind("need")}
    assert f"{C11_CANONICAL}#conflict[location=上海]" in need_labels
    conflict_edges = [
        edge
        for edge in result.demand_graph.edges
        if edge.relation == "merged_into" and edge.attributes.get("conflicts") == ["location"]
    ]
    assert conflict_edges, "成员到需求图的归属边必须显式带上 location 冲突，而不是悄悄并入"
    assert result.integrity.ok


# ------------------------------------------------------------------ C-11-r2：派生需求按文字碰撞的真实端到端


C11R2_INFERRED = "获取上海中考名额分配的方法"
C11R2_BARE_TITLE = "名额分配怎么申请？"
C11R2_LOCAL_TITLE = "上海名额分配怎么申请？"


def test_inferred_text_collision_survives_the_real_pipeline_without_losing_a_group(storage) -> None:
    """C-11-r2 真实端到端（**没有任何人工属性替换**）：同文字不同轮廓的派生行各自走完全链。

    两个标题在真实主题词"上海中考"下经真实迁移、ingest、规则抽取与 `record_extraction`
    落库：裸句自然给出 location=None，带"上海"前缀那句被抽取器自然识别出 location=上海。
    `scope_of` 会把已被 topic 包含的"上海"去掉，两条推断模板句因此完全相同——正是
    Codex 复核的反例形状。本用例与 observe_c11 那组"入库层受控改属性"的方法级/属性替换
    测试相区分：这里全部是未替换的真实链路，验证派生层不再按文字去重丢组，
    且 needs.csv、评分、需求图与 report.json 都传播到位。
    """
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [C11R2_BARE_TITLE, C11R2_LOCAL_TITLE])
    result = run(storage)

    problem_rows = rows(
        storage.conn,
        "SELECT id, canonical_text, location FROM problems WHERE run_id = ? ORDER BY id",
        (storage.run_id,),
    )
    assert [(row["canonical_text"], row["location"]) for row in problem_rows] == [
        ("名额分配怎么申请", None),
        ("上海名额分配怎么申请", "上海"),
    ], "真实抽取就该自然给出缺失/已知两种轮廓"

    explicit = explicit_of(storage)
    inferred = needs_of(storage, "inferred_need")
    assert len(explicit) == 2 and len(inferred) == 2, "派生行不许被同文字的 seen 略过"
    inferred_payload = {str(row["canonical_text"]): payload_of(row) for row in inferred}
    conflict_text = f"{C11R2_INFERRED}#conflict[location=上海]"
    assert set(inferred_payload) == {C11R2_INFERRED, conflict_text}
    assert len({row["stable_key"] for row in inferred}) == 2, "稳定键必须区分派生分组"

    by_stable_key = {str(row["stable_key"]): row for row in explicit}
    bare = inferred_payload[C11R2_INFERRED]
    local = inferred_payload[conflict_text]
    assert "location" not in bare["attributes"], "缺地域的派生行不得披上别组的地域"
    assert "location" in bare["attribute_gaps"]
    assert local["attributes"]["location"] == "上海"
    # 基线=最小成员问题 id 的轮廓，即缺地域那组；裸文字行指向的父簇必须真是缺地域的那条。
    assert "location" not in payload_of(by_stable_key[bare["derived_from_stable_key"]])["attributes"]
    assert (
        payload_of(by_stable_key[local["derived_from_stable_key"]])["attributes"]["location"] == "上海"
    )
    assert set(bare["evidence_ids"]) & set(local["evidence_ids"]) == set(), "两组证据不得混挂"
    assert len(bare["evidence_ids"]) == len(local["evidence_ids"]) == 1
    assert bare["evidence_ids"] == payload_of(
        by_stable_key[bare["derived_from_stable_key"]]
    )["evidence_ids"]
    assert local["evidence_ids"] == payload_of(
        by_stable_key[local["derived_from_stable_key"]]
    )["evidence_ids"]

    header, body = parse_csv(file_of(storage, result.export, "needs.csv"))
    canon = header.index("canonical_text")
    kind = header.index("statement_kind")
    key = header.index("stable_key")
    derived = header.index("derived_from_stable_key")
    evidence_col = header.index("evidence_ids")
    exported = [row for row in body if row[kind] == "inferred_need"]
    assert {row[canon] for row in exported} == {C11R2_INFERRED, conflict_text}
    assert len({row[key] for row in exported}) == 2
    assert all(row[derived] for row in exported), "needs.csv 每一派生行都带着自己的父簇引用"
    assert {row[derived] for row in exported} == {
        bare["derived_from_stable_key"],
        local["derived_from_stable_key"],
    }
    assert len({row[evidence_col] for row in exported}) == 2, "needs.csv 上两组证据也各不相同"

    unscored = rows(
        storage.conn,
        "SELECT id FROM needs WHERE run_id = ? AND statement_kind = 'inferred_need'"
        " AND score IS NULL",
        (storage.run_id,),
    )
    assert unscored == [], "两条派生行各算各的分，不该有行没被评分链路接到"

    graph = result.demand_graph
    bare_id = int(next(row["id"] for row in inferred if row["canonical_text"] == C11R2_INFERRED))
    local_id = int(next(row["id"] for row in inferred if row["canonical_text"] == conflict_text))
    bare_parent_id = int(by_stable_key[bare["derived_from_stable_key"]]["id"])
    local_parent_id = int(by_stable_key[local["derived_from_stable_key"]]["id"])
    assert {(edge.source, edge.target) for edge in graph.edges if edge.relation == "derived_from"} == {
        (f"need:{bare_id}", f"need:{bare_parent_id}"),
        (f"need:{local_id}", f"need:{local_parent_id}"),
    }, "图上每条派生行只指自己的父簇"
    assert {edge.target for edge in graph.outgoing(f"need:{bare_id}", "supported_by")} == {
        f"ev:{bare['evidence_ids'][0]}"
    }
    assert {edge.target for edge in graph.outgoing(f"need:{local_id}", "supported_by")} == {
        f"ev:{local['evidence_ids'][0]}"
    }

    report = json.loads(file_of(storage, result.export, "report.json"))
    assert report["statistics"]["analysis"]["needs_by_statement_kind"]["inferred_need"] == 2, (
        "report.json 的派生需求计数必须看见两组，而不是被文字去重成一组"
    )
    assert result.export.verification is not None and result.export.verification.ok
    assert result.integrity.ok

