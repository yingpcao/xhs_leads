"""分组切分测试：近重复家族/来源泄漏拒绝、指纹稳定、组不足拒评。"""

from __future__ import annotations

import pytest

from xhs_discovery.evaluation.splits import (
    SPLIT_VERSION,
    InsufficientGroupsError,
    SplitItem,
    group_split,
    validate_split,
)


def _items(specs: list[tuple[str, str, str]]) -> list[SplitItem]:
    return [SplitItem(sample_id=s, source_id=src, family_id=fam) for s, src, fam in specs]


def _many_items(n: int = 12) -> list[SplitItem]:
    # 每个样本独立来源与家族，但 4 个一组共享家族制造近重复簇。
    items = []
    for i in range(n):
        items.append(SplitItem(sample_id=f"s{i}", source_id=f"src{i}", family_id=f"fam{i // 4}"))
    return items


class TestGrouping:
    def test_same_family_never_splits(self):
        for seed in (1, 7, 20261029, 999):
            result = group_split(_many_items(), seed=seed)
            validate_split(result, _many_items())
            by_family: dict[str, set[str]] = {}
            for item in _many_items():
                by_family.setdefault(item.family_id, set()).add(result.partition_of(item.sample_id))
            assert all(len(parts) == 1 for parts in by_family.values())

    def test_shared_source_joins_families_into_one_group(self):
        # s1/s2 不同家族但同来源：必须整组同分区，防止跨源泄漏。
        specs = [
            ("s1", "srcA", "fam1"),
            ("s2", "srcA", "fam2"),
            ("s3", "srcB", "fam3"),
            ("s4", "srcC", "fam4"),
            ("s5", "srcD", "fam5"),
            ("s6", "srcE", "fam6"),
        ]
        items = _items(specs)
        result = group_split(items, seed=5)
        assert result.partition_of("s1") == result.partition_of("s2")
        validate_split(result, items)

    def test_duplicate_sample_id_rejected(self):
        items = _items([("s1", "a", "x"), ("s1", "b", "y")])
        with pytest.raises(ValueError, match="唯一"):
            group_split(items)


class TestRefusal:
    def test_single_group_refused(self):
        items = _items([(f"s{i}", "srcA", "famA") for i in range(10)])
        with pytest.raises(InsufficientGroupsError, match="独立来源/家族组"):
            group_split(items)

    def test_empty_refused(self):
        with pytest.raises(InsufficientGroupsError):
            group_split([])

    def test_bad_ratio_refused(self):
        with pytest.raises(ValueError, match="heldout_ratio"):
            group_split(_many_items(), heldout_ratio=0.0)

    def test_validate_detects_tampered_leakage(self):
        items = _many_items()
        result = group_split(items, seed=3)
        # 人为把同一家族的一个样本挪到另一分区：validate_split 必须抓出。
        fam_samples = [i.sample_id for i in items if i.family_id == "fam0"]
        part = result.partition_of(fam_samples[0])
        moved = next(s for s in fam_samples if result.partition_of(s) == part and s != fam_samples[0])
        if part == "dev":
            result.dev_sample_ids.remove(moved)
        else:
            result.heldout_sample_ids.remove(moved)
        with pytest.raises(ValueError):
            validate_split(result, items)


class TestFingerprint:
    def test_stable_across_runs(self):
        first = group_split(_many_items(), seed=42)
        second = group_split(list(reversed(_many_items())), seed=42)
        assert first.fingerprint == second.fingerprint
        assert first.dev_sample_ids == second.dev_sample_ids
        assert first.version == SPLIT_VERSION

    def test_seed_and_version_change_fingerprint(self):
        base = group_split(_many_items(), seed=42)
        other_seed = group_split(_many_items(), seed=43)
        assert base.fingerprint != other_seed.fingerprint
        mutated = base.model_copy(update={"version": "tampered"})
        with pytest.raises(ValueError, match="指纹"):
            validate_split(mutated, _many_items())

    def test_partitions_nonempty(self):
        result = group_split(_many_items(), seed=8)
        assert result.dev_sample_ids and result.heldout_sample_ids
        assert set(result.dev_sample_ids) & set(result.heldout_sample_ids) == set()
