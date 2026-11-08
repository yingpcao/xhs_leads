"""P0-29：五类产物导出与反读校验。

导出的价值在于"别人拿这五个文件能复核我们的结论"，所以这里钉的全是交付面：

- 五个文件齐全、路径在 run 目录内、`artifacts` 登记了 kind 与哈希，report.json 最后落盘
  以便嵌进前四个的摘要；
- `needs.csv` 的展示口径：0 分与 NULL 分分列，构想与联想候选不进排行榜，
  失效引用（自检 violation）涉及的行不展示；
- CSV 安全：以 `= + - @ 制表符 回车` 开头的文本前置撇号、NUL 换成替换符，反读逐格相等；
- GraphML：节点 id 是 `<前缀>_<序号>` 且精确身份另存 `ref`，同名属性只声明一种类型，
  复合值 JSON 化成 string 并在报告里点名；
- report.json：版本/配置/预算/统计/异常/局限各有其位，配置快照不完整时预算上限留空而不是猜；
- 反读校验能区分"文件被改过"、"库里又写了新事实"、"转义吃掉了数据"；
- 不改写历史：同一分析版本内容变了就报错，要新内容显式换 `snapshot`；
  换 `analysis_version` 各写各的目录，P0-C 的 `p0-preliminary.1` 一个字节都不动。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.export import (
    COLUMNS_BY_FILENAME,
    EVIDENCE_COLUMNS,
    EXPORT_FILES,
    KEYWORD_COLUMNS,
    NEED_COLUMNS,
    ExportConflict,
    csv_cell,
    export_directory,
    export_run,
    parse_csv,
    render_graphml,
    verify_export,
    xml_text,
)
from xhs_discovery.analysis.graph import NODE_PREFIX, Graph, GraphEdge, GraphNode

from tests.conftest import T0
from tests.test_analysis_clustering import HELP_QUESTION, cluster, explicit_of
from tests.test_analysis_extraction import (
    at,
    observe_body,
    observe_suggestions,
    observe_titles,
    prepare,
    rows,
)
from tests.test_analysis_scoring import Card, observe_cards, score

SECOND_VERSION = "p0-rule-baseline.4"
ROOT = Path(__file__).resolve().parents[1]


def export(storage, **overrides):
    """跑一次完整导出（含反读校验），返回 `ExportResult`。"""
    kwargs = dict(analysis_version=ANALYSIS_VERSION, extractor_version=EXTRACTOR_VERSION, at=at(20))
    kwargs.update(overrides)
    return export_run(storage.conn, storage.paths, storage.run_id, **kwargs)


def analyze(storage, *, analysis_version=ANALYSIS_VERSION) -> dict:
    """采一条最小链路并跑完分析：分析层之后的导出才有东西可导。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, "名额分配到底要什么条件？")
    cluster(storage, analysis_version=analysis_version)
    score(storage, analysis_version=analysis_version)
    return {"keyword_id": keyword_id}


def artifact_rows(storage) -> dict[str, dict]:
    return {
        str(row["kind"]): dict(row)
        for row in rows(storage.conn, "SELECT * FROM artifacts WHERE run_id = ?", (storage.run_id,))
    }


def read(storage, rel_path: str) -> str:
    return (storage.paths.root / rel_path).read_text(encoding="utf-8")


