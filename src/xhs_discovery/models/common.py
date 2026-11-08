"""共享基础类型：内容版本指纹、原始产物引用、时间戳。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ArtifactKind = Literal["stdout_json", "stderr_log", "raw_response", "export"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    """契约基类：禁止未知字段，避免把上游漂移静默吞掉。"""

    model_config = ConfigDict(extra="forbid")


class RawArtifactRef(StrictModel):
    """原始响应先原子落盘，再在事务里引用；重启后可补导入。"""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    kind: ArtifactKind

    @field_validator("path")
    @classmethod
    def _relative_only(cls, value: str) -> str:
        from xhs_discovery.relpath import split_relative

        split_relative(value)
        return value


class SessionRef(StrictModel):
    """一次采集会话；同一关键词在不同会话可产生不同观察。"""

    session_id: str = Field(min_length=1)
    collected_at: datetime
    source_name: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    synthetic: bool = False

    @field_validator("collected_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("collected_at 必须带时区")
        return value


__all__ = ["ArtifactKind", "StrictModel", "RawArtifactRef", "SessionRef", "utc_now", "sha256_text"]
