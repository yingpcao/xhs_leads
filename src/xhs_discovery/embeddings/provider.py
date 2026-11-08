"""可替换 Embedding 提供者协议 + 仅测试用合成 stub。

边界（PROJECT_CHECKLIST P2-04、DEVELOPMENT_PLAN.md:208-209）：
- 模块导入时不加载任何模型；真实模型提供者尚未接入（需单独授权）；
- 配置关闭（embedding_enabled=False，即当前生产默认）时不会构造任何 provider；
- 合成 stub 输出显式 synthetic 记录，绝不能进入生产聚类/评分；
- 不访问网络、不下载权重。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

PROVIDER_PROTOCOL_VERSION = "p2-provider-v1"


@runtime_checkable
class EmbeddingProvider(Protocol):
    """提供者必须自报全部版本信息：缓存键与完整性校验依赖它们。"""

    model_name: str
    model_version: str
    preprocess_version: str
    dimension: int
    synthetic: bool

    def embed(self, texts: list[str], *, task_type: str, instruction: str, normalize: bool) -> list[list[float]]:
        ...


class SyntheticStubProvider:
    """显式合成 stub：仅供单元测试产生确定性向量，禁止用于生产。"""

    def __init__(self, dimension: int = 16) -> None:
        if not 1 <= dimension <= 1024:
            raise ValueError("stub 维度必须在 1..1024")
        self.model_name = "synthetic-stub"
        self.model_version = "stub-v1"
        self.preprocess_version = "none"
        self.dimension = dimension
        self.synthetic = True
        self.call_count = 0

    def embed(self, texts: list[str], *, task_type: str, instruction: str, normalize: bool) -> list[list[float]]:
        self.call_count += 1
        return [self._vector(t, task_type, instruction, normalize) for t in texts]

    def _vector(self, text: str, task_type: str, instruction: str, normalize: bool) -> list[float]:
        seed = f"{self.model_version}|{task_type}|{instruction}|{int(normalize)}|{text}"
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        # 从哈希确定性展开 dimension 个 [-1,1) 的值；同一输入永远同一向量。
        values: list[float] = []
        for i in range(self.dimension):
            byte = digest[i % len(digest)] ^ ((i // len(digest)) * 7 & 0xFF)
            values.append(byte / 128.0 - 1.0)
        if normalize:
            norm = math.sqrt(sum(v * v for v in values))
            if norm > 0:
                values = [v / norm for v in values]
        return values


class ProviderDisabledError(RuntimeError):
    """模型未启用：不构造 provider、不导入重型依赖。"""


class RealProviderNotAvailableError(RuntimeError):
    """P2 离线 harness 阶段没有可用的真实模型提供者，需单独授权后接入。"""


def build_provider(
    *,
    embedding_enabled: bool,
    embedding_model: str | None,
    allow_synthetic_stub: bool = False,
) -> EmbeddingProvider:
    """按配置构造 provider。当前只存在合成 stub；真实模型一律拒绝。

    - 未启用 -> ProviderDisabledError（调用方因此不会加载模型）；
    - 请求非 stub 模型 -> RealProviderNotAvailableError，不做任何下载/导入。
    """
    if not embedding_enabled:
        raise ProviderDisabledError("models.embedding_enabled=false：不加载模型、不构造 provider")
    if embedding_model == "synthetic-stub":
        if not allow_synthetic_stub:
            raise RealProviderNotAvailableError(
                "synthetic-stub 只允许在单元测试显式 allow_synthetic_stub=True 时使用"
            )
        return SyntheticStubProvider()
    raise RealProviderNotAvailableError(
        f"模型提供者 {embedding_model!r} 尚未在 P2 离线 harness 中接入；不下载、不加载"
    )


__all__ = [
    "PROVIDER_PROTOCOL_VERSION",
    "EmbeddingProvider",
    "SyntheticStubProvider",
    "ProviderDisabledError",
    "RealProviderNotAvailableError",
    "build_provider",
]
