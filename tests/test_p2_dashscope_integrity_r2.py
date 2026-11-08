"""P2-C-DASHSCOPE-INTEGRITY-r2 离线专项测试：零真实网络、零真实密钥、零真实 claim。

覆盖：
- fake transport 端到端稳定 L2 归一化（normalize=True/False、极大/极小 finite、
  超大 JSON 整数、zero、NaN/inf），私有辅助函数不作为唯一测试面；
- validate_official_endpoint 精确路径（拒绝 startswith/endswith 兼容的额外路径变体）；
- 响应元数据脱敏：request_id 长度/字符集受限，usage 排除 bool/负值；
- smoke 脚本持久化原子 claim：claim-first（占用前不读密钥）、重复/并发/失败/崩溃
  不再请求、--check-only 零网络、真实 claim 文件不被导入/测试触碰。
"""

from __future__ import annotations

import importlib.util
import json
import math
import threading
from pathlib import Path

import pytest

from xhs_discovery.embeddings.dashscope import (
    DOCUMENTED_TASK_TYPES,
    MODEL_NAME,
    DashScopeEmbeddingProvider,
    DashScopeProviderError,
    TransportResponse,
    estimate_cost_cny,
    validate_official_endpoint,
)

TASK = DOCUMENTED_TASK_TYPES[0]
FAKE_KEY = "sk-FAKE-NOT-REAL-0123456789"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "p2_dashscope_smoke", PROJECT_ROOT / "scripts" / "p2_dashscope_smoke.py"
)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)  # 只导入：不得联网、不得触碰真实 claim


def _ok_body(vectors, *, model: str = MODEL_NAME, extra: dict | None = None) -> bytes:
    data = [{"index": i, "object": "embedding", "embedding": vec} for i, vec in enumerate(vectors)]
    payload = {"object": "list", "model": model, "data": data,
               "usage": {"prompt_tokens": 42, "total_tokens": 42}}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeTransport:
    def __init__(self, response):
        self._response = response
        self.calls: list[str] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(url)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _embed(body: bytes, *, dim: int, count: int, normalize: bool, monkeypatch) -> list[list[float]]:
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
    provider = DashScopeEmbeddingProvider(dimension=dim, transport=FakeTransport(TransportResponse(200, body)))
    return provider.embed(["甲"] * count, task_type=TASK, instruction="", normalize=normalize)


