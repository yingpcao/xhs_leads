"""P0-04：数据源/操作/结果状态契约。核心是"无结果"与"没采成功"必须分开表达。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from xhs_discovery.models.common import RawArtifactRef, SessionRef
from xhs_discovery.models.enums import (
    Capability,
    CapabilityState,
    ErrorCategory,
    OperationType,
    ResultState,
)
from xhs_discovery.models.errors import error_from_exit_code, error_from_word_status
from xhs_discovery.models.source import (
    AuthorPostsResult,
    AuthorRef,
    Capabilities,
    ContentVersionRef,
    KeywordText,
    OperationContext,
    PerWordOutcome,
    PostBodyResult,
    PostSearchResult,
    SuggestionItem,
    SuggestionResult,
)
from xhs_discovery.sources.base import SearchSource


def suggestion(state, ctx_factory, session_factory, items=(), **kw):
    return SuggestionResult(
        state=state, context=ctx_factory(), session=session_factory(), items=list(items), **kw
    )


def test_confirmed_empty_result_is_valid_and_carries_no_error(ctx_factory, session_factory):
    result = suggestion(ResultState.completed, ctx_factory, session_factory)
    assert result.items == []
    assert result.error is None
    assert result.state is ResultState.completed


def test_failed_requires_error_and_cannot_carry_items(ctx_factory, session_factory, auth_error):
    with pytest.raises(ValidationError, match="failed 必须携带 error"):
        suggestion(ResultState.failed, ctx_factory, session_factory)
    with pytest.raises(ValidationError, match="failed 不得返回 items"):
        suggestion(
            ResultState.failed,
            ctx_factory,
            session_factory,
            [SuggestionItem(text="上海中考 志愿", rank=1)],
            error=auth_error,
        )
    failed = suggestion(
        ResultState.failed, ctx_factory, session_factory, error=auth_error
    )
    assert failed.error.human_action_required is True


def test_completed_cannot_carry_error(ctx_factory, session_factory, auth_error):
    with pytest.raises(ValidationError, match="completed 不得携带 error"):
        suggestion(ResultState.completed, ctx_factory, session_factory, error=auth_error)


def test_unsupported_is_not_disguised_as_empty(ctx_factory, session_factory, auth_error):
    with pytest.raises(ValidationError, match="unsupported 必须携带"):
        suggestion(ResultState.unsupported, ctx_factory, session_factory)
    result = suggestion(
        ResultState.unsupported, ctx_factory, session_factory, error=auth_error
    )
    assert result.items == []
    assert result.state is ResultState.unsupported


def test_skipped_cannot_carry_items(ctx_factory, session_factory, card_factory):
    with pytest.raises(ValidationError, match="skipped 不得返回 items"):
        PostSearchResult(
            state=ResultState.skipped,
            context=ctx_factory(OperationType.search),
            session=session_factory(),
            items=[card_factory("n1", 1)],
        )


def test_partial_must_expose_per_word_status(ctx_factory, session_factory, suggestion_factory):
    items = suggestion_factory("上海中考 志愿", "上海中考 分数线")
    with pytest.raises(ValidationError, match="partial 必须提供逐词状态"):
        suggestion(ResultState.partial, ctx_factory, session_factory, items)
    outcome = PerWordOutcome(
        keyword=KeywordText(raw="上海中考 分数线", normalized="上海中考 分数线"),
        state=ResultState.failed,
        error=error_from_word_status("NETWORK_ERROR", "网络抖动"),
    )
    partial = suggestion(
        ResultState.partial, ctx_factory, session_factory, items, per_word=[outcome]
    )
    assert partial.per_word[0].error.retryable is True


def test_per_word_failed_without_error_and_skipped_without_reason_are_rejected():
    kw = KeywordText(raw="上海中考 志愿", normalized="上海中考 志愿")
    with pytest.raises(ValidationError, match="per-word failed 必须携带 error"):
        PerWordOutcome(keyword=kw, state=ResultState.failed)
    with pytest.raises(ValidationError, match="per-word skipped 必须用 warnings"):
        PerWordOutcome(keyword=kw, state=ResultState.skipped)
    with pytest.raises(ValidationError, match="completed 不能同时携带 error"):
        PerWordOutcome(
            keyword=kw, state=ResultState.completed, error=error_from_word_status("OK", "x")
        )


def test_suggestion_order_and_unique_rank_are_preserved(ctx_factory, session_factory):
    with pytest.raises(ValidationError, match="原始顺序"):
        suggestion(
            ResultState.completed,
            ctx_factory,
            session_factory,
            [SuggestionItem(text="b", rank=2), SuggestionItem(text="a", rank=1)],
        )
    with pytest.raises(ValidationError, match="rank 不得重复"):
        suggestion(
            ResultState.completed,
            ctx_factory,
            session_factory,
            [SuggestionItem(text="a", rank=1), SuggestionItem(text="b", rank=1)],
        )


def test_result_classes_are_bound_to_operation_type(ctx_factory, session_factory):
    with pytest.raises(ValidationError, match="只能用于 suggest 操作"):
        SuggestionResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.search),
            session=session_factory(),
            items=[],
        )
    with pytest.raises(ValidationError, match="只能用于 body 操作"):
        PostBodyResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.search),
            session=session_factory(),
            body="文本",
            body_is_empty=False,
        )
    with pytest.raises(ValidationError, match="只能用于 search 操作"):
        PostSearchResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.suggest),
            session=session_factory(),
            items=[],
        )
    with pytest.raises(ValidationError, match="只能用于 author_posts 操作"):
        AuthorPostsResult(
            state=ResultState.unsupported,
            context=ctx_factory(OperationType.suggest),
            session=session_factory(),
            error=error_from_word_status("OK", "占位"),
        )


def test_post_search_dedupes_note_id_and_keeps_result_order(ctx_factory, session_factory, card_factory):
    with pytest.raises(ValidationError, match="note_id 必须去重"):
        PostSearchResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.search),
            session=session_factory(),
            items=[card_factory("n1", 1), card_factory("n1", 2)],
        )
    with pytest.raises(ValidationError, match="原始顺序"):
        PostSearchResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.search),
            session=session_factory(),
            items=[card_factory("n2", 2), card_factory("n1", 1)],
        )


def test_post_card_missing_markers_are_mandatory(card_factory):
    with pytest.raises(ValidationError, match="title_missing"):
        card_factory("n1", 1, title=None)
    with pytest.raises(ValidationError, match="published_at_missing"):
        card_factory("n1", 1, published_at_text=None)
    with pytest.raises(ValidationError, match="完整链接"):
        card_factory("n1", 1, access_url="/explore/n1")
    ok = card_factory("n1", 1, title=None, title_missing=True, published_at_text=None,
                      published_at_missing=True)
    assert ok.title is None


def test_author_reference_keeps_unknown_instead_of_inventing_identity():
    assert AuthorRef(kind="unknown").public_ref is None
    with pytest.raises(ValidationError, match="不得填写 public_ref"):
        AuthorRef(kind="unknown", public_ref="user-9")
    with pytest.raises(ValidationError, match="public_ref 必填"):
        AuthorRef(kind="public_ref")


def test_body_result_separates_inaccessible_from_really_empty(ctx_factory, session_factory, auth_error):
    with pytest.raises(ValidationError, match="不能记 completed"):
        PostBodyResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.body),
            session=session_factory(),
        )
    empty = PostBodyResult(
        state=ResultState.completed,
        context=ctx_factory(OperationType.body),
        session=session_factory(),
        body=None,
        body_is_empty=True,
    )
    assert empty.body_is_empty is True
    with pytest.raises(ValidationError, match="unsupported 不得返回 正文"):
        PostBodyResult(
            state=ResultState.unsupported,
            context=ctx_factory(OperationType.body),
            session=session_factory(),
            body="伪造正文",
            error=auth_error,
        )


def test_body_requires_matching_content_version(ctx_factory, session_factory):
    text = "志愿到底怎么排，求助"
    from xhs_discovery.models.common import sha256_text

    with pytest.raises(ValidationError, match="必须带内容版本"):
        PostBodyResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.body),
            session=session_factory(),
            body=text,
        )
    version = ContentVersionRef(text=text, text_sha256=sha256_text(text))
    result = PostBodyResult(
        state=ResultState.completed,
        context=ctx_factory(OperationType.body),
        session=session_factory(),
        body=text,
        content_version=version,
    )
    assert result.content_version.text_sha256 == sha256_text(text)
    with pytest.raises(ValidationError, match="哈希与文本不一致"):
        ContentVersionRef(text=text, text_sha256="0" * 64)
    with pytest.raises(ValidationError, match="文本必须与返回正文一致"):
        PostBodyResult(
            state=ResultState.completed,
            context=ctx_factory(OperationType.body),
            session=session_factory(),
            body=text,
            content_version=ContentVersionRef(text=text + "改", text_sha256=sha256_text(text + "改")),
        )


def test_capabilities_declare_the_verified_title_only_boundary():
    caps = Capabilities.title_only()
    assert caps.is_supported(Capability.suggestions)
    assert caps.state_of(Capability.post_body) is CapabilityState.unsupported
    assert caps.state_of(Capability.author_posts) is CapabilityState.unsupported


def test_operation_context_exposes_logical_key(ctx_factory):
    ctx = ctx_factory(revisit_seq=2, attempt_no=3)
    assert ctx.logical_key == ("run-a", 1, "suggest", 2)
    with pytest.raises(ValidationError):
        ctx_factory(attempt_no=0)
    with pytest.raises(ValidationError):
        ctx_factory(revisit_seq=-1)


def test_missing_keyword_id_requires_explicit_pre_registry(ctx_factory):
    """没有 keyword_id 就进入调度会共享 None 幂等键，必须显式声明预入库。"""
    with pytest.raises(ValidationError, match="必须已取得 keyword_id"):
        ctx_factory(keyword_id=None)
    ctx = ctx_factory(keyword_id=None, allow_pre_registry=True)
    assert ctx.keyword_identity == "norm:上海中考"


def test_pre_registry_contexts_do_not_share_logical_key_across_keywords(ctx_factory):
    a = ctx_factory(keyword_id=None, allow_pre_registry=True,
                    keyword=KeywordText(raw="上海中考", normalized="上海中考"))
    b = ctx_factory(keyword_id=None, allow_pre_registry=True,
                    keyword=KeywordText(raw="上海中考 志愿", normalized="上海中考 志愿"))
    assert a.logical_key != b.logical_key
    assert a.logical_key[1] != b.logical_key[1]
    # 同一词的别名写法规范化后仍应复用同一身份，避免重复扣预算
    a_alias = ctx_factory(keyword_id=None, allow_pre_registry=True,
                          keyword=KeywordText(raw=" 上海中考 ", normalized="上海中考"))
    assert a_alias.logical_key == a.logical_key
    # 入库后以 keyword_id 为身份，与预入库文本键不同层，由调度在落库后统一
    registered = ctx_factory(keyword_id=42)
    assert registered.logical_key == ("run-a", 42, "suggest", 0)


def test_raw_artifact_paths_stay_inside_the_run_directory():
    from xhs_discovery.models.common import sha256_text

    digest = sha256_text("{}")
    ok = RawArtifactRef(path="runs/run-a/operations/k1/suggest/r0/a1/stdout.json",
                        sha256=digest, size_bytes=2, kind="stdout_json")
    assert ok.kind == "stdout_json"
    with pytest.raises(ValidationError, match="相对路径"):
        RawArtifactRef(path="D:/outside/stdout.json", sha256=digest, size_bytes=2, kind="stdout_json")
    with pytest.raises(ValidationError, match=r"\.\."):
        RawArtifactRef(path="runs/../../etc/passwd", sha256=digest, size_bytes=2, kind="stdout_json")
    with pytest.raises(ValidationError):
        RawArtifactRef(path="runs/a.json", sha256="xyz", size_bytes=2, kind="stdout_json")


TRAVELLING_PATHS = [
    ("../outside.json", "穿越"),
    (r"..\outside.json", "穿越"),
    ("runs/run-a/../../outside.json", "穿越"),
    (r"runs/run-a\..\..\outside.json", "穿越"),
    ("runs/run-a/..\\..\\outside.json", "穿越"),
    ("C:/Windows/win.ini", "绝对路径"),
    (r"C:\runs\stdout.json", "绝对路径"),
    ("D:/outside/stdout.json", "绝对路径"),
    ("//server/share/stdout.json", "绝对路径"),
    (r"\\server\share\stdout.json", "绝对路径"),
    ("/etc/passwd", "绝对路径"),
    (r"\runs\stdout.json", "绝对路径"),
]


@pytest.mark.parametrize("value", [p for p, _ in TRAVELLING_PATHS])
def test_raw_artifact_ref_rejects_windows_and_posix_escape_forms(value):
    """两种解析器都要检查：只用 Posix 规则会放过 `..\\`、盘符与根相对路径。"""
    from xhs_discovery.models.common import sha256_text

    with pytest.raises(ValidationError) as exc:
        RawArtifactRef(
            path=value, sha256=sha256_text("{}"), size_bytes=1, kind="stdout_json"
        )
    expected_reason = dict(TRAVELLING_PATHS)[value]
    assert expected_reason in str(exc.value)


def test_raw_artifact_ref_accepts_plain_relative_paths():
    from xhs_discovery.models.common import sha256_text

    for value in (
        "runs/run-a/operations/k1/suggest/r0/a1/stdout.json",
        "runs/run-a/stderr.txt",
        "runs/run-a/exports/needs.csv",
    ):
        ref = RawArtifactRef(path=value, sha256=sha256_text("{}"), size_bytes=1, kind="stdout_json")
        assert ref.path == value


def test_session_timestamps_must_be_timezone_aware(ctx_factory):
    with pytest.raises(ValidationError, match="必须带时区"):
        SessionRef(
            session_id="s",
            collected_at=datetime(2026, 10, 24, 12, 0, 0),
            source_name="fixture",
            source_version="1",
        )


def test_result_serializes_and_round_trips(ctx_factory, session_factory, suggestion_factory):
    result = suggestion(
        ResultState.completed, ctx_factory, session_factory, suggestion_factory("上海中考 志愿")
    )
    payload = result.model_dump_json()
    restored = SuggestionResult.model_validate_json(payload)
    assert restored == result
    assert restored.session.synthetic is True


class _TitleOnlySource(SearchSource):
    name = "title-only-fixture"
    version = "0"
    synthetic = True

    def capabilities(self) -> Capabilities:
        return Capabilities.title_only()

    def new_session(self) -> SessionRef:
        return SessionRef(
            session_id="s1",
            collected_at=datetime.now(timezone.utc),
            source_name=self.name,
            source_version="0",
            synthetic=True,
        )

    def get_suggestions(self, context: OperationContext) -> SuggestionResult:
        return SuggestionResult(
            state=ResultState.completed,
            context=context,
            session=self.new_session(),
            items=[],
        )


def test_source_base_returns_unsupported_not_fake_empty_for_missing_capabilities(
    ctx_factory,
):
    source = _TitleOnlySource()
    body = source.fetch_post_body(ctx_factory(), note_id="n1")
    assert body.state is ResultState.unsupported
    assert body.body is None
    assert body.error is not None and body.error.code == "CAPABILITY_UNSUPPORTED"
    authors = source.get_author_posts(ctx_factory(), author_ref="user-1", limit=5)
    assert authors.state is ResultState.unsupported


def test_search_posts_routes_to_source_when_capability_is_declared(ctx_factory, card_factory):
    class _CardSource(_TitleOnlySource):
        name = "cards-fixture"

        def capabilities(self) -> Capabilities:
            return Capabilities(
                suggestions=CapabilityState.supported,
                search_cards=CapabilityState.supported,
                post_body=CapabilityState.unsupported,
                author_posts=CapabilityState.unsupported,
            )

        def _search_posts(self, context, *, limit, session):
            return PostSearchResult(
                state=ResultState.completed,
                context=context,
                session=session,
                items=[card_factory("n1", 1)] if limit else [],
            )

    declared = _CardSource().search_posts(ctx_factory(OperationType.search), limit=3)
    assert declared.state is ResultState.completed
    assert [c.note_id for c in declared.items] == ["n1"]
    # title_only 声明 search_cards 可用但未实现，属于适配器缺陷而不是空结果
    with pytest.raises(NotImplementedError):
        _TitleOnlySource().search_posts(ctx_factory(OperationType.search), limit=3)


def test_declaring_capability_without_implementing_it_fails_loudly(ctx_factory):
    class _LiarSource(_TitleOnlySource):
        name = "liar"

        def capabilities(self) -> Capabilities:
            return Capabilities(
                suggestions=CapabilityState.supported,
                search_cards=CapabilityState.supported,
                post_body=CapabilityState.supported,
                author_posts=CapabilityState.supported,
            )

    liar = _LiarSource()
    with pytest.raises(NotImplementedError, match="_search_posts"):
        liar.search_posts(ctx_factory(OperationType.search), limit=1)
    with pytest.raises(NotImplementedError, match="_fetch_post_body"):
        liar.fetch_post_body(ctx_factory(OperationType.body), note_id="n1")
    with pytest.raises(NotImplementedError, match="_get_author_posts"):
        liar.get_author_posts(ctx_factory(OperationType.author_posts), author_ref="u", limit=1)


@pytest.mark.parametrize(
    "code, category, retryable, human",
    [
        (1, ErrorCategory.system, False, True),
        (2, ErrorCategory.usage, False, True),
        (3, ErrorCategory.auth, False, True),
        (4, ErrorCategory.action_required, False, True),
        (6, ErrorCategory.profile_busy, True, False),
        (130, ErrorCategory.cancelled, False, False),
    ],
)
def test_cli_exit_codes_map_to_the_documented_policy(code, category, retryable, human):
    err = error_from_exit_code(code, "样例")
    assert err.category is category
    assert err.retryable is retryable
    assert err.human_action_required is human
    assert err.exit_code == code


def test_unknown_exit_code_is_not_silently_treated_as_no_results():
    err = error_from_exit_code(99, "意外退出码")
    assert err.code == "UNKNOWN_CLASSIFICATION"
    assert err.human_action_required is True
    assert err.retryable is False


@pytest.mark.parametrize("code", [0, 7])
def test_success_and_partial_codes_are_not_classified_as_failures(code):
    err = error_from_exit_code(code, "应由状态表达")
    assert err.code == "UNKNOWN_CLASSIFICATION"
    assert "状态" in err.message


@pytest.mark.parametrize(
    "status, retryable, human",
    [
        ("AUTH_REQUIRED", False, True),
        ("ACTION_REQUIRED", False, True),
        ("NETWORK_ERROR", True, False),
        ("PAGE_TIMEOUT", True, False),
        ("SELECTOR_MISMATCH", False, True),
        ("OUTPUT_ERROR", False, True),
        ("BROWSER_CLOSED", False, False),
        ("SKIPPED", False, False),
    ],
)
def test_word_status_classification(status, retryable, human):
    err = error_from_word_status(status, "逐词")
    assert err.retryable is retryable
    assert err.human_action_required is human


def test_unknown_word_status_requires_human_check():
    err = error_from_word_status("TELEPATHY", "未定义")
    assert err.code == "UNKNOWN_CLASSIFICATION"
    assert err.human_action_required is True


@pytest.mark.parametrize(
    "code, category, retryable, human",
    [
        ("NETWORK_ERROR", ErrorCategory.network, True, False),
        ("PAGE_TIMEOUT", ErrorCategory.network, True, False),
        ("SELECTOR_MISMATCH", ErrorCategory.page, False, True),
        ("PAGE_ERROR", ErrorCategory.page, False, True),
        ("PROFILE_BUSY", ErrorCategory.profile_busy, True, False),
        ("OUTPUT_ERROR", ErrorCategory.output, False, True),
    ],
)
def test_exit_code_5_is_resolved_by_the_per_word_error_code(code, category, retryable, human):
    """退出码 5 覆盖浏览器/网络/页面/选择器，不能一律标可重试或一律当未知协议。"""
    from xhs_discovery.models.errors import classify_cli_failure

    err = classify_cli_failure(5, "引擎报告失败", error_code=code)
    assert err.category is category
    assert err.retryable is retryable
    assert err.human_action_required is human
    assert err.exit_code == 5
    assert err.detail["exit_code_signal"] == "5"


def test_exit_code_5_without_error_code_falls_back_to_page_pause():
    from xhs_discovery.models.errors import classify_cli_failure

    err = classify_cli_failure(5, "只有退出码")
    assert err.code == "CLI_EXIT_5"
    assert err.category is ErrorCategory.page
    assert err.retryable is False
    assert err.human_action_required is True


@pytest.mark.parametrize(
    "exit_code, error_code, expected_category",
    [
        (3, "NETWORK_ERROR", ErrorCategory.auth),
        (4, "PAGE_TIMEOUT", ErrorCategory.action_required),
        (5, "AUTH_REQUIRED", ErrorCategory.auth),
        (5, "ACTION_REQUIRED", ErrorCategory.action_required),
    ],
)
def test_auth_and_action_signals_always_pause(exit_code, error_code, expected_category):
    """认证/验证优先暂停：进程级 3/4 不得被逐词网络抖动降级成可重试。"""
    from xhs_discovery.models.errors import classify_cli_failure

    err = classify_cli_failure(exit_code, "组合信号", error_code=error_code)
    assert err.category is expected_category
    assert err.retryable is False
    assert err.human_action_required is True
    assert err.detail["word_status"] == error_code


def test_non_auth_exit_code_keeps_the_per_word_retry_decision():
    from xhs_discovery.models.errors import classify_cli_failure

    err = classify_cli_failure(1, "系统错误期间逐词报网络抖动", error_code="NETWORK_ERROR")
    assert err.category is ErrorCategory.network
    assert err.retryable is True and err.human_action_required is False


def test_unregistered_error_code_with_exit_code_still_needs_human_check():
    from xhs_discovery.models.errors import classify_cli_failure

    err = classify_cli_failure(5, "新状态", error_code="QUANTUM_FAILURE")
    assert err.code == "UNKNOWN_CLASSIFICATION"
    assert err.human_action_required is True
    assert err.retryable is False


def test_stderr_excerpt_is_truncated_into_detail():
    from xhs_discovery.models.errors import classify_cli_failure

    err = classify_cli_failure(2, "用法错误", stderr_excerpt="x" * 900)
    assert len(err.detail["stderr_excerpt"]) == 400
