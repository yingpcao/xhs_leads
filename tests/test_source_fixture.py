"""P0-11/P0-12：完全离线的 synthetic 数据源与异常场景。

覆盖的是"录制好的平台返回"是否诚实：
- 同一逻辑键必须逐字段重放出同一结果（否则预算与恢复无法核对）；
- 三分支、三层、多父、环路必须在数据集里真实存在；
- 五种结果状态（completed/partial/failed/skipped/unsupported）与"确实没有 vs 没采到"必须可区分；
- 认证、限频、结构漂移、未知作者/日期/标题、广告、地域/年份/否定都得有对应场景。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xhs_discovery.models.enums import (
    Capability,
    CapabilityState,
    ErrorCategory,
    OperationType,
    ResultState,
)
from xhs_discovery.models.source import OperationContext
from xhs_discovery.sources.fixture import FixtureDataset, FixtureSearchSource

FIXTURE_ROOT = Path("tests/fixtures")
DATASET = "shanghai_zhongkao"


@pytest.fixture
def dataset() -> FixtureDataset:
    return FixtureDataset.load(FIXTURE_ROOT, DATASET)


@pytest.fixture
def source(dataset: FixtureDataset) -> FixtureSearchSource:
    return FixtureSearchSource(dataset)


def context_for(source: FixtureSearchSource, keyword: str, *, attempt_no: int = 1,
                operation_type: OperationType = OperationType.suggest, revisit_seq: int = 0):
    normalized = keyword
    ctx = OperationContext(
        run_id="run-fixture",
        keyword_id=None,
        keyword={"raw": normalized, "normalized": normalized},
        operation_type=operation_type,
        revisit_seq=revisit_seq,
        attempt_no=attempt_no,
        allow_pre_registry=True,
    )
    return source.bind(ctx) or ctx


def test_dataset_loads_and_declares_synthetic(dataset: FixtureDataset, source: FixtureSearchSource):
    assert dataset.name == DATASET
    assert dataset.version == "2"
    assert len(dataset.digest) == 16
    assert source.synthetic is True
    assert source.name == "fixture"
    caps = source.capabilities()
    for capability in Capability:
        assert caps.is_supported(capability)


def test_dataset_name_must_match_filename(tmp_path: Path):
    payload = json.loads((FIXTURE_ROOT / f"{DATASET}.json").read_text(encoding="utf-8"))
    payload["dataset"] = "other_name"
    target = tmp_path / "shanghai_zhongkao.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="数据集名称与文件名不一致"):
        FixtureDataset.load(tmp_path, "shanghai_zhongkao")


def test_missing_dataset_file_reports_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="fixture 数据集不存在"):
        FixtureDataset.load(tmp_path, "nothing_here")


def test_unsupported_capability_declaration_fails_fast(tmp_path: Path):
    payload = {"dataset": "tiny", "version": "1", "capabilities": {"suggestions": "sometimes"}}
    (tmp_path / "tiny.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = FixtureDataset.load(tmp_path, "tiny")
    with pytest.raises(ValueError, match="能力声明非法"):
        FixtureSearchSource(loaded)


def test_three_branches_three_levels_multi_parent_and_cycle_exist(dataset: FixtureDataset):
    seed_branches = dataset.suggestions["上海中考"]
    assert len(seed_branches) >= 3
    # 三层：种子 → 分支 → 分支的分支
    assert dataset.suggestions["上海中考志愿填报"] and dataset.suggestions["上海中考志愿排序"]
    # 多父：建档线同时被多个父词推荐
    parents = [
        parent
        for parent, items in dataset.suggestions.items()
        if "上海中考建档线" in items
    ]
    assert len(parents) >= 3
    # 环路：子词把祖先推荐回来
    assert "上海中考" in dataset.suggestions["上海中考志愿填报"]
    assert "上海中考志愿填报" in dataset.suggestions["上海中考志愿排序"]


def test_suggestions_are_deterministic_per_logical_key(source: FixtureSearchSource):
    first = source.get_suggestions(context_for(source, "上海中考"))
    second = source.get_suggestions(context_for(source, "上海中考"))
    assert first.model_dump() == second.model_dump()
    assert first.session.session_id == second.session.session_id
    assert first.session.synthetic is True
    assert DATASET in first.session.source_version


def test_session_identity_follows_attempt_and_keyword(source: FixtureSearchSource):
    a1 = source.get_suggestions(context_for(source, "上海中考", attempt_no=1))
    a2 = source.get_suggestions(context_for(source, "上海中考", attempt_no=2))
    other = source.get_suggestions(context_for(source, "上海中考自招"))
    assert a1.session.session_id != a2.session.session_id != other.session.session_id
    assert a1.session.collected_at < a2.session.collected_at


def test_session_requires_binding(source: FixtureSearchSource):
    with pytest.raises(ValueError, match="必须先 bind"):
        source.new_session()


def test_empty_platform_result_is_completed_not_failed(source: FixtureSearchSource):
    result = source.get_suggestions(context_for(source, "上海中考空结果"))
    assert result.state is ResultState.completed
    assert result.items == []
    assert result.error is None
    assert any("有效结果" in note for note in result.warnings)


def test_unknown_keyword_yields_empty_completed_not_exception(source: FixtureSearchSource):
    result = source.get_suggestions(context_for(source, "这个词数据集里根本没有"))
    assert result.state is ResultState.completed
    assert result.items == []


def test_retryable_failure_then_success_per_attempt(source: FixtureSearchSource):
    failing = source.get_suggestions(context_for(source, "上海中考重试词", attempt_no=1))
    assert failing.state is ResultState.failed
    assert failing.error is not None and failing.error.retryable
    assert failing.error.code == "WORD_PAGE_TIMEOUT"
    assert failing.error.category is ErrorCategory.network
    recovered = source.get_suggestions(context_for(source, "上海中考重试词", attempt_no=2))
    assert recovered.state is ResultState.completed
    assert [item.text for item in recovered.items][:1] == ["上海中考重试成功后发现的词"]
    # 剧本用完后重复最后一项：停止重试是引擎的职责，数据源不得自己造"第 N 次又坏了"
    beyond = source.get_suggestions(context_for(source, "上海中考重试词", attempt_no=9))
    assert [item.text for item in beyond.items] == [item.text for item in recovered.items]
    assert beyond.session.session_id != recovered.session.session_id
    assert failing.error.detail["synthetic"] == "true"


def test_auth_failure_is_not_retryable_and_needs_human(source: FixtureSearchSource):
    result = source.get_suggestions(context_for(source, "上海中考认证暂停"))
    assert result.state is ResultState.failed
    error = result.error
    assert error is not None
    assert error.exit_code == 3
    assert error.category is ErrorCategory.auth
    assert error.retryable is False
    assert error.human_action_required is True


def test_skipped_is_explicit_and_carries_no_items(source: FixtureSearchSource):
    result = source.get_suggestions(context_for(source, "上海中考跳过词"))
    assert result.state is ResultState.skipped
    assert result.items == []
    assert "rate_limited" in result.warnings


def test_partial_suggestions_must_name_failed_word(source: FixtureSearchSource):
    result = source.get_suggestions(context_for(source, "上海中考未知作者"))
    assert result.state is ResultState.partial
    assert len(result.items) == 2
    assert len(result.per_word) == 1
    assert result.per_word[0].state is ResultState.failed
    assert result.per_word[0].error.code == "WORD_SELECTOR_MISMATCH"
    assert result.per_word[0].error.retryable is False
    assert result.per_word[0].warnings == []


def test_partial_search_reports_per_word_failure(source: FixtureSearchSource):
    result = source.search_posts(context_for(source, "上海中考部分结果",
                                             operation_type=OperationType.search), limit=5)
    assert result.state is ResultState.partial
    assert len(result.items) == 2
    assert [item.keyword.raw for item in result.per_word] == ["上海中考失败的那半"]


def test_search_limit_trims_cards_without_changing_rank(source: FixtureSearchSource):
    ctx = context_for(source, "上海中考", operation_type=OperationType.search)
    limited = source.search_posts(ctx, limit=2)
    assert [card.result_rank for card in limited.items] == [1, 2]
    full = source.search_posts(ctx, limit=8)
    assert len(full.items) == 4


def test_search_rejects_zero_limit(source: FixtureSearchSource):
    with pytest.raises(ValueError, match="limit 必须 >= 1"):
        source.search_posts(context_for(source, "上海中考", operation_type=OperationType.search), limit=0)


def test_card_missing_fields_stay_missing_not_invented(source: FixtureSearchSource):
    ctx = context_for(source, "上海中考未知作者", operation_type=OperationType.search)
    cards = {card.note_id: card for card in source.search_posts(ctx, limit=5).items}
    assert cards["fx-0010"].author.public_ref is None
    assert cards["fx-0010"].author.kind == "unknown"
    assert cards["fx-0007"].published_at_missing is True
    assert cards["fx-0007"].likes_text is None

    untitled = {card.note_id: card for card in source.search_posts(
        context_for(source, "上海中考无标题", operation_type=OperationType.search), limit=5
    ).items}
    assert untitled["fx-0021"].title is None
    assert untitled["fx-0021"].title_missing is True


def test_body_empty_and_body_unsupported_are_different_facts(source: FixtureSearchSource):
    empty = source.fetch_post_body(
        context_for(source, "上海中考", operation_type=OperationType.body), note_id="fx-0017"
    )
    assert empty.state is ResultState.completed
    assert empty.body is None and empty.body_is_empty is True

    blocked = source.fetch_post_body(
        context_for(source, "上海中考", operation_type=OperationType.body), note_id="fx-0016"
    )
    assert blocked.state is ResultState.unsupported
    assert blocked.body is None and blocked.body_is_empty is False
    assert blocked.error.code == "WORD_PAGE_ERROR"
    assert blocked.error.category is ErrorCategory.page

    undeclared = source.fetch_post_body(
        context_for(source, "上海中考", operation_type=OperationType.body), note_id="fx-0021"
    )
    assert undeclared.state is ResultState.unsupported
    assert undeclared.error.code == "CAPABILITY_UNSUPPORTED"
    assert "未登记正文" in " ".join(undeclared.warnings)


def test_body_success_carries_content_version(source: FixtureSearchSource):
    body = source.fetch_post_body(
        context_for(source, "上海中考", operation_type=OperationType.body), note_id="fx-0001"
    )
    assert body.state is ResultState.completed
    assert body.body and "志愿到底怎么排" in body.body
    assert body.content_version.text == body.body


def test_author_posts_use_declared_catalog(source: FixtureSearchSource):
    def fetch(ref: str):
        return source.get_author_posts(
            context_for(source, "上海中考", operation_type=OperationType.author_posts),
            author_ref=ref,
            limit=5,
        )

    result = fetch("user-a1")
    assert result.state is ResultState.completed
    assert [card.note_id for card in result.items] == ["fx-0001", "fx-0018"]

    # 数据集显式声明"确实没有公开笔记"：completed + 空列表，且带上可复核的说明
    declared_empty = fetch("user-a7")
    assert declared_empty.state is ResultState.completed
    assert declared_empty.items == []
    assert any("为空" in note for note in declared_empty.warnings)

    # 未登记的作者 = 没采到，不能伪装成"该作者没有公开笔记"
    unknown = fetch("user-zz")
    assert unknown.state is ResultState.unsupported
    assert unknown.error is not None
    assert unknown.error.code == "CAPABILITY_UNSUPPORTED"
    assert unknown.error.retryable is False and unknown.error.human_action_required is False
    assert any("未登记" in note for note in unknown.warnings)


def test_region_year_and_negation_keywords_have_own_scenarios(dataset: FixtureDataset,
                                                             source: FixtureSearchSource):
    for keyword in ("北京中考分数线", "上海2026中考建档线", "上海中考志愿怎么填不踩坑"):
        assert keyword in dataset.scenario_keywords
        assert keyword in dataset.cards

    beijing = source.search_posts(
        context_for(source, "北京中考分数线", operation_type=OperationType.search), limit=5
    )
    assert [card.note_id for card in beijing.items] == ["fx-0002"]
    assert "北京" in beijing.items[0].title


def test_marketing_note_is_present_so_source_type_can_be_judged(source: FixtureSearchSource):
    cards = source.search_posts(
        context_for(source, "上海中考广告", operation_type=OperationType.search), limit=5
    ).items
    assert [card.note_id for card in cards] == ["fx-0005"]
    assert any("优惠" in card.title or "报名" in card.title for card in cards)


def test_dataset_note_reference_is_validated(source: FixtureSearchSource):
    with pytest.raises(ValueError, match="未登记的笔记"):
        source.dataset.note("fx-9999")
