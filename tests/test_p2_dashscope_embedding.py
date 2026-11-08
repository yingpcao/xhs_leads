"""DashScope 真实 API provider 离线测试：注入 fake transport，零真实网络、零真实密钥。

覆盖：missing key / disabled 默认零网络 / endpoint 白名单 / redirect 拒绝 /
HTTP 错误分类 / 乱序与重复 index / NaN / 维度与数量 / secret 脱敏 /
缓存身份隔离 / task_type·instruction·normalize 语义 / batch 上限 / 不重试。
"""

from __future__ import annotations

import json
import math

import pytest

from xhs_discovery.embeddings.cache import CacheIntegrityError, VectorCache, cache_key
from xhs_discovery.embeddings.dashscope import (
    DOCUMENTED_TASK_TYPES,
    MODEL_NAME,
    DashScopeEmbeddingProvider,
    DashScopeProviderError,
    TransportResponse,
    default_transport,
    estimate_cost_cny,
    validate_official_endpoint,
)
from xhs_discovery.embeddings.provider import (
    RealProviderNotAvailableError,
    build_provider,
)

TASK = DOCUMENTED_TASK_TYPES[0]
KEY = "sk-SECRET-DO-NOT-LEAK-1234567890"


def _ok_body(vectors: list[list[float]], *, model: str = MODEL_NAME) -> bytes:
    data = [
        {"index": i, "object": "embedding", "embedding": vec}
        for i, vec in enumerate(vectors)
    ]
    return json.dumps(
        {"object": "list", "model": model, "data": data,
         "usage": {"prompt_tokens": 42, "total_tokens": 42}},
        ensure_ascii=False,
    ).encode("utf-8")


class FakeTransport:
    def __init__(self, responses):
        # responses: 列表或单值；每项为 TransportResponse 或异常
        self._responses = list(responses) if isinstance(responses, list) else [responses]
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout})
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", KEY)


def _provider(transport, **kw):
    return DashScopeEmbeddingProvider(transport=transport, **kw)


def _embed(provider, transport_texts=("构造文本一", "构造文本二")):
    return provider.embed(list(transport_texts), task_type=TASK, instruction="", normalize=True)


