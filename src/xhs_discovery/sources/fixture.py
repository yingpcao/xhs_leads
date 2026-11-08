"""完全离线的 synthetic 数据源（P0-11/P0-12）。

数据集是"录制好的平台返回"，不是运行时随机生成：同一份数据集 + 同一个逻辑键
（run / 关键词 / 操作 / 重探 / 尝试）永远得到同样的结果，因此重放可以逐字段比对。

规则：
- 每个返回都带 `synthetic=True` 的会话与数据集版本，模拟数据不得伪装真实来源；
- "平台确实没有" 用 completed + 空 items 表达，"没采到" 用 failed + error 表达；
- 未声明正文的笔记返回 unsupported，不返回"空正文"——确实为空必须是显式事实；
- 数据集未登记的作者列表同样返回 unsupported；"作者确实没有公开笔记"必须由数据集显式声明空列表；
- 关键词不在数据集里时按"平台无联想/无卡片"处理（completed 空结果），数据集覆盖它能
  产出的全部词，因此测试里的拼写错误表现为空结果而不是异常。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xhs_discovery._version import CODE_VERSION
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.enums import (
    Capability,
    CapabilityState,
    ResultState,
)
from xhs_discovery.models.errors import ErrorInfo, classify_cli_failure
from xhs_discovery.models.source import (
    AuthorPostsResult,
    Capabilities,
    ContentVersionRef,
    KeywordText,
    OperationContext,
    PerWordOutcome,
    PostBodyResult,
    PostCard,
    PostSearchResult,
    SuggestionItem,
    SuggestionResult,
    AuthorRef,
)
from xhs_discovery.sources.base import AccessNotStarted, SearchSource, unsupported_error
from xhs_discovery.text.normalize import normalize_text

DATASET_SUFFIX = ".json"
# fixture 的观察时间只由逻辑键推导，绝不用墙上时钟：否则"同配置可重放"永远测不出来。
_BASE_TIME = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)


class FixtureDataset:
    """一个数据集文件一次加载；名称不一致或缺字段立刻报错，不做静默降级。"""

    def __init__(self, payload: dict[str, Any], *, path: Path, digest: str) -> None:
        if payload.get("dataset") != path.stem:
            raise ValueError(f"数据集名称与文件名不一致：{payload.get('dataset')!r} != {path.stem}")
        self.path = path
        self.name = str(payload["dataset"])
        self.version = str(payload["version"])
        self.payload = payload
        self.notes: dict[str, dict[str, Any]] = payload.get("notes", {})
        self.suggestions: dict[str, list[str]] = payload.get("suggestions", {})
        self.cards: dict[str, list[str]] = payload.get("cards", {})
        self.bodies: dict[str, str] = {
            note_id: str(note["body"]) for note_id, note in self.notes.items() if note.get("body")
        }
        self.ambiguous_seeds = tuple(payload.get("ambiguous_seeds", ()))
        self.regions = tuple(payload.get("regions", ()))
        self.scenario_keywords = tuple(payload.get("scenario_keywords", ()))
        self.digest = digest[:16]

    @classmethod
    def load(cls, base_path: Path, dataset: str) -> "FixtureDataset":
        path = Path(base_path) / f"{dataset}{DATASET_SUFFIX}"
        if not path.exists():
            raise FileNotFoundError(f"fixture 数据集不存在：{path}")
        raw = path.read_bytes()
        return cls(json.loads(raw.decode("utf-8")), path=path, digest=hashlib.sha256(raw).hexdigest())

    def scenario(self, kind: str, key: str) -> dict[str, Any] | None:
        value = self.payload.get(kind, {}).get(key)
        return value if isinstance(value, dict) else None

    def attempt_spec(self, kind: str, keyword: str, attempt_no: int) -> dict[str, Any]:
        """按尝试序号取剧本；超出剧本长度时重复最后一项（停止重试是引擎的职责，不是数据源的）。"""
        scenario = self.scenario(kind, keyword)
        if scenario is None:
            return {}
        attempts = scenario.get("attempts")
        if isinstance(attempts, list) and attempts:
            index = min(max(attempt_no, 1), len(attempts)) - 1
            spec = dict(attempts[index])
            spec.setdefault("note", scenario.get("note"))
            return spec
        return dict(scenario)

    def note(self, note_id: str) -> dict[str, Any]:
        note = self.notes.get(note_id)
        if note is None:
            raise ValueError(f"fixture 引用了未登记的笔记：{note_id}")
        return note


def _error_from_spec(spec: dict[str, Any], *, default_message: str) -> ErrorInfo:
    exit_code = spec.get("exit_code")
    error_code = spec.get("error_code")
    message = str(spec.get("message") or default_message)
    if exit_code is None and not error_code:
        # 未编码的失败也要给出可复核的分类，不能只有一句"失败了"
        return ErrorInfo(
            code="FIXTURE_FAILURE",
            message=message,
            category="system",
            retryable=False,
            human_action_required=True,
            detail={"synthetic": "true"},
        )
    info = classify_cli_failure(
        int(exit_code) if exit_code is not None else 5,
        message,
        error_code=str(error_code) if error_code else None,
    )
    detail = dict(info.detail)
    detail["synthetic"] = "true"
    return info.model_copy(update={"detail": detail})


def _skip_error(spec: dict[str, Any], *, default_message: str) -> ErrorInfo | None:
    """数据集为 skipped 场景声明了退出码/错误码时，把原因带回去。

    skipped 同样要能复核"为什么没采"，只留一句 warning 会和"平台确实没有"混成一类。
    """
    if spec.get("exit_code") is None and not spec.get("error_code"):
        return None
    return _error_from_spec(spec, default_message=default_message)


def _warnings(spec: dict[str, Any]) -> list[str]:
    warnings = [str(item) for item in spec.get("warnings", [])]
    note = spec.get("note")
    if note:
        warnings.append(str(note))
    return warnings


def _per_word(specs: list[dict[str, Any]], fallback: KeywordText) -> list[PerWordOutcome]:
    outcomes: list[PerWordOutcome] = []
    for entry in specs:
        raw = str(entry.get("keyword") or fallback.raw)
        state = ResultState(str(entry.get("state", ResultState.failed)))
        outcomes.append(
            PerWordOutcome(
                keyword=KeywordText(raw=raw, normalized=normalize_text(raw)),
                state=state,
                error=_error_from_spec(entry, default_message="逐词失败（fixture 模拟）")
                if state is ResultState.failed
                else None,
                warnings=[str(item) for item in entry.get("warnings", [])],
            )
        )
    return outcomes


class FixtureSearchSource(SearchSource):
    """合成数据源：能力、会话与结果全部来自数据集，绝不联网、不读凭据、不接模型。"""

    name = "fixture"
    version = CODE_VERSION
    synthetic = True

    def __init__(self, dataset: FixtureDataset) -> None:
        self.dataset = dataset
        self._bound: OperationContext | None = None
        self._capabilities = self._read_capabilities(dataset)

    @staticmethod
    def _read_capabilities(dataset: FixtureDataset) -> Capabilities:
        raw = dataset.payload.get("capabilities", {})
        try:
            return Capabilities(
                suggestions=CapabilityState(str(raw.get("suggestions", "unsupported"))),
                search_cards=CapabilityState(str(raw.get("search_cards", "unsupported"))),
                post_body=CapabilityState(str(raw.get("post_body", "unsupported"))),
                author_posts=CapabilityState(str(raw.get("author_posts", "unsupported"))),
            )
        except ValueError as exc:  # 数据集写错能力值时立刻失败，不静默当 unsupported
            raise ValueError(f"fixture 数据集能力声明非法：{exc}") from exc

    def bind(self, context: OperationContext) -> OperationContext:
        """引擎在每次操作前绑定逻辑键，会话号由此派生（保证重放与补导入得到同一 session_id）。"""
        self._bound = context
        return context

    # 基类在派发到 _search_posts 之前就要 new_session()，所以这三条公开入口自己先绑定，
    # 否则单测里直接调用 source.search_posts 会得到"未绑定"错误，绑定责任也说不清属于谁。
    def search_posts(self, context: OperationContext, *, limit: int) -> PostSearchResult:
        self.bind(context)
        return super().search_posts(context, limit=limit)

    def fetch_post_body(self, context: OperationContext, *, note_id: str) -> PostBodyResult:
        self.bind(context)
        return super().fetch_post_body(context, note_id=note_id)

    def get_author_posts(
        self, context: OperationContext, *, author_ref: str, limit: int
    ) -> AuthorPostsResult:
        self.bind(context)
        return super().get_author_posts(context, author_ref=author_ref, limit=limit)

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def _session_source_version(self) -> str:
        """会话来源版本的唯一出处：new_session 与恢复核验共用，绝不出现第二套拼接。"""
        return (
            f"{self.version}+{self.dataset.name}@{self.dataset.version}#{self.dataset.digest}"
        )

    def session_matches_source(self, session: SessionRef, raw_responses=()) -> bool:
        """fixture 恢复核验：来源身份必须与**恢复后重建的数据源实例**逐字相等。

        恢复用的数据集是 run 创建时固化的字节副本，name/version/digest 由内容哈希派生，
        同一数据集重建必然得到同一身份；对不上就是换了来源，不得当作本次结果补入库。
        fixture 不产出原始响应文件，`raw_responses` 恒为空、也无需参与派生。
        """
        return (
            session.source_name == self.name
            and session.source_version == self._session_source_version()
        )

    def new_session(self) -> SessionRef:
        if self._bound is None:
            raise AccessNotStarted(
                "fixture 的会话号是逻辑键的纯函数，调用操作前必须先 bind(context)；"
                "未绑定就取会话说明调用方绕过了引擎（请求根本没发出，可以退回预留）"
            )
        context = self._bound
        identity = "|".join(
            (
                context.run_id,
                str(context.keyword_identity),
                context.operation_type.value,
                str(context.revisit_seq),
                str(context.attempt_no),
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        offset = (context.revisit_seq * 100 + context.attempt_no) * 60
        return SessionRef(
            session_id=f"fx-{digest}",
            collected_at=_BASE_TIME + timedelta(seconds=offset),
            source_name=self.name,
            source_version=self._session_source_version(),
            synthetic=True,
        )

    # ------------------------------------------------------------------ 联想

    def get_suggestions(self, context: OperationContext) -> SuggestionResult:
        self.bind(context)
        session = self.new_session()
        spec = self.dataset.attempt_spec(
            "suggestion_scenarios", context.keyword.normalized, context.attempt_no
        )
        state = ResultState(str(spec.get("state", ResultState.completed)))
        warnings = _warnings(spec)
        if state in (ResultState.failed, ResultState.unsupported):
            message = "联想采集失败（fixture 模拟）"
            if state is ResultState.unsupported:
                message = f"{self.name} 不支持 {Capability.suggestions.value}：{spec.get('message', '数据集声明该场景无联想能力')}"
            return SuggestionResult(
                state=state,
                context=context,
                session=session,
                items=[],
                error=_error_from_spec(spec, default_message=message),
                warnings=warnings,
            )
        texts = spec.get("items")
        if texts is None:
            texts = self.dataset.suggestions.get(context.keyword.normalized, [])
        items = [SuggestionItem(text=str(text), rank=index + 1) for index, text in enumerate(texts)]
        per_word = _per_word(spec.get("per_word", []), context.keyword)
        if state is ResultState.partial and not per_word:
            raise ValueError("fixture 的联想 partial 场景必须声明失败词，否则与 completed 无法区分")
        if state is ResultState.skipped:
            items = []
            warnings = warnings or ["fixture 声明跳过该词"]
        return SuggestionResult(
            state=state,
            context=context,
            session=session,
            items=items,
            per_word=per_word,
            warnings=warnings,
            error=_skip_error(spec, default_message="该词被跳过（fixture 模拟）"),
        )

    # ------------------------------------------------------------------ 卡片

    def _search_posts(
        self, context: OperationContext, *, limit: int, session: SessionRef
    ) -> PostSearchResult:
        if limit < 1:
            raise AccessNotStarted(
                "limit 必须 >= 1：0 卡片的采集没有意义，请用 skipped 表达"
            )
        spec = self.dataset.attempt_spec("search_scenarios", context.keyword.normalized, context.attempt_no)
        state = ResultState(str(spec.get("state", ResultState.completed)))
        warnings = _warnings(spec)
        if state in (ResultState.failed, ResultState.unsupported):
            return PostSearchResult(
                state=state,
                context=context,
                session=session,
                items=[],
                error=_error_from_spec(spec, default_message="搜索失败（fixture 模拟）"),
                warnings=warnings,
            )
        note_ids = list(spec.get("note_ids") or self.dataset.cards.get(context.keyword.normalized, []))
        cards = [
            self._card(note_id, index + 1, context.keyword)
            for index, note_id in enumerate(note_ids[:limit])
        ]
        per_word = _per_word(spec.get("per_word", []), context.keyword)
        if state is ResultState.partial and not per_word:
            raise ValueError("fixture 的搜索 partial 场景必须声明失败词，否则与 completed 无法区分")
        if state is ResultState.partial and not cards:
            raise ValueError("fixture 的 partial 场景必须有可解析部分，全失败应记 failed")
        if state is ResultState.skipped:
            cards = []
            warnings = warnings or ["fixture 声明跳过该词"]
        return PostSearchResult(
            state=state,
            context=context,
            session=session,
            items=cards,
            per_word=per_word,
            warnings=warnings,
            error=_skip_error(spec, default_message="该词被跳过（fixture 模拟）"),
        )

    def _card(self, note_id: str, rank: int, keyword: KeywordText) -> PostCard:
        note = self.dataset.note(note_id)
        title = note.get("title")
        published = note.get("published_at")
        author_ref = note.get("author_public_ref")
        prefix = self.dataset.payload.get("note_url_prefix", "https://www.xiaohongshu.com/explore/")
        likes = note.get("likes")
        return PostCard(
            note_id=note_id,
            access_url=f"{prefix}{note_id}",
            title=title,
            title_missing=title is None,
            author=AuthorRef(
                kind="unknown" if author_ref is None else "public_ref",
                public_ref=None if author_ref is None else str(author_ref),
            ),
            published_at_text=published,
            published_at_missing=published is None,
            likes_text=None if likes is None else str(likes),
            result_rank=rank,
            search_keyword=keyword,
        )

    # ------------------------------------------------------------------ 正文

    def _fetch_post_body(
        self, context: OperationContext, *, note_id: str, session: SessionRef
    ) -> PostBodyResult:
        spec = self.dataset.scenario("body_scenarios", note_id) or {}
        declared = self.dataset.bodies.get(note_id)
        if spec:
            state = ResultState(str(spec.get("state", ResultState.completed)))
        else:
            # 数据集没写正文，就是"不可访问"，不能伪装成"确实为空"
            state = ResultState.completed if declared is not None else ResultState.unsupported
        warnings = _warnings(spec)
        if state in (ResultState.failed, ResultState.unsupported, ResultState.skipped):
            return PostBodyResult(
                state=state,
                context=context,
                session=session,
                body=None,
                error=(
                    _error_from_spec(spec, default_message=f"正文不可访问（fixture 模拟 {note_id}）")
                    if spec
                    else unsupported_error(
                        Capability.post_body,
                        self.name,
                        f"fixture 数据集未登记 {note_id} 的正文，无法区分确实为空",
                    )
                ),
                warnings=warnings or ["fixture：该笔记未登记正文"],
            )
        if spec.get("body_is_empty"):
            return PostBodyResult(
                state=ResultState.completed,
                context=context,
                session=session,
                body=None,
                body_is_empty=True,
                warnings=warnings,
            )
        if declared is None:
            raise ValueError(f"fixture 未登记 {note_id} 的正文，无法记 completed")
        return PostBodyResult(
            state=ResultState.completed,
            context=context,
            session=session,
            body=declared,
            content_version=ContentVersionRef(
                text=declared, text_sha256=hashlib.sha256(declared.encode("utf-8")).hexdigest()
            ),
            warnings=warnings,
        )

    # ------------------------------------------------------------------ 作者公开笔记

    def _get_author_posts(
        self, context: OperationContext, *, author_ref: str, limit: int, session: SessionRef
    ) -> AuthorPostsResult:
        spec = self.dataset.scenario("author_scenarios", author_ref) or {}
        state = ResultState(str(spec.get("state", ResultState.completed)))
        warnings = _warnings(spec)
        if state in (ResultState.failed, ResultState.unsupported, ResultState.skipped):
            return AuthorPostsResult(
                state=state,
                context=context,
                session=session,
                items=[],
                error=(
                    _error_from_spec(spec, default_message=f"作者列表不可访问：{author_ref}")
                    if spec
                    else unsupported_error(
                        Capability.author_posts,
                        self.name,
                        f"fixture 数据集未登记作者 {author_ref} 的公开笔记列表",
                    )
                ),
                warnings=warnings or ["fixture：该作者未登记公开笔记"],
            )
        catalog = self.dataset.payload.get("cards_by_author", {})
        if spec.get("note_ids") is not None:
            note_ids = list(spec["note_ids"])
        elif author_ref in catalog:
            note_ids = list(catalog[author_ref])
        else:
            # 数据集没有这个作者的列表 = 没采到，不能伪装成"该作者确实没有公开笔记"
            return AuthorPostsResult(
                state=ResultState.unsupported,
                context=context,
                session=session,
                items=[],
                error=unsupported_error(
                    Capability.author_posts,
                    self.name,
                    f"fixture 数据集未登记作者 {author_ref} 的公开笔记列表",
                ),
                warnings=warnings or ["fixture：该作者未登记公开笔记"],
            )
        return AuthorPostsResult(
            state=state,
            context=context,
            session=session,
            items=[
                self._card(note_id, index + 1, context.keyword)
                for index, note_id in enumerate(note_ids[:limit])
            ],
            warnings=warnings
            or (["fixture：该作者公开笔记列表为空（数据集显式声明）"] if not note_ids else []),
        )


__all__ = ["FixtureDataset", "FixtureSearchSource"]
