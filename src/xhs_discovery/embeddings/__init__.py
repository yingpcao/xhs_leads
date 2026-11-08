"""P2 离线 Embedding 基础设施：可替换 provider 协议、版本化缓存、确定性检索。

生产默认（embedding_enabled=False）下不会构造 provider，也不会导入本包以外的
重型依赖；合成 stub 记录带来源标记，无法进入生产聚类/评分。
真实 API provider（dashscope 子模块）仅供 benchmark 显式 opt-in 构造，
不接入 build_provider，普通生产配置不会自动联网。
"""

from xhs_discovery.embeddings.provider import (
    EmbeddingProvider,
    ProviderDisabledError,
    RealProviderNotAvailableError,
    SyntheticStubProvider,
    build_provider,
)
from xhs_discovery.embeddings.cache import CACHE_SCHEMA_VERSION, CacheIntegrityError, VectorCache, cache_key
from xhs_discovery.embeddings.retrieval import cosine, cosine_top_k

__all__ = [
    "EmbeddingProvider",
    "ProviderDisabledError",
    "RealProviderNotAvailableError",
    "SyntheticStubProvider",
    "build_provider",
    "CACHE_SCHEMA_VERSION",
    "CacheIntegrityError",
    "VectorCache",
    "cache_key",
    "cosine",
    "cosine_top_k",
]
