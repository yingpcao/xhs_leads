from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xhs_discovery.models.enums import OperationType, SourceField
from xhs_discovery.models.errors import ErrorInfo
from xhs_discovery.models.common import SessionRef
from xhs_discovery.models.evidence import Evidence, EvidenceProvenance
from xhs_discovery.models.run import RunIdentity
from xhs_discovery.models.source import (
    AuthorRef,
    KeywordText,
    OperationContext,
    PostCard,
    SuggestionItem,
)
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage import db as storage_db
from xhs_discovery.storage.repository import Repository

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
CONFIG_HASH = "a" * 64


def build_run_identity(run_id: str = "run-a", **overrides) -> RunIdentity:
    payload = dict(
        run_id=run_id,
        seed=KeywordText(raw="上海中考", normalized="上海中考"),
        mode="fixture",
        config_hash=CONFIG_HASH,
        random_seed=20261024,
        created_at=T0,
        synthetic_fixture=True,
    )
    payload.update(overrides)
    return RunIdentity(**payload)


@dataclass(frozen=True)
class StorageRuntime:
    """一个测试一个文件库：产物目录与数据库同在 tmp 下，跨连接可见才能测恢复。"""

    paths: RuntimePaths
    conn: sqlite3.Connection
    repo: Repository
    run_id: str


@pytest.fixture
def storage(tmp_path: Path):
    paths = RuntimePaths(tmp_path / "data")
    paths.runs_dir.mkdir(parents=True, exist_ok=True)
    conn = storage_db.connect(paths.db_path)
    repo = Repository(conn)
    identity = build_run_identity("run-a")
    repo.create_run(identity, config_json='{"budget": {}}')
    try:
        yield StorageRuntime(paths=paths, conn=conn, repo=repo, run_id=identity.run_id)
    finally:
        conn.close()


@pytest.fixture
def make_dir_link():
    """尽力创建目录链接，返回所用机制（symlink/junction）；失败返回 None 由用例 skip。"""

    def make(target: Path, link: Path) -> str | None:
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
            return "symlink"
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and link.is_dir():
                return "junction"
        return None

    return make


@pytest.fixture
def ctx_factory():
    def make(operation_type: OperationType = OperationType.suggest, **overrides) -> OperationContext:
        payload = dict(
            run_id="run-a",
            keyword_id=1,
            keyword=KeywordText(raw="上海中考", normalized="上海中考"),
            operation_type=operation_type,
            revisit_seq=0,
            attempt_no=1,
        )
        payload.update(overrides)
        return OperationContext(**payload)

    return make


@pytest.fixture
def session_factory():
    def make(**overrides) -> SessionRef:
        payload = dict(
            session_id="sess-1",
            collected_at=T0,
            source_name="fixture",
            source_version="0.0.1",
            synthetic=True,
        )
        payload.update(overrides)
        return SessionRef(**payload)

    return make


@pytest.fixture
def suggestion_factory():
    def make(*texts: str) -> list[SuggestionItem]:
        return [SuggestionItem(text=t, rank=i + 1) for i, t in enumerate(texts)]

    return make


@pytest.fixture
def card_factory():
    def make(note_id: str, rank: int, title: str = "志愿到底怎么排", **overrides) -> PostCard:
        payload = dict(
            note_id=note_id,
            access_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            title=title,
            author=AuthorRef(kind="public_ref", public_ref="user-1"),
            published_at_text="2026-09-01",
            likes_text="128",
            result_rank=rank,
            search_keyword=KeywordText(raw="上海中考 志愿", normalized="上海中考 志愿"),
        )
        payload.update(overrides)
        return PostCard(**payload)

    return make


@pytest.fixture
def evidence_factory(ctx_factory):
    def make(
        container: str,
        quote: str,
        *,
        source_field: SourceField = SourceField.title,
        note_id: str | None = "note-1",
        rank: int | None = None,
        run_id: str = "run-a",
        synthetic: bool = True,
        occurrence: int = 0,
    ) -> Evidence:
        provenance = EvidenceProvenance(
            run_id=run_id,
            operation=ctx_factory(),
            session_id="sess-1",
            keyword=KeywordText(raw="上海中考", normalized="上海中考"),
            note_id=note_id,
            suggestion_rank=rank,
        )
        return Evidence.locate(
            container_text=container,
            quote=quote,
            source_field=source_field,
            provenance=provenance,
            extractor_version="p0-rule-extract.1",
            analysis_version="p0-rule-baseline.1",
            synthetic=synthetic,
            occurrence=occurrence,
        )

    return make


@pytest.fixture
def auth_error() -> ErrorInfo:
    return ErrorInfo(
        code="CLI_EXIT_3",
        message="需要登录",
        category="auth",
        retryable=False,
        human_action_required=True,
        exit_code=3,
    )