class TestStableL2Normalization:
    DIM = 256

    def test_huge_finite_1e308_normalizes_to_unit_not_zeros(self, monkeypatch):
        vec = [1e308] * self.DIM
        body = _ok_body([vec])
        out = _embed(body, dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)[0]
        assert all(math.isfinite(v) and v != 0.0 for v in out)
        assert all(math.isclose(v, 1.0 / math.sqrt(self.DIM), rel_tol=1e-9) for v in out)
        norm = math.sqrt(sum(v * v for v in out))
        assert math.isfinite(norm) and math.isclose(norm, 1.0, rel_tol=1e-12)

    def test_huge_mixed_sign_stays_finite(self, monkeypatch):
        vec = [1e308, -1e308] + [0.0] * (self.DIM - 2)
        out = _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)[0]
        assert all(math.isfinite(v) for v in out)
        assert math.isclose(out[0], 1 / math.sqrt(2), rel_tol=1e-12)
        assert math.isclose(out[1], -1 / math.sqrt(2), rel_tol=1e-12)

    def test_normalize_false_keeps_raw_huge_finite(self, monkeypatch):
        vec = [1e308] * self.DIM
        out = _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=False, monkeypatch=monkeypatch)[0]
        assert out == vec

    def test_tiny_finite_normalizes_correctly(self, monkeypatch):
        vec = [1e-300] + [0.0] * (self.DIM - 1)
        out = _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)[0]
        assert math.isclose(out[0], 1.0, abs_tol=1e-12)
        assert math.isclose(math.sqrt(sum(v * v for v in out)), 1.0, rel_tol=1e-12)

    def test_huge_json_integer_rejected_as_safe_error(self, monkeypatch):
        payload = {"model": MODEL_NAME, "data": [
            {"index": 0, "embedding": [10 ** 400] + [0.0] * (self.DIM - 1)}]}
        body = json.dumps(payload).encode()
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(body, dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)
        assert exc.value.category == "non_finite_vector"
        assert "400" not in str(exc.value)

    def test_huge_json_integer_rejected_even_without_normalize(self, monkeypatch):
        payload = {"model": MODEL_NAME, "data": [
            {"index": 0, "embedding": [9912] * self.DIM}]}
        body = json.dumps(payload).encode().replace(b"9912", b"1" + b"0" * 400)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(body, dim=self.DIM, count=1, normalize=False, monkeypatch=monkeypatch)
        assert exc.value.category == "non_finite_vector"

    def test_json_infinity_literal_rejected(self, monkeypatch):
        body = ('{"model": "' + MODEL_NAME + '", "data": [{"index": 0, "embedding": ['
                + ",".join(["1e999"] + ["0.0"] * (self.DIM - 1)) + "]}]}").encode()
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(body, dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)
        assert exc.value.category == "non_finite_vector"

    def test_nan_rejected_end_to_end(self, monkeypatch):
        vec = [float("nan")] + [0.0] * (self.DIM - 1)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)
        assert exc.value.category == "non_finite_vector"

    def test_zero_vector_rejected_both_modes(self, monkeypatch):
        vec = [0.0] * self.DIM
        for normalize in (True, False):
            with pytest.raises(DashScopeProviderError) as exc:
                _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=normalize, monkeypatch=monkeypatch)
            assert exc.value.category == "zero_norm_vector"

    def test_ordinary_vectors_unchanged(self, monkeypatch):
        vec = [3.0, 4.0] + [0.0] * (self.DIM - 2)
        raw = _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=False, monkeypatch=monkeypatch)[0]
        assert raw == vec
        out = _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)[0]
        assert math.isclose(out[0], 0.6, rel_tol=1e-12)
        assert math.isclose(out[1], 0.8, rel_tol=1e-12)

    @pytest.mark.parametrize("dim", [256, 1024, 2048, 2560])
    def test_huge_and_tiny_across_production_dims(self, dim, monkeypatch):
        """不只测既有 256：真实可配置的 1024/2048/2560 也必须稳定归一化。"""
        out = _embed(_ok_body([[1e308] * dim]), dim=dim, count=1, normalize=True, monkeypatch=monkeypatch)[0]
        assert all(math.isfinite(v) and v > 0.0 for v in out)
        assert math.isclose(math.sqrt(sum(v * v for v in out)), 1.0, rel_tol=1e-9)
        out_small = _embed(_ok_body([[1e-300] * dim]), dim=dim, count=1, normalize=True, monkeypatch=monkeypatch)[0]
        assert all(math.isfinite(v) and v > 0.0 for v in out_small)
        assert math.isclose(math.sqrt(sum(v * v for v in out_small)), 1.0, rel_tol=1e-9)

    @pytest.mark.parametrize("bad", [True, "1.0", None, [1.0], {"v": 1}])
    def test_non_numeric_entries_rejected_at_entry(self, bad, monkeypatch):
        vec = [bad] + [0.5] * (self.DIM - 1)
        with pytest.raises(DashScopeProviderError) as exc:
            _embed(_ok_body([vec]), dim=self.DIM, count=1, normalize=True, monkeypatch=monkeypatch)
        assert exc.value.category == "non_finite_vector"

    def test_complex_rejected_by_numeric_guard(self):
        """complex 不是 JSON 可表达值，但入口守卫同样必须拒绝（补充私有守卫用例）。"""
        from xhs_discovery.embeddings.dashscope import _to_finite_float

        with pytest.raises(DashScopeProviderError):
            _to_finite_float(1 + 2j)


