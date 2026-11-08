"""显式 opt-in 的 DashScope（阿里云百炼·北京）OpenAI 兼容 embeddings provider。

P2-C-DASHSCOPE-EMBEDDING-r1 边界：
- 仅供 API benchmark 专用入口显式构造；build_provider/产品配置照常拒绝，
  普通生产配置不会自动联网，也不加载本模块以外的重型依赖；
- 只用标准库 urllib；不安装新依赖、不下载本地权重；
- 密钥只在调用函数内部 ``os.environ.get("DASHSCOPE_API_KEY")`` 读取，
  不打印、不序列化、不写文件；错误只带安全分类与 HTTP 状态；
- 单一官方北京 endpoint 白名单（https://dashscope.aliyuncs.com/compatible-mode/v1），
  拒绝自定义 host、拒绝 redirect（防 Authorization 外泄）；
- HTTP/JSON/超时/认证/限流一律 fail closed：不自动重试、不降级合成；
- OpenAI 兼容 embeddings 请求体不支持 task_type/instruction 语义：
  未登记的 task_type 与非空 instruction 明确拒绝（不静默忽略）；
  normalize=True 是文档化的确定后处理（对返回向量做稳定缩放 L2 归一化，
  输出再次校验 finite 与非零/单位范数，溢出/异常一律 fail closed）；
  上述语义全部纳入缓存身份（provider/endpoint/API contract revision/
  维度/预处理/指令/归一化/来源，见 cache_key 扩展字段）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

PROVIDER_NAME = "dashscope-openai-compat"
API_CONTRACT_REVISION = "p2-dashscope-embeddings-v1"
OFFICIAL_BEIJING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDINGS_PATH = "/embeddings"
# 精确允许的完整请求路径（r2 收紧：不接受 startswith/endswith 的额外路径变体）。
OFFICIAL_EMBEDDINGS_PATH = urllib.parse.urlsplit(OFFICIAL_BEIJING_BASE_URL).path + EMBEDDINGS_PATH
_ALLOWED_HOST = "dashscope.aliyuncs.com"

MODEL_NAME = "qwen3.7-text-embedding"
# 官方模型没有公布不可变权重版本号：如实标记 unknown/mutable，不捏造。
MODEL_WEIGHT_VERSION = "unknown-mutable"
PREPROCESS_VERSION = "p2-dashscope-v1"

DIMENSIONS_ALLOWED = (256, 512, 768, 1024, 1536, 2048, 2560)
MAX_INPUTS_PER_REQUEST = 20
KEY_ENV_VAR = "DASHSCOPE_API_KEY"

# 仅登记 benchmark 专用语义；其余 task_type 明确拒绝（API 无任务定制参数）。
DOCUMENTED_TASK_TYPES = ("p2-smoke-benchmark",)

# 公布的同步调用标价（2026-10-29 查阅官方文档），仅作估计记录，不冒充账单。
LISTED_SYNC_PRICE_CNY_PER_1K_TOKENS = 0.0005

# r2：服务返回的 request_id 只有匹配安全字符集且长度受限才作为元数据输出。
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")
# usage token 数的量级上限：超出即视为不可信元数据，排除（也避免费用换算溢出）。
_MAX_SAFE_TOKENS = 10**12


class DashScopeProviderError(RuntimeError):
    """fail-closed 安全错误：只携带分类与 HTTP 状态，绝不含密钥/原始 body/header。"""

    def __init__(self, category: str, *, http_status: int | None = None) -> None:
        self.category = category
        self.http_status = http_status
        status = f" (http_status={http_status})" if http_status is not None else ""
        super().__init__(f"dashscope provider failed closed: {category}{status}")


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


# transport 契约：(url, headers, payload_bytes, timeout_seconds) -> TransportResponse
Transport = Callable[[str, dict[str, str], bytes, float], TransportResponse]


def validate_official_endpoint(url: str) -> str:
    """只允许 HTTPS 官方北京域名的 compatible-mode embeddings 精确路径。"""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _ALLOWED_HOST
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DashScopeProviderError("insecure_or_untrusted_endpoint")
    if parsed.path != OFFICIAL_EMBEDDINGS_PATH:
        raise DashScopeProviderError("insecure_or_untrusted_endpoint")
    return url


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """永不跟随 redirect：避免 Authorization 头被发往其它 URL。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(newurl, code, "redirect refused", {}, fp)  # type: ignore[arg-type]