class TestDisabledAndDefaults:
    def test_build_provider_never_returns_dashscope(self):
        with pytest.raises(RealProviderNotAvailableError):
            build_provider(embedding_enabled=True, embedding_model=MODEL_NAME)

    def test_disabled_still_raises(self):
        from xhs_discovery.embeddings.provider import ProviderDisabledError

        with pytest.raises(ProviderDisabledError):
            build_provider(embedding_enabled=False, embedding_model=None)

    def test_missing_key_zero_network(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        transport = FakeTransport(TransportResponse(200, b"{}"))
        provider = _provider(transport)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(provider)
        assert exc.value.category == "missing_api_key"
        assert transport.calls == []

    def test_security_check_only_reports_presence(self, with_key):
        provider = _provider(FakeTransport(TransportResponse(200, b"{}")))
        result = provider.security_check()
        assert result == {"api_key_present": True}
        assert KEY not in json.dumps(result)


class TestEndpointAndRedirect:
    @pytest.mark.parametrize(
        "url",
        [
            "http://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            "https://evil.example.com/compatible-mode/v1/embeddings",
            "https://dashscope.aliyuncs.com.evil.example/v1/embeddings",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "https://dashscope.aliyuncs.com:8443/compatible-mode/v1/embeddings",
            "https://user@dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings?next=evil",
        ],
    )
    def test_untrusted_or_insecure_endpoints_rejected(self, url):
        with pytest.raises(DashScopeProviderError) as exc:
            validate_official_endpoint(url)
        assert exc.value.category == "insecure_or_untrusted_endpoint"

    def test_official_beijing_endpoint_accepted(self):
        assert validate_official_endpoint(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        )

    def test_default_transport_rejects_custom_host_before_network(self, with_key, monkeypatch):
        called = []

        def spy_open(*a, **kw):
            called.append(1)
            raise AssertionError("must not open")

        monkeypatch.setattr("urllib.request.OpenerDirector.open", spy_open)
        with pytest.raises(DashScopeProviderError):
            default_transport("https://evil.example/compatible-mode/v1/embeddings", {}, b"{}", 5.0)
        assert called == []

    def test_redirect_never_followed(self, monkeypatch):
        import urllib.error
        import urllib.request

        handler = default_transport.__globals__["_RefuseRedirect"]()
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(None, None, 302, "Found", None, "https://other.example/x")


class TestRequestConstruction:
    def test_payload_and_headers(self, with_key):
        transport = FakeTransport(TransportResponse(200, _ok_body([[1.0] + [0.0] * 1023] * 2)))
        provider = _provider(transport, dimension=1024)
        _embed(provider)
        call = transport.calls[0]
        assert call["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        assert call["timeout"] == 30.0
        assert call["headers"]["Authorization"] == f"Bearer {KEY}"
        payload = json.loads(call["payload"].decode("utf-8"))
        assert payload["model"] == MODEL_NAME
        assert payload["encoding_format"] == "float"
        assert payload["dimensions"] == 1024
        assert payload["input"] == ["构造文本一", "构造文本二"]

    def test_batch_over_20_rejected_before_network(self, with_key):
        transport = FakeTransport(TransportResponse(200, b"{}"))
        provider = _provider(transport)
        with pytest.raises(DashScopeProviderError) as exc:
            provider.embed([f"文本{i}" for i in range(21)], task_type=TASK, instruction="", normalize=True)
        assert exc.value.category == "batch_size_rejected"
        assert transport.calls == []

    def test_semantics_rejected_not_silently_ignored(self, with_key):
        provider = _provider(FakeTransport(TransportResponse(200, b"{}")))
        with pytest.raises(DashScopeProviderError) as exc:
            provider.embed(["t"], task_type="retrieval-query", instruction="", normalize=True)
        assert exc.value.category == "unsupported_task_type"
        with pytest.raises(DashScopeProviderError) as exc:
            provider.embed(["t"], task_type=TASK, instruction="代表这个需求", normalize=True)
        assert exc.value.category == "unsupported_instruction"

    def test_dimension_allowlist(self):
        for dim in (256, 512, 768, 1024, 1536, 2048, 2560):
            DashScopeEmbeddingProvider(dimension=dim, transport=FakeTransport(TransportResponse(200, b"{}")))
        with pytest.raises(DashScopeProviderError):
            DashScopeEmbeddingProvider(dimension=1023, transport=FakeTransport(TransportResponse(200, b"{}")))


class TestResponseValidation:
    DIM = 256

    def _vec(self, one_at: int) -> list[float]:
        return [1.0 if i == one_at else 0.0 for i in range(self.DIM)]

    def _embed_with(self, body: bytes, count=2, dim=None):
        transport = FakeTransport(TransportResponse(200, body))
        provider = _provider(transport, dimension=dim or self.DIM)
        return provider.embed(["甲", "乙"][:count], task_type=TASK, instruction="", normalize=False)

    def test_out_of_order_indices_reordered(self):
        body = json.dumps({
            "model": MODEL_NAME,
            "data": [
                {"index": 1, "embedding": self._vec(1)},
                {"index": 0, "embedding": self._vec(0)},
            ],
        }).encode()
        vectors = self._embed_with(body)
        assert vectors[0] == self._vec(0) and vectors[1] == self._vec(1)

    def test_duplicate_indices_rejected(self):
        body = json.dumps({
            "model": MODEL_NAME,
            "data": [
                {"index": 0, "embedding": self._vec(0)},
                {"index": 0, "embedding": self._vec(1)},
            ],
        }).encode()
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(body)
        assert exc.value.category == "bad_response_index"

    def test_count_mismatch_rejected(self):
        body = json.dumps({"model": MODEL_NAME, "data": [{"index": 0, "embedding": self._vec(0)}]}).encode()
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(body, count=2)
        assert exc.value.category == "bad_response_count"

    def test_wrong_dim_rejected(self):
        body = json.dumps({"model": MODEL_NAME, "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(body, count=1, dim=self.DIM)
        assert exc.value.category == "bad_response_dimension"

    def test_nan_rejected(self):
        bad = self._vec(0)
        bad[3] = float("nan")
        body = json.dumps({"model": MODEL_NAME, "data": [{"index": 0, "embedding": bad}]}).encode()
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(body, count=1)
        assert exc.value.category == "non_finite_vector"

    def test_zero_norm_rejected(self):
        body = json.dumps({"model": MODEL_NAME, "data": [
            {"index": 0, "embedding": [0.0] * self.DIM}]}).encode()
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(body, count=1)
        assert exc.value.category == "zero_norm_vector"

    def test_model_mismatch_rejected(self):
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(_ok_body([self._vec(0)], model="some-other-model"), count=1)
        assert exc.value.category == "model_mismatch"

    def test_bad_json_rejected(self):
        with pytest.raises(DashScopeProviderError) as exc:
            self._embed_with(b"not-json{{{", count=1)
        assert exc.value.category == "bad_response_json"

    def test_normalize_true_produces_unit_norm(self, with_key):
        vec = [3.0, 4.0] + [0.0] * (254)
        transport = FakeTransport(TransportResponse(200, _ok_body([vec])))
        provider = _provider(transport, dimension=256)
        out = provider.embed(["甲"], task_type=TASK, instruction="", normalize=True)[0]
        assert math.isclose(math.sqrt(sum(v * v for v in out)), 1.0, rel_tol=1e-12)
        out_raw = provider.embed(["甲"], task_type=TASK, instruction="", normalize=False)[0]
        assert out_raw == vec


class TestFailureClassificationAndRedaction:
    @pytest.mark.parametrize(
        ("status", "category"),
        [(401, "auth"), (403, "auth"), (429, "rate_limited"), (500, "http_error"), (302, "http_error")],
    )
    def test_http_statuses_classified_no_retry(self, with_key, status, category):
        transport = FakeTransport(TransportResponse(status, b"SECRET-BODY-LEAK-CHECK"))
        provider = _provider(transport)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(provider)
        assert exc.value.category == category
        assert exc.value.http_status == status
        assert provider.call_count == 1
        assert len(transport.calls) == 1  # 绝不重试
        assert "SECRET-BODY-LEAK-CHECK" not in str(exc.value)

    def test_error_and_logs_never_leak_key_or_headers(self, with_key):
        transport = FakeTransport(TransportResponse(401, b'{"error":{"message":"bad ' + KEY.encode() + b'"}}'))
        provider = _provider(transport)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(provider)
        assert KEY not in str(exc.value)
        assert KEY not in repr(exc.value)
        assert KEY not in json.dumps({"category": exc.value.category, "status": exc.value.http_status})

    def test_timeout_fail_closed_no_retry(self, with_key):
        transport = FakeTransport(TimeoutError("timed out"))
        provider = _provider(transport)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(provider)
        assert exc.value.category == "transport_error"
        assert provider.call_count == 1
        assert len(transport.calls) == 1

    def test_no_fallback_to_synthetic_on_failure(self, with_key):
        transport = FakeTransport(TransportResponse(500, b""))
        provider = _provider(transport)
        with pytest.raises(DashScopeProviderError):
            _embed(provider)
        assert provider.last_receipt is None
        assert provider.synthetic is False


class TestCacheIdentityIsolation:
    def _key(self, content="构造文本", **overrides):
        params = {
            "content": content,
            "model_name": MODEL_NAME,
            "model_version": "unknown-mutable",
            "preprocess_version": "p2-dashscope-v1",
            "task_type": TASK,
            "instruction": "",
            "normalize": True,
            "synthetic": False,
            "provider": "dashscope-openai-compat",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            "api_contract_revision": "p2-dashscope-embeddings-v1",
            "dimensions": 1024,
        }
        params.update(overrides)
        return cache_key(**params)

    def test_identity_fields_change_key(self):
        base = self._key()
        assert self._key(dimensions=768) != base
        assert self._key(api_contract_revision="p2-dashscope-embeddings-v2") != base
        assert self._key(endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddingsx?") != base
        assert self._key(synthetic=True) != base
        assert self._key(model_version="v-next") != base
        assert self._key(content="换一个文本") != base

    def test_stub_key_backward_compatible(self):
        # 不传扩展字段时与既有合成 stub 身份一致（老键不漂移）。
        legacy = cache_key(
            content="构造文本", model_name=MODEL_NAME, model_version="unknown-mutable",
            preprocess_version="p2-dashscope-v1", task_type=TASK, instruction="",
            normalize=True, synthetic=False,
        )
        assert legacy != self._key("构造文本")  # 扩展身份存在 => 键不同，API 记录不与无身份记录混用

    def test_real_api_record_isolated_from_synthetic(self):
        with VectorCache(expected_dim=4) as cache:
            real_key = cache_key(
                content="甲", model_name=MODEL_NAME, model_version="unknown-mutable",
                preprocess_version="p2-dashscope-v1", task_type=TASK, instruction="",
                normalize=False, synthetic=False,
            )
            synth_key = cache_key(
                content="甲", model_name="synthetic-stub", model_version="stub-v1",
                preprocess_version="none", task_type=TASK, instruction="",
                normalize=False, synthetic=True,
            )
            cache.put(real_key, [1.0, 0.0, 0.0, 0.0], model_name=MODEL_NAME,
                      model_version="unknown-mutable", synthetic=False)
            cache.put(synth_key, [0.0, 1.0, 0.0, 0.0], model_name="synthetic-stub",
                      model_version="stub-v1", synthetic=True)
            assert cache.get(real_key, require_real=True) == [1.0, 0.0, 0.0, 0.0]
            with pytest.raises(CacheIntegrityError):
                cache.get(synth_key, require_real=True)


class TestCostEstimate:
    def test_estimate_not_invoice(self):
        assert estimate_cost_cny({"total_tokens": 2000}) == pytest.approx(0.001)
        assert estimate_cost_cny({}) is None

    def test_receipt_fields_safe(self, with_key):
        base = [1.0] + [0.0] * 255
        alt = [0.0, 1.0] + [0.0] * 254
        transport = FakeTransport(TransportResponse(200, _ok_body([base, alt])))
        provider = _provider(transport, dimension=256)
        out, receipt = provider.embed_detailed(["甲", "乙"], task_type=TASK, instruction="", normalize=False)
        assert receipt.returned_model == MODEL_NAME
        assert receipt.usage == {"prompt_tokens": 42, "total_tokens": 42}
        assert receipt.count == 2
        assert len(receipt.vector_sha256) == 2
        assert receipt.estimated_cost_cny == pytest.approx(42 / 1000 * 0.0005)
        assert KEY not in json.dumps(receipt.__dict__)