class TestNoNativeLeakage:
    DIM = 256
    SECRET_BODY_FRAGMENT = "INTERNAL-LEAK-CHECK-9f3a"

    def _overflow_body(self) -> bytes:
        payload = {"model": MODEL_NAME, "data": [
            {"index": 0, "embedding": [10 ** 400] + [0.0] * (self.DIM - 1)}]}
        return json.dumps(payload).encode()

    def test_huge_int_full_traceback_has_no_native_or_url_leak(self, monkeypatch):
        import traceback

        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        transport = FakeTransport(TransportResponse(200, self._overflow_body()))
        provider = DashScopeEmbeddingProvider(dimension=self.DIM, transport=transport)
        with pytest.raises(DashScopeProviderError) as excinfo:
            provider.embed(["甲"], task_type=TASK, instruction="", normalize=True)
        rendered = "".join(
            traceback.format_exception(excinfo.type, excinfo.value, excinfo.value.__traceback__)
        )
        assert "OverflowError" not in rendered
        assert FAKE_KEY not in rendered
        assert "dashscope.aliyuncs" not in rendered
        assert str(10 ** 40)[:20] not in rendered
        assert excinfo.value.__cause__ is None

    def test_unexpected_transport_error_fails_closed_without_original(self, monkeypatch):
        class Boom(Exception):
            def __str__(self):
                return f"secret-url https://dashscope.aliyuncs.com/x key={FAKE_KEY}"

        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        provider = DashScopeEmbeddingProvider(dimension=self.DIM, transport=FakeTransport(Boom()))
        with pytest.raises(DashScopeProviderError) as excinfo:
            provider.embed(["甲"], task_type=TASK, instruction="", normalize=False)
        rendered = f"{excinfo.value!r}{excinfo.value.__cause__!r}"
        assert FAKE_KEY not in rendered and "secret-url" not in rendered
        assert excinfo.value.category == "transport_error"


class TestEndpointExactPath:
    OFFICIAL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"

    def test_exact_official_accepted(self):
        assert validate_official_endpoint(self.OFFICIAL) == self.OFFICIAL

    @pytest.mark.parametrize("url", [
        "https://dashscope.aliyuncs.com/compatible-mode/v1evil/embeddings",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings/extra",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddements",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "https://dashscope.aliyuncs.com/compatible-mode/embeddings",
        "https://dashscope.aliyuncs.com/embeddings",
    ])
    def test_prefix_suffix_loose_variants_rejected(self, url):
        with pytest.raises(DashScopeProviderError) as exc:
            validate_official_endpoint(url)
        assert exc.value.category == "insecure_or_untrusted_endpoint"


class TestResponseMetadataSanitization:
    DIM = 256

    def _receipt(self, extra: dict, monkeypatch):
        vec = [1.0] + [0.0] * (self.DIM - 1)
        body = _ok_body([vec], extra=extra)
        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        provider = DashScopeEmbeddingProvider(dimension=self.DIM, transport=FakeTransport(TransportResponse(200, body)))
        return provider.embed_detailed(["甲"], task_type=TASK, instruction="", normalize=False)[1]

    def test_valid_request_id_kept(self, monkeypatch):
        receipt = self._receipt({"id": "a7840fcb-3ca5-969e-81bd-b6eea99e3533"}, monkeypatch)
        assert receipt.request_id == "a7840fcb-3ca5-969e-81bd-b6eea99e3533"

    @pytest.mark.parametrize("bad_id", [
        "x" * 65,
        "id with spaces and\nnewline",
        "标识符-中文",
        "\u0007evil",
    ])
    def test_unsafe_request_id_dropped(self, bad_id, monkeypatch):
        receipt = self._receipt({"id": bad_id}, monkeypatch)
        assert receipt.request_id is None

    def test_usage_bool_and_negative_excluded(self, monkeypatch):
        vec = [1.0] + [0.0] * (self.DIM - 1)
        body = _ok_body([vec], extra={"usage": {"prompt_tokens": True, "total_tokens": -5}})
        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        provider = DashScopeEmbeddingProvider(dimension=self.DIM, transport=FakeTransport(TransportResponse(200, body)))
        receipt = provider.embed_detailed(["甲"], task_type=TASK, instruction="", normalize=False)[1]
        assert receipt.usage == {}
        assert receipt.estimated_cost_cny is None

    def test_estimate_cost_excludes_bool(self):
        assert estimate_cost_cny({"total_tokens": True}) is None
        assert estimate_cost_cny({"total_tokens": -100}) is None
        assert estimate_cost_cny({"total_tokens": 2000}) == pytest.approx(0.001)

    def test_usage_huge_int_excluded_by_single_rule(self, monkeypatch):
        vec = [1.0] + [0.0] * (self.DIM - 1)
        body = _ok_body([vec], extra={"usage": {"prompt_tokens": 10 ** 400, "total_tokens": 42}})
        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        provider = DashScopeEmbeddingProvider(dimension=self.DIM, transport=FakeTransport(TransportResponse(200, body)))
        receipt = provider.embed_detailed(["甲"], task_type=TASK, instruction="", normalize=False)[1]
        assert receipt.usage == {"total_tokens": 42}
        assert estimate_cost_cny({"total_tokens": 10 ** 400}) is None


