"""Embedding 基础设施测试：禁用不构造 provider、导入不加载模型、
版本/任务/指令任一不同即缓存 miss、NaN/维度拒绝、合成记录不能进生产检索。"""

from __future__ import annotations

import math
import sys

import pytest

from xhs_discovery.embeddings import (
    CacheIntegrityError,
    ProviderDisabledError,
    RealProviderNotAvailableError,
    SyntheticStubProvider,
    VectorCache,
    build_provider,
    cache_key,
    cosine,
    cosine_top_k,
)


def _key(**overrides) -> str:
    params = {
        "content": "上海中考数学怎么提高",
        "model_name": "synthetic-stub",
        "model_version": "stub-v1",
        "preprocess_version": "none",
        "task_type": "retrieval",
        "instruction": "represent the need",
        "normalize": True,
        "synthetic": True,
    }
    params.update(overrides)
    return cache_key(**params)


class TestNoImportLoad:
    def test_importing_embeddings_loads_no_model(self):
        import xhs_discovery.embeddings  # noqa: F401

        assert "sentence_transformers" not in sys.modules
        assert "torch" not in sys.modules

    def test_disabled_config_constructs_no_provider(self):
        with pytest.raises(ProviderDisabledError):
            build_provider(embedding_enabled=False, embedding_model=None)

    def test_real_model_request_refused_without_download(self):
        with pytest.raises(RealProviderNotAvailableError):
            build_provider(embedding_enabled=True, embedding_model="qwen3-embedding-0.6b")

    def test_stub_requires_explicit_opt_in(self):
        with pytest.raises(RealProviderNotAvailableError):
            build_provider(embedding_enabled=True, embedding_model="synthetic-stub")
        provider = build_provider(
            embedding_enabled=True, embedding_model="synthetic-stub", allow_synthetic_stub=True
        )
        assert provider.synthetic is True


class TestStubProvider:
    def test_deterministic_and_protocol(self):
        provider = SyntheticStubProvider(dimension=8)
        first = provider.embed(["文本A"], task_type="retrieval", instruction="i", normalize=False)
        second = provider.embed(["文本A"], task_type="retrieval", instruction="i", normalize=False)
        assert first == second
        assert len(first[0]) == 8
        other = provider.embed(["文本B"], task_type="retrieval", instruction="i", normalize=False)
        assert other[0] != first[0]

    def test_normalize_produces_unit_norm(self):
        provider = SyntheticStubProvider(dimension=8)
        vec = provider.embed(["t"], task_type="retrieval", instruction="i", normalize=True)[0]
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


class TestCacheKey:
    @pytest.mark.parametrize(
        "override",
        [
            {"model_name": "other-model"},
            {"model_version": "stub-v2"},
            {"preprocess_version": "v2"},
            {"task_type": "clustering"},
            {"instruction": "different instruction"},
            {"normalize": False},
            {"synthetic": False},
            {"content": "同一句话改了标点"},
        ],
    )
    def test_any_field_change_misses(self, override):
        assert _key() != _key(**override)

    def test_identical_inputs_same_key(self):
        assert _key() == _key()

    def test_missing_field_rejected(self):
        with pytest.raises(CacheIntegrityError):
            cache_key(
                content="c", model_name="m", model_version="", preprocess_version="p",
                task_type="t", instruction="i", normalize=True, synthetic=True,
            )


class TestCacheIntegrity:
    def test_nan_rejected_on_put(self):
        with VectorCache(expected_dim=4) as cache:
            with pytest.raises(CacheIntegrityError, match="NaN"):
                cache.put(_key(), [0.1, 0.2, float("nan"), 0.4],
                          model_name="synthetic-stub", model_version="stub-v1", synthetic=True)

    def test_inf_rejected_on_put(self):
        with VectorCache(expected_dim=4) as cache:
            with pytest.raises(CacheIntegrityError):
                cache.put(_key(), [0.1, 0.2, 0.3, float("inf")],
                          model_name="synthetic-stub", model_version="stub-v1", synthetic=True)

    def test_dimension_mismatch_rejected(self):
        with VectorCache(expected_dim=4) as cache:
            with pytest.raises(CacheIntegrityError, match="维度"):
                cache.put(_key(), [0.1, 0.2], model_name="m", model_version="v", synthetic=True)

    def test_wrong_version_key_misses(self):
        with VectorCache(expected_dim=4) as cache:
            key_v1 = _key()
            cache.put(key_v1, [0.1, 0.2, 0.3, 0.4], model_name="synthetic-stub",
                      model_version="stub-v1", synthetic=True)
            assert cache.get(_key(model_version="stub-v2")) is None
            assert cache.get(key_v1) is not None

    def test_synthetic_record_cannot_contaminate_real_retrieval(self):
        with VectorCache(expected_dim=4) as cache:
            synth_key = _key(synthetic=True)
            cache.put(synth_key, [0.1, 0.2, 0.3, 0.4], model_name="synthetic-stub",
                      model_version="stub-v1", synthetic=True)
            assert cache.get(synth_key) is not None
            with pytest.raises(CacheIntegrityError, match="合成"):
                cache.get(synth_key, require_real=True)

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "vectors.db"
        key = _key()
        with VectorCache(path=path, expected_dim=4) as cache:
            cache.put(key, [0.1, 0.2, 0.3, 0.4], model_name="synthetic-stub",
                      model_version="stub-v1", synthetic=True)
        with VectorCache(path=path, expected_dim=4) as reopened:
            assert reopened.get(key) == pytest.approx([0.1, 0.2, 0.3, 0.4])
            assert reopened.stats()["synthetic"] == 1


class TestRetrieval:
    def test_cosine_values_and_errors(self):
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        with pytest.raises(ValueError, match="维度"):
            cosine([1.0], [1.0, 0.0])
        with pytest.raises(ValueError, match="NaN"):
            cosine([float("nan"), 0.0], [1.0, 0.0])
        with pytest.raises(ValueError, match="零向量"):
            cosine([0.0, 0.0], [1.0, 1.0])

    def test_top_k_deterministic_with_ties(self):
        corpus = {"b": [1.0, 0.0], "a": [1.0, 0.0], "c": [0.0, 1.0]}
        first = cosine_top_k([1.0, 0.0], corpus, 2)
        second = cosine_top_k([1.0, 0.0], dict(reversed(list(corpus.items()))), 2)
        assert first == second
        assert [key for key, _ in first] == ["a", "b"]

    def test_stub_vectors_retrieve_self_first(self):
        provider = SyntheticStubProvider(dimension=16)
        texts = ["上海中考数学怎么提高", "北京考研资料", "上海中考数学如何提高"]
        vectors = provider.embed(texts, task_type="retrieval", instruction="i", normalize=True)
        corpus = {t: v for t, v in zip(texts, vectors)}
        top = cosine_top_k(vectors[0], corpus, 2)
        assert top[0][0] == texts[0]
        assert 0.9 <= top[0][1] <= 1.0 + 1e-9