def default_transport(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> TransportResponse:
    validate_official_endpoint(url)
    request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read()
    except urllib.error.HTTPError as exc:
        # 只保留状态码；不读取、不转发服务原始 body/header。
        raise DashScopeProviderError(_http_category(exc.code), http_status=exc.code) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or reason == "timed out":
            raise DashScopeProviderError("timeout") from None
        raise DashScopeProviderError("transport_error") from None
    except (TimeoutError, OSError):
        raise DashScopeProviderError("timeout") from None
    if status != 200:
        raise DashScopeProviderError(_http_category(status), http_status=status)
    return TransportResponse(status=status, body=body)


def _http_category(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "timeout"
    return "http_error"


def _check_semantics(task_type: str, instruction: str) -> None:
    if task_type not in DOCUMENTED_TASK_TYPES:
        raise DashScopeProviderError("unsupported_task_type")
    if instruction != "":
        # API 契约没有 instruction 参数：明确拒绝，不做静默忽略或私自拼接预处理。
        raise DashScopeProviderError("unsupported_instruction")


def _to_finite_float(value: object) -> float:
    """把响应中的单个数值安全转成有限 float；入口拒绝 bool/字符串/complex 等
    非数值，超大整数/NaN/inf 一律安全拒绝；任何原生异常都转成安全分类，不带出原文。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DashScopeProviderError("non_finite_vector")
    try:
        result = float(value)
        finite = math.isfinite(result)
    except (OverflowError, ValueError, TypeError):
        raise DashScopeProviderError("non_finite_vector") from None
    if not finite:
        raise DashScopeProviderError("non_finite_vector")
    return result


def _safe_token_count(value: object) -> int | None:
    """usage 字段的单一安全规则：仅接受非 bool、非负、量级受限的 int；smoke/receipt 共用。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_SAFE_TOKENS:
        return None
    return value


def _is_zero_norm(values: list[float]) -> bool:
    # 缩放判据：范数为零当且仅当所有分量为零；避免 sum(v*v) 下溢误判。
    return max((abs(v) for v in values), default=0.0) == 0.0


def _stable_l2_norm(values: list[float]) -> float:
    """按最大绝对值缩放计算 L2 范数；可能返回 inf（真实范数超出 float 范围），
    但绝不由朴素 sum(v*v) 溢出产生假 inf/假 0；不泄漏原生异常。"""
    try:
        scale = max((abs(v) for v in values), default=0.0)
        if scale == 0.0:
            return 0.0
        if not math.isfinite(scale):
            return float("inf")
        scaled = [v / scale for v in values]
        return scale * math.sqrt(sum(s * s for s in scaled))
    except (OverflowError, ValueError, TypeError, ZeroDivisionError):
        raise DashScopeProviderError("vector_normalization_failed") from None


def _l2_normalize(vector: list[float]) -> list[float]:
    """稳定 L2 归一化：先按最大绝对值缩放再归一，避免 sqrt(sum(v*v)) 溢出为 inf
    而静默输出全 0；输出仍再次校验 finite 与非零/单位范数；不泄漏原生异常。"""
    try:
        scale = max(abs(v) for v in vector)
        if scale == 0.0:
            raise DashScopeProviderError("zero_norm_vector")
        if not math.isfinite(scale):
            raise DashScopeProviderError("non_finite_vector")
        scaled = [v / scale for v in vector]
        scaled_norm = math.sqrt(sum(s * s for s in scaled))
        if not math.isfinite(scaled_norm) or scaled_norm == 0.0:
            raise DashScopeProviderError("vector_normalization_failed")
        result = [s / scaled_norm for s in scaled]
        if not all(math.isfinite(v) for v in result) or _is_zero_norm(result):
            raise DashScopeProviderError("vector_normalization_failed")
        out_norm = _stable_l2_norm(result)
        if not math.isfinite(out_norm) or not math.isclose(out_norm, 1.0, rel_tol=1e-9):
            raise DashScopeProviderError("vector_normalization_failed")
        return result
    except DashScopeProviderError:
        raise
    except (OverflowError, ValueError, TypeError, ZeroDivisionError):
        raise DashScopeProviderError("vector_normalization_failed") from None


def _validate_item(item: Any, expected_dim: int) -> tuple[int, list[float]]:
    if not isinstance(item, dict):
        raise DashScopeProviderError("bad_response_shape")
    index = item.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise DashScopeProviderError("bad_response_index")
    raw = item.get("embedding")
    if not isinstance(raw, list) or len(raw) != expected_dim:
        raise DashScopeProviderError("bad_response_dimension")
    values = [_to_finite_float(v) for v in raw]
    if _is_zero_norm(values):
        raise DashScopeProviderError("zero_norm_vector")
    return index, values


def parse_and_validate_response(
    body: bytes, *, expected_model: str, expected_count: int, expected_dim: int
) -> tuple[list[list[float]], dict[str, Any]]:
    """校验模型名、数量、index 唯一连续（乱序可重排）、维度、finite、非零范数。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DashScopeProviderError("bad_response_json") from None
    if not isinstance(payload, dict):
        raise DashScopeProviderError("bad_response_shape")
    if payload.get("model") != expected_model:
        raise DashScopeProviderError("model_mismatch")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise DashScopeProviderError("bad_response_count")
    pairs = [_validate_item(item, expected_dim) for item in data]
    indices = sorted(index for index, _ in pairs)
    if indices != list(range(expected_count)):
        raise DashScopeProviderError("bad_response_index")
    vectors = [vector for _, vector in sorted(pairs, key=lambda pair: pair[0])]
    usage_raw = payload.get("usage")
    usage: dict[str, Any] = {}
    if isinstance(usage_raw, dict):
        for name in ("prompt_tokens", "total_tokens"):
            safe = _safe_token_count(usage_raw.get(name))
            if safe is not None:
                usage[name] = safe
    request_id_raw = payload.get("id") or payload.get("request_id")
    request_id = request_id_raw if isinstance(request_id_raw, str) and _SAFE_REQUEST_ID.match(request_id_raw) else None
    receipt = {
        "request_id": request_id,
        "returned_model": payload.get("model"),
        "usage": usage,
    }
    return vectors, receipt


def estimate_cost_cny(usage: dict[str, Any]) -> float | None:
    """按公布标价估算费用；仅估计，不冒充账单。复用 _safe_token_count 单一安全规则。"""
    tokens = _safe_token_count(usage.get("total_tokens"))
    if tokens is None:
        tokens = _safe_token_count(usage.get("prompt_tokens"))
    if tokens is None:
        return None
    return tokens / 1000.0 * LISTED_SYNC_PRICE_CNY_PER_1K_TOKENS


@dataclass
class EmbeddingReceipt:
    """成功调用的安全记录：不含密钥、header、请求对象或原始错误 body。"""

    returned_model: str | None
    dimension: int
    count: int
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    latency_seconds: float | None = None
    estimated_cost_cny: float | None = None
    vector_sha256: list[str] = field(default_factory=list)


def _vector_hash(vector: list[float]) -> str:
    canonical = json.dumps(vector, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DashScopeEmbeddingProvider:
    """协议兼容的真实 API provider；仅 benchmark 显式构造，永不进入 build_provider。"""

    synthetic = False
    model_name = MODEL_NAME
    model_version = MODEL_WEIGHT_VERSION
    preprocess_version = PREPROCESS_VERSION
    provider_name = PROVIDER_NAME
    api_contract_revision = API_CONTRACT_REVISION

    def __init__(
        self,
        *,
        dimension: int = 1024,
        timeout_seconds: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        if dimension not in DIMENSIONS_ALLOWED:
            raise DashScopeProviderError("unsupported_dimension")
        if not 0 < timeout_seconds <= 30.0:
            raise DashScopeProviderError("invalid_timeout")
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self.endpoint = OFFICIAL_BEIJING_BASE_URL + EMBEDDINGS_PATH
        validate_official_endpoint(self.endpoint)
        self._transport: Transport = transport if transport is not None else default_transport
        self.call_count = 0
        self.last_receipt: EmbeddingReceipt | None = None

    def embed(self, texts: list[str], *, task_type: str, instruction: str, normalize: bool) -> list[list[float]]:
        vectors, _receipt = self.embed_detailed(texts, task_type=task_type, instruction=instruction, normalize=normalize)
        return vectors

    def embed_detailed(
        self, texts: list[str], *, task_type: str, instruction: str, normalize: bool
    ) -> tuple[list[list[float]], EmbeddingReceipt]:
        """至多一次 HTTP 调用；失败即抛出（不重试、不降级）。"""
        if not texts or len(texts) > MAX_INPUTS_PER_REQUEST:
            raise DashScopeProviderError("batch_size_rejected")
        if not all(isinstance(t, str) and t for t in texts):
            raise DashScopeProviderError("invalid_input_text")
        _check_semantics(task_type, instruction)
        api_key = os.environ.get(KEY_ENV_VAR)
        if not api_key:
            raise DashScopeProviderError("missing_api_key")
        payload = json.dumps(
            {"model": self.model_name, "input": list(texts), "encoding_format": "float", "dimensions": self.dimension},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.call_count += 1
        started = time.monotonic()
        try:
            response = self._transport(self.endpoint, headers, payload, self.timeout_seconds)
        except DashScopeProviderError:
            raise
        except Exception:
            # 自定义 transport 的意外异常同样 fail closed，且不带出原始异常内容。
            raise DashScopeProviderError("transport_error") from None
        latency = time.monotonic() - started
        if response.status != 200:
            raise DashScopeProviderError(_http_category(response.status), http_status=response.status)
        vectors, meta = parse_and_validate_response(
            response.body, expected_model=self.model_name, expected_count=len(texts), expected_dim=self.dimension
        )
        if normalize:
            vectors = [_l2_normalize(v) for v in vectors]
        receipt = EmbeddingReceipt(
            returned_model=meta["returned_model"],
            dimension=self.dimension,
            count=len(vectors),
            usage=meta["usage"],
            request_id=meta["request_id"],
            latency_seconds=round(latency, 3),
            estimated_cost_cny=estimate_cost_cny(meta["usage"]),
            vector_sha256=[_vector_hash(v) for v in vectors],
        )
        self.last_receipt = receipt
        return vectors, receipt

    def cache_identity(self) -> dict[str, Any]:
        """并入 cache_key 的扩展身份字段（provider/endpoint/API revision/维度）。"""
        return {
            "provider": self.provider_name,
            "endpoint": self.endpoint,
            "api_contract_revision": self.api_contract_revision,
            "dimensions": self.dimension,
        }

    def security_check(self) -> dict[str, bool]:
        """安全预检：只输出密钥是否存在，不回显长度/内容，不发任何请求。"""
        return {"api_key_present": bool(os.environ.get(KEY_ENV_VAR))}


__all__ = [
    "API_CONTRACT_REVISION",
    "DashScopeEmbeddingProvider",
    "DashScopeProviderError",
    "DOCUMENTED_TASK_TYPES",
    "EmbeddingReceipt",
    "KEY_ENV_VAR",
    "LISTED_SYNC_PRICE_CNY_PER_1K_TOKENS",
    "MAX_INPUTS_PER_REQUEST",
    "MODEL_NAME",
    "MODEL_WEIGHT_VERSION",
    "OFFICIAL_BEIJING_BASE_URL",
    "PREPROCESS_VERSION",
    "PROVIDER_NAME",
    "TransportResponse",
    "default_transport",
    "estimate_cost_cny",
    "parse_and_validate_response",
    "validate_official_endpoint",
]