class TestSmokeClaimGuard:
    def _main(self, monkeypatch, tmp_path, live_call, argv=None, key=True):
        if key:
            monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        else:
            monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        claim = tmp_path / "claim" / "single_use.json"
        rc = smoke.main(argv or [], claim_file=claim, live_call=live_call)
        return rc, claim

    def test_first_run_claims_then_second_blocked(self, monkeypatch, tmp_path, capsys):
        calls = []
        rc, claim = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 0)
        assert rc == 0 and len(calls) == 1 and claim.exists()
        rc2, _ = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 0)
        assert rc2 == 3 and len(calls) == 1
        assert "ALREADY_CONSUMED" in capsys.readouterr().out

    def test_failed_run_not_retried(self, monkeypatch, tmp_path):
        calls = []
        rc, _ = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 1)
        assert rc == 1 and len(calls) == 1
        rc2, _ = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 0)
        assert rc2 == 3 and len(calls) == 1

    def test_crash_keeps_claim_no_retry(self, monkeypatch, tmp_path):
        calls = []

        def crasher():
            calls.append(1)
            raise RuntimeError("simulated crash")

        with pytest.raises(RuntimeError):
            self._main(monkeypatch, tmp_path, crasher)
        assert len(calls) == 1
        rc, _ = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 0)
        assert rc == 3 and len(calls) == 1

    def test_concurrent_claim_exactly_one_winner(self, tmp_path):
        claim = tmp_path / "race.json"
        results: list[bool] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(smoke.claim_single_use(claim, {"race": True}))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(1 for r in results if r) == 1

    def test_missing_key_after_claim_no_network_no_reuse(self, monkeypatch, tmp_path):
        """claim-first：发行 claim 前不读取密钥；缺失即 BLOCKED，claim 已占用不复用。"""
        calls = []
        rc, claim = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 0, key=False)
        assert rc == 2 and calls == []
        assert claim.exists()  # 已占用：失败不再自动重试/重占
        rc2, _ = self._main(monkeypatch, tmp_path, lambda: calls.append(1) or 0, key=True)
        assert rc2 == 3 and calls == []

    def test_check_only_zero_network_and_untouched_real_claim(self, monkeypatch, capsys):
        real_claim = smoke.CLAIM_FILE
        before = real_claim.read_text(encoding="utf-8")
        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        rc = smoke.main(["--check-only"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["network_used"] is False and out["claim_consumed"] is True
        assert real_claim.read_text(encoding="utf-8") == before

    def test_import_did_not_create_or_consume_real_claim(self):
        data = json.loads(smoke.CLAIM_FILE.read_text(encoding="utf-8"))
        assert data["phase"] == "consumed"
        assert data["request_id"] == "a7840fcb-3ca5-969e-81bd-b6eea99e3533"

    def test_real_claim_blocks_network_path(self, monkeypatch, capsys):
        import urllib.request

        def spy(*a, **kw):
            raise AssertionError("real network attempted")

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
        monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
        rc = smoke.main([])
        assert rc == 3
        assert "ALREADY_CONSUMED" in capsys.readouterr().out
