"""版本化向量缓存：键含内容哈希/模型版本/预处理版本/任务/指令/归一化与来源标记。

完整性规则（DEVELOPMENT_PLAN.md:209）：
- 任一版本字段不同 => 键不同 => miss，绝不返回近似匹配；
- 写入前校验维度一致与全部有限值（NaN/Inf 拒绝）；
- synthetic 与 real 记录分离存储于键内，require_real=True 时拒绝合成记录，
  合成向量无法污染生产检索；
- 持久化用标准库 sqlite3 单表文件，不触发任何生产迁移。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = "p2-vector-cache-v1"

# 键的构造与检索只依赖确定性输入；instruction 以哈希入键，避免长文本进主键。
KEY_FIELDS = (
    "content_sha256",
    "model_name",
    "model_version",
    "preprocess_version",
    "task_type",
    "instruction_sha256",
    "normalize",
    "provenance",
)


class CacheIntegrityError(ValueError):
    pass


def cache_key(
    *,
    content: str,
    model_name: str,
    model_version: str,
    preprocess_version: str,
    task_type: str,
    instruction: str,
    normalize: bool,
    synthetic: bool,
    provider: str | None = None,
    endpoint: str | None = None,
    api_contract_revision: str | None = None,
    dimensions: int | None = None,
) -> str:
    """构造缓存身份键。

    后四个字段为真实 API provider 的扩展身份（provider/官方endpoint/
    API contract revision/维度）；省略时与既有合成 stub 键完全一致，
    因此身份不同 => 键不同，不复用近似匹配。
    """
    payload = {
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "model_name": model_name,
        "model_version": model_version,
        "preprocess_version": preprocess_version,
        "task_type": task_type,
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "normalize": bool(normalize),
        "provenance": "synthetic" if synthetic else "real",
        "schema": CACHE_SCHEMA_VERSION,
    }
    if any(payload[field] in (None, "") for field in KEY_FIELDS):
        raise CacheIntegrityError("缓存键的每个版本字段都必须显式提供")
    if provider is not None:
        payload["provider"] = provider
    if endpoint is not None:
        payload["endpoint"] = endpoint
    if api_contract_revision is not None:
        payload["api_contract_revision"] = api_contract_revision
    if dimensions is not None:
        payload["dimensions"] = int(dimensions)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_vector(vector: list[float], expected_dim: int) -> None:
    if len(vector) != expected_dim:
        raise CacheIntegrityError(f"维度不一致：期望 {expected_dim}，实际 {len(vector)}")
    for value in vector:
        if not isinstance(value, (int, float)):
            raise CacheIntegrityError("向量元素必须是数值")
        if not math.isfinite(value):
            raise CacheIntegrityError("向量含 NaN/Inf，拒绝写入缓存")


class VectorCache:
    """最小持久本地缓存。记录带来源标记；生产读取可要求 real。"""

    def __init__(self, path: Path | None = None, expected_dim: int = 16) -> None:
        self.expected_dim = expected_dim
        self.path = Path(path) if path is not None else None
        self._mem: dict[str, dict[str, Any]] = {}
        self._conn: sqlite3.Connection | None = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
            # 独立缓存文件、单表 IF NOT EXISTS：不属于生产库迁移。
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vector_cache ("
                " key TEXT PRIMARY KEY, provenance TEXT NOT NULL,"
                " model_name TEXT, model_version TEXT, dim INTEGER,"
                " payload TEXT NOT NULL, schema_version TEXT NOT NULL)"
            )
            self._conn.commit()
            for row in self._conn.execute("SELECT key, payload FROM vector_cache"):
                self._mem[row[0]] = json.loads(row[1])

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "VectorCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def put(self, key: str, vector: list[float], *, model_name: str, model_version: str, synthetic: bool) -> None:
        _validate_vector(vector, self.expected_dim)
        record = {
            "key": key,
            "vector": vector,
            "model_name": model_name,
            "model_version": model_version,
            "dim": len(vector),
            "provenance": "synthetic" if synthetic else "real",
            "schema_version": CACHE_SCHEMA_VERSION,
        }
        if self._conn is not None:
            # upsert：同一 key 重写不报错，持久化覆盖内存语义。
            self._conn.execute(
                "INSERT INTO vector_cache(key, provenance, model_name, model_version, dim, payload, schema_version)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (key, record["provenance"], model_name, model_version, record["dim"],
                 json.dumps(record, ensure_ascii=False), CACHE_SCHEMA_VERSION),
            )
            self._conn.commit()
        self._mem[key] = record

    def get(self, key: str, *, require_real: bool = False) -> list[float] | None:
        record = self._mem.get(key)
        if record is None:
            return None
        if record.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise CacheIntegrityError("缓存记录版本不一致，拒绝使用")
        if require_real and record["provenance"] != "real":
            raise CacheIntegrityError(
                f"key {key[:12]}… 是 {record['provenance']} 记录：生产检索拒绝合成向量"
            )
        vector = record["vector"]
        _validate_vector(vector, record["dim"])
        return list(vector)

    def stats(self) -> dict[str, int]:
        synthetic = sum(1 for r in self._mem.values() if r["provenance"] == "synthetic")
        return {"total": len(self._mem), "synthetic": synthetic, "real": len(self._mem) - synthetic}


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheIntegrityError",
    "VectorCache",
    "cache_key",
]