@pytest.fixture
def budgeted(tmp_path: Path):
    """一个带完整配置快照与真实预算流水的 run：导出只有在这种库上才给得出预算上限。"""
    from xhs_discovery.config import load_settings
    from xhs_discovery.paths import RuntimePaths
    from xhs_discovery.storage import db as storage_db
    from xhs_discovery.storage.ledger import Ledger
    from xhs_discovery.storage.repository import Repository

    from tests.conftest import StorageRuntime, build_run_identity

    paths = RuntimePaths(tmp_path / "budget")
    paths.runs_dir.mkdir(parents=True, exist_ok=True)
    conn = storage_db.connect(paths.db_path)
    repo = Repository(conn)
    settings = load_settings(ROOT / "config" / "offline-demo.yaml")
    identity = build_run_identity("run-budget")
    repo.create_run(identity, config_json=settings.canonical_json())
    runtime = StorageRuntime(paths=paths, conn=conn, repo=repo, run_id=identity.run_id)
    analyze(runtime)
    ledger = Ledger.from_settings(conn, identity.run_id, settings)
    ledger.record_quota("max_posts_stored", 3, reason="入库 3 篇")
    try:
        yield SimpleNamespace(paths=paths, conn=conn, repo=repo, run_id=identity.run_id,
                              settings=settings)
    finally:
        conn.close()


def file_of(storage, result, filename: str) -> str:
    entry = next(item for item in result.files if item.filename == filename)
    return read(storage, entry.rel_path)


# ------------------------------------------------------------------ 五类产物

def test_the_five_artifacts_land_and_are_registered(storage) -> None:
    analyze(storage)
    result = export(storage)
    assert [item.filename for item in result.files] == [name for name, _ in EXPORT_FILES]
    registered = artifact_rows(storage)
    assert set(registered) == {kind for _, kind in EXPORT_FILES}
    directory = export_directory(storage.paths, storage.run_id, ANALYSIS_VERSION)
    assert directory.is_dir()
    for item in result.files:
        target = storage.paths.resolve_within_run(item.rel_path, storage.run_id)
        assert target.is_file(), f"{item.filename} 没落盘"
        assert str(registered[item.artifact_kind]["rel_path"]) == item.rel_path
        assert str(registered[item.artifact_kind]["sha256"]) == item.sha256
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item.sha256
    assert storage.repo.events(storage.run_id, kind="analysis_export"), "导出本身也要可审计"


def test_report_is_rendered_last_and_embeds_the_other_four_digests(storage) -> None:
    analyze(storage)
    result = export(storage)
    report = json.loads(file_of(storage, result, "report.json"))
    listed = {item["filename"]: item for item in report["exported"]["files"]}
    by_name = {item.filename: item for item in result.files}
    for filename in ("keywords.csv", "needs.csv", "evidence.csv", "graph.graphml"):
        assert listed[filename]["sha256"] == by_name[filename].sha256
    assert "report.json" not in listed, "报告不能自指哈希"


def test_csv_headers_match_the_documented_columns_and_rows_are_deterministic(storage) -> None:
    analyze(storage)
    result = export(storage)
    for filename in COLUMNS_BY_FILENAME:
        header, _body = parse_csv(file_of(storage, result, filename))
        assert header == list(COLUMNS_BY_FILENAME[filename])
    first = file_of(storage, export(storage, snapshot="a"), "needs.csv")
    second = file_of(storage, export(storage, snapshot="b"), "needs.csv")
    assert first == second, "同一批库事实必须逐字节导出同一份"


# ------------------------------------------------------------------ 展示口径

def test_needs_csv_only_shows_scored_rows_with_evidence(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    observe_body(storage, keyword_id, HELP_QUESTION, "要是有自动填志愿的工具就好了")
    cluster(storage)
    score(storage)
    kinds = {
        str(row["statement_kind"])
        for row in rows(storage.conn, "SELECT statement_kind FROM needs WHERE run_id = ?",
                        (storage.run_id,))
    }
    assert {"explicit_problem", "search_intent_candidate", "solution_idea"} <= kinds
    result = export(storage)
    _header, body = parse_csv(file_of(storage, result, "needs.csv"))
    column = {name: i for i, name in enumerate(COLUMNS_BY_FILENAME["needs.csv"])}
    shown = {row[column["statement_kind"]] for row in body}
    assert "search_intent_candidate" not in shown, "联想候选不能冒充需求"
    assert "solution_idea" not in shown
    assert all(row[column["score"]] != "" for row in body)
    assert all(int(row[column["evidence_rows"]]) > 0 for row in body)
    assert body, "显性需求这条总该在"
    assert {row[column["statement_kind"]] for row in body} <= {"explicit_problem", "inferred_need"}


def test_a_broken_reference_removes_the_row_from_the_leaderboard(storage) -> None:
    """失效引用反例：原句不在它引用的版本行里，这条需求就不该出现在排行榜上。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    cluster(storage)
    score(storage)
    version_id = rows(
        storage.conn, "SELECT content_version_id AS id FROM evidence WHERE run_id = ?",
        (storage.run_id,),
    )[0]["id"]
    storage.conn.execute(
        "UPDATE post_content_versions SET text = ? WHERE id = ?", ("帖子被改过了", version_id)
    )
    storage.conn.commit()
    result = export(storage)
    assert file_of(storage, result, "needs.csv").strip().count("\n") == 0, "只剩表头"
    report = json.loads(file_of(storage, result, "report.json"))
    assert report["analysis"]["graph_integrity"]["ok"] is False
    assert report["anomalies"]["violation_codes"].get("evidence_quote_broken")
    assert report["analysis"]["exclusions"]["broken_evidence"]


def test_a_zero_score_need_is_still_displayable_but_a_null_score_is_not(storage) -> None:
    """0 分量是真实但过时的需求，NULL 分是根本没有证据：两者都进得来/进不去 must 可区分。"""
    keyword_id = prepare(storage)
    observe_cards(storage, keyword_id, [Card(HELP_QUESTION, published="2024-01-01")])
    cluster(storage)
    score(storage)
    need = explicit_of(storage)[0]
    result = export(storage)
    _header, body = parse_csv(file_of(storage, result, "needs.csv"))
    column = {name: i for i, name in enumerate(NEED_COLUMNS)}
    row = next(item for item in body if item[column["need_id"]] == str(need["id"]))
    assert row[column["score"]] != "" and float(row[column["score"]]) == float(need["score"])
    assert float(row[column["F_normalized"]]) == 0.0, "解析得出但很久没再被提：F 是真 0，不是 unknown"
    assert row[column["F_missing_reason"]] == ""
    assert row[column["displayable"]] == "1"


# ------------------------------------------------------------------ CSV 安全

def test_formula_like_text_is_prefixed_and_reads_back_identically(storage) -> None:
    for value in ("=1+1", "+8613800000000", "-是的", "@上海中考", "\t缩进", "\r回车"):
        cell = csv_cell(value)
        assert cell.startswith("'"), value
        assert cell[1:] == value
    assert csv_cell("普通标题") == "普通标题"
    assert csv_cell("含\x00nul") == "含\ufffdnul"
    assert csv_cell(True) == "true" and csv_cell(None) == ""


def test_a_title_that_looks_like_a_formula_survives_the_round_trip(storage) -> None:
    keyword_id = prepare(storage)
    tricky = "=上海中考名额分配怎么申请"
    observe_titles(storage, keyword_id, [tricky])
    cluster(storage)
    score(storage)
    result = export(storage)
    text = file_of(storage, result, "needs.csv")
    assert ",=上海" not in text, "原始公式形态不许进 CSV"
    _header, body = parse_csv(text)
    column = {name: i for i, name in enumerate(NEED_COLUMNS)}
    assert body[0][column["canonical_text"]] == f"'{tricky}"
    report = json.loads(file_of(storage, result, "report.json"))
    assert any("撇号" in item for item in report["limitations"])


# ------------------------------------------------------------------ GraphML

def test_graphml_ids_are_xml_names_and_ref_keeps_the_exact_identity(storage) -> None:
    analyze(storage)
    result = export(storage)
    text = file_of(storage, result, "graph.graphml")
    assert text.startswith("<?xml")
    root = ElementTree.fromstring(text)
    prefix = root.tag.split("}")[0] + "}"  # 命名空间前缀，元素名要带它才找得到
    nodes = root.findall(f"{prefix}graph/{prefix}node")
    identifiers = [item.get("id") for item in nodes]
    assert identifiers, "图不为空才有意义"
    assert len(identifiers) == len(set(identifiers))
    assert all(item.replace("_", "").isalnum() for item in identifiers), "id 必须是合法 XML Name"
    names = {key.get("id"): key.get("attr.name") for key in root.findall(f"{prefix}key")}
    refs = {}
    for item in nodes:
        for data in item.findall(f"{prefix}data"):
            if names.get(data.get("key")) == "ref":
                refs[item.get("id")] = data.text or ""
    assert refs, "ref 属性要装得下精确身份"
    assert all(value.split(":")[0] in NODE_PREFIX.values() for value in refs.values())
    report = json.loads(file_of(storage, result, "report.json"))
    scheme = report["graph"]["graphml"]["node_id_scheme"]
    assert set(scheme["prefixes"].values()) <= set(NODE_PREFIX.values())
    assert NODE_PREFIX["topic"] in scheme["prefixes"].values()


def test_complex_and_mixed_attribute_values_become_declared_strings(storage) -> None:
    analyze(storage)
    result = export(storage)
    report = json.loads(file_of(storage, result, "report.json"))
    plan = report["graph"]["graphml"]
    assert plan["declared_types"], "报告要给出每种属性的类型"
    assert all(value in {"boolean", "int", "long", "double", "string"} for value in plan["declared_types"].values())
    text = file_of(storage, result, "graph.graphml")
    assert "None" not in text and "[1, 2]" not in text


def test_illegal_xml_characters_are_scrubbed_from_labels() -> None:
    assert xml_text("含\x00控制符") == "含\ufffd控制符"
    assert xml_text("正常") == "正常"


# ------------------------------------------------------------------ report.json

def test_report_carries_versions_config_budget_and_the_missing_snapshot_caveat(storage) -> None:
    analyze(storage)
    result = export(storage)
    report = json.loads(file_of(storage, result, "report.json"))
    meta = report["meta"]
    for key in ("code_version", "protocol_version", "analysis_version", "extractor_version",
                "schema_version", "head_schema_version"):
        assert meta[key], key
    assert meta["analysis_version"] == ANALYSIS_VERSION
    assert report["run"]["config_hash"]
    assert report["run"]["random_seed"]
    assert report["budget"]["limits_from_config"] is False
    assert any("额度" in item or "配置快照" in item for item in report["limitations"])
    assert report["statistics"]["keywords"]["total"] >= 1


def test_budget_limits_come_from_a_real_config_snapshot(budgeted) -> None:
    report = json.loads(file_of(budgeted, export(budgeted), "report.json"))
    assert report["budget"]["limits_from_config"] is True
    keys = report["budget"]["keys"]
    assert keys["suggest_attempts"]["limit"] == float(
        budgeted.settings.budget.suggest_attempts
    )
    assert keys["max_posts_stored"]["used"] == 3.0
    assert set(keys) >= {"suggest_attempts", "search_attempts", "max_posts_stored"}
    assert not any("额度" in item and "不猜" in item for item in report["limitations"])


# ------------------------------------------------------------------ 反读校验

def test_verification_passes_on_a_fresh_export(storage) -> None:
    analyze(storage)
    result = export(storage)
    assert result.verification.ok, list(result.verification.problems)
    verification = verify_export(storage.conn, storage.paths, storage.run_id)
    assert verification.ok, list(verification.problems)
    assert verification.checks, "反读要有一条条能对上的检查，不是一个布尔"


def test_a_tampered_file_is_caught(storage) -> None:
    analyze(storage)
    result = export(storage, verify=False)
    entry = next(item for item in result.files if item.filename == "needs.csv")
    target = storage.paths.resolve_within_run(entry.rel_path, storage.run_id)
    target.write_bytes(b"need_id,stable_key\n999,fake\n")
    verification = verify_export(storage.conn, storage.paths, storage.run_id)
    assert verification.ok is False
    failed = [item["check"] for item in verification.checks if not item["ok"]]
    assert any("needs.csv" in name for name in failed), failed


def test_a_moved_database_after_export_is_caught(storage) -> None:
    """导出之后库里又写了新事实：反读要按"这份文件还对不对得上现在库"报不一致，而不是沉默。"""
    analyze(storage)
    export(storage)
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["志愿填报怎么改"], session_id="s-later", minutes=30)
    cluster(storage)
    verification = verify_export(storage.conn, storage.paths, storage.run_id)
    assert verification.ok is False


def test_export_refuses_to_overwrite_a_registered_file(storage) -> None:
    analyze(storage)
    export(storage)
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, ["志愿填报怎么改"], session_id="s-new", minutes=31)
    cluster(storage)
    score(storage)
    with pytest.raises(ExportConflict, match="snapshot"):
        export(storage)
    result = export(storage, snapshot="rev2")
    assert result.export_dir.endswith("rev2")
    assert verify_export(storage.conn, storage.paths, storage.run_id, snapshot="rev2").ok


def test_re_exporting_the_same_version_is_idempotent(storage) -> None:
    analyze(storage)
    export(storage)
    second = export(storage)
    assert all(item.written is False for item in second.files), "内容没变就不该重写文件"
    assert all(item.sha256 for item in second.files)


# ------------------------------------------------------------------ 版本与历史

def test_a_new_analysis_version_writes_its_own_directory_and_leaves_history(storage) -> None:
    analyze(storage)
    first = export(storage)
    before = {item.filename: item.sha256 for item in first.files}
    analyze(storage, analysis_version=SECOND_VERSION)
    second = export(storage, analysis_version=SECOND_VERSION)
    assert second.export_dir != first.export_dir
    assert {item.filename: item.sha256 for item in first.files} == before, "旧导出不许被动过"
    for item in second.files:
        assert item.rel_path.startswith("runs/") and storage.run_id in item.rel_path
        assert SECOND_VERSION in item.rel_path
    for row in rows(
        storage.conn,
        "SELECT rel_path, sha256, analysis_version FROM artifacts WHERE run_id = ? AND kind = 'needs_csv'",
        (storage.run_id,),
    ):
        assert str(row["rel_path"]).endswith(f"{row['analysis_version']}/needs.csv")
    report = json.loads(file_of(storage, second, "report.json"))
    assert report["meta"]["analysis_version"] == SECOND_VERSION
    assert json.loads(file_of(storage, first, "report.json"))["meta"]["analysis_version"] == ANALYSIS_VERSION


def test_export_never_touches_preliminary_rows_or_rewards(storage) -> None:
    from xhs_discovery.discovery.merge import PRELIMINARY_ANALYSIS_VERSION

    analyze(storage)
    rewards = rows(storage.conn, "SELECT id, total_reward FROM branch_stats WHERE run_id = ?",
                   (storage.run_id,))
    needs = rows(storage.conn, "SELECT id, score, score_json FROM needs")
    export(storage)
    assert rows(storage.conn, "SELECT id, total_reward FROM branch_stats WHERE run_id = ?",
                (storage.run_id,)) == rewards
    assert rows(storage.conn, "SELECT id, score, score_json FROM needs") == needs
    assert rows(storage.conn, "SELECT id FROM needs WHERE analysis_version = ?",
                (PRELIMINARY_ANALYSIS_VERSION,)) == []


def test_another_run_never_leaks_into_this_export(storage) -> None:
    """跨 run 泄漏是 C-09 的老问题：导出按 run_id 取行，另一个 run 的词与需求一个字都不许进来。"""
    from tests.conftest import build_run_identity

    analyze(storage)
    other = build_run_identity("run-b")
    storage.repo.create_run(other, config_json='{"budget": {}}')
    foreign = storage.repo.ensure_keyword("北京高考", "北京高考", seen_at=T0)
    storage.repo.upsert_run_keyword("run-b", foreign, depth=0, origin="seed",
                                    status="queued", seen_at=T0)
    keywords = file_of(storage, export(storage), "keywords.csv")
    assert "北京高考" not in keywords
    assert "上海中考" in keywords
    report = json.loads(file_of(storage, export_run(storage.conn, storage.paths, "run-b", at=at(21)), "report.json"))
    assert report["meta"]["run_id"] == "run-b"
    assert report["run"]["id"] == "run-b"


# ------------------------------------------------------------------ 证据与来源


def table(storage, result, filename: str, columns: tuple[str, ...]) -> list[dict]:
    """把 CSV 读回成"列名 → 原样单元格"，反读测试与被测代码用同一份列定义。"""
    header, body = parse_csv(file_of(storage, result, filename))
    assert header == list(columns)
    return [dict(zip(header, row)) for row in body]


def test_evidence_csv_keeps_a_locatable_quote_and_full_provenance(storage) -> None:
    analyze(storage)
    result = export(storage)
    rows_of = table(storage, result, "evidence.csv", EVIDENCE_COLUMNS)
    assert rows_of, "正文与标题各一条证据"
    for row in rows_of:
        assert row["locatable"] == "1", "证据进得了导出就必须点得开原句"
        assert row["container_sha256"], "容器哈希是复核的抓手"
        assert row["quote"] and row["char_start"] != ""
        assert row["operation_id"] and row["attempt_no"] and row["session_id"]
        assert row["extractor_version"] == EXTRACTOR_VERSION
    posts = [row for row in rows_of if row["source_field"] in ("title", "body")]
    assert posts and all(row["note_id"] and row["post_id"] for row in posts)
    assert all(row["access_url"].startswith("https://www.xiaohongshu.com/explore/") for row in posts)


def test_a_suggestion_never_masquerades_as_a_post_fact(storage) -> None:
    """平台返回的联想词只是候选：它进图时带自己的节点类型，但一条帖子证据都不许有。"""
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_body(storage, keyword_id, HELP_QUESTION, "名额分配到底要什么条件？")
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    cluster(storage)
    score(storage)
    result = export(storage)
    rows_of = table(storage, result, "evidence.csv", EVIDENCE_COLUMNS)
    assert rows_of and {row["source_field"] for row in rows_of} == {"title", "body"}, (
        "联想文本不产生可定位原文证据：它没有帖子身份，也不能借用别的帖子的"
    )
    for row in rows_of:
        assert row["note_id"] and row["post_id"] and row["content_version_id"]
    graphml = file_of(storage, result, "graph.graphml")
    assert f'"{NODE_PREFIX["suggestion"]}_0"' in graphml
    assert f"{NODE_PREFIX['suggestion']}:1" in graphml, "联想节点的 ref 指向联想版本行"
    for row in table(storage, result, "needs.csv", NEED_COLUMNS):
        assert row["statement_kind"] != "search_intent_candidate"


def test_a_vanished_container_is_reported_and_takes_the_need_down(storage) -> None:
    """重复内容之外最狠的反例是"版本行说这份正文缺失"：证据还在库里，但它再也点不开。"""
    analyze(storage)
    version_id = rows(
        storage.conn,
        "SELECT content_version_id AS id FROM evidence WHERE run_id = ? AND source_field = 'body'",
        (storage.run_id,),
    )
    assert version_id, "样本里得有正文证据"
    storage.conn.execute(
        "UPDATE post_content_versions SET is_missing = 1, text = '' WHERE id = ?",
        (version_id[0]["id"],),
    )
    storage.conn.commit()
    result = export(storage)
    rows_of = table(storage, result, "evidence.csv", EVIDENCE_COLUMNS)
    gone = [row for row in rows_of if row["container_is_missing"] == "1"]
    assert gone and all(row["locatable"] == "0" for row in gone)
    broken = {row["evidence_id"] for row in gone}
    for row in table(storage, result, "needs.csv", NEED_COLUMNS):
        assert not broken & {value for value in row["evidence_ids"].split("|") if value}


# ------------------------------------------------------------------ 关键词表


def test_keywords_csv_describes_the_discovery_graph_not_just_the_seed(storage) -> None:
    keyword_id = prepare(storage)
    observe_titles(storage, keyword_id, [HELP_QUESTION])
    observe_suggestions(storage, keyword_id, ["上海中考志愿怎么填"])
    cluster(storage)
    score(storage)
    rows_of = table(storage, export(storage), "keywords.csv", KEYWORD_COLUMNS)
    assert len(rows_of) == len(
        rows(storage.conn, "SELECT keyword_id FROM run_keywords WHERE run_id = ?", (storage.run_id,))
    )
    seed = next(row for row in rows_of if row["is_seed"] == "1")
    assert seed["normalized"] == "上海中考" and seed["origin"] == "seed"
    assert int(seed["suggestion_observations"]) >= 1 and int(seed["suggestions_returned"]) >= 1
    child = next(row for row in rows_of if row["normalized"] != "上海中考")
    assert child["parent_normalized"] == "上海中考"
    assert int(child["parents"]) >= 1
    assert {row["analysis_version"] for row in rows_of} == {ANALYSIS_VERSION}


# ------------------------------------------------------------------ 值的形状


def test_cell_rendering_keeps_numbers_stable_and_complex_values_machine_readable() -> None:
    assert csv_cell(0.0) == "0.0" and csv_cell(3) == "3"
    assert csv_cell({"b": 1, "a": [1, 2]}) == '{"a": [1, 2], "b": 1}'
    assert csv_cell(["乙", "甲"]) == '["乙", "甲"]'
    assert csv_cell(1e-7) == "1e-07"


def test_graphml_declares_one_type_per_attribute_and_jsonifies_the_rest() -> None:
    """同名属性出现 int 与 bool，或值是列表/字典：GraphML 只能有一个 attr.type，降级成 string 要报备。"""
    nodes = [
        GraphNode("keyword", "kw:1", "上海中考", {"weight": 1, "tags": ["a", "b"]}),
        GraphNode("keyword", "kw:2", "中考", {"weight": True, "tags": []}),
    ]
    plan = render_graphml(
        Graph(run_id="run-a", analysis_version=ANALYSIS_VERSION, nodes=nodes, edges=())
    )
    assert plan.declared_types["weight"] == "string"
    assert "node.tags" in plan.stringified_attrs
    assert plan.nodes == 2 and plan.edges == 0
    _root = ElementTree.fromstring(plan.document)


# ------------------------------------------------------------------ 报告与异常


def test_report_separates_export_bookkeeping_from_run_facts(storage) -> None:
    analyze(storage)
    storage.repo.record_event(storage.run_id, "branch_stalled", detail={"branch_key": "b-analysis"}, at=at(9))
    result = export(storage)
    report = json.loads(file_of(storage, result, "report.json"))
    assert [item["kind"] for item in report["anomalies"]["notable_events"]] == ["branch_stalled"]
    assert "analysis_export" not in report["statistics"]["events"]
    assert "export_verification_failed" not in report["statistics"]["events"]
    assert any("合成 fixture" in item for item in report["limitations"])
    assert report["exported"]["directory"] == result.export_dir


def test_a_stray_file_in_the_export_directory_is_caught(storage) -> None:
    analyze(storage)
    result = export(storage)
    directory = storage.paths.resolve_within_run(result.export_dir, storage.run_id)
    (directory / "extra.txt").write_text("顺手放的", encoding="utf-8")
    verification = verify_export(storage.conn, storage.paths, storage.run_id)
    assert verification.ok is False
    assert any("只含五类产物" in item["check"] and not item["ok"] for item in verification.checks)
