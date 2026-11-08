"""确定性切分：按来源/内容家族分组，防止近重复跨分区泄漏。

设计约束（DEVELOPMENT_PLAN.md P2、PROJECT_CHECKLIST P2-02）：
- 同一样本或同一家族/来源的记录必须整组进入同一分区，禁止逐条随机切分；
- 分组用并查集合并共享 source_id 或 family_id 的样本，组分配由
  (seed, SPLIT_VERSION, 组键) 的哈希决定，可复现且带指纹；
- 组数不足或任一分区为空时抛 InsufficientGroupsError：拒绝产出会误导人的评估。
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable

from pydantic import Field, model_validator

from xhs_discovery.models.common import StrictModel

SPLIT_VERSION = "p2-split-v1"
MIN_GROUPS = 2


class InsufficientGroupsError(ValueError):
    """分组不足/分区为空：此时任何 dev/heldout 对比都会误导，必须拒绝评估。"""


class SplitItem(StrictModel):
    sample_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)

    def group_keys(self) -> tuple[str, str]:
        return (f"source:{self.source_id}", f"family:{self.family_id}")


class SplitResult(StrictModel):
    dev_sample_ids: list[str]
    heldout_sample_ids: list[str]
    group_assignment: dict[str, str]
    seed: int
    version: str
    fingerprint: str
    # 冻结切生参数：指纹包含 ratio，事后改参数必然指纹失配。
    heldout_ratio: float = 0.4

    def partition_of(self, sample_id: str) -> str:
        if sample_id in self.dev_sample_ids:
            return "dev"
        if sample_id in self.heldout_sample_ids:
            return "heldout"
        raise KeyError(f"样本 {sample_id} 不在切分结果中")


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _union_groups(items: list[SplitItem]) -> dict[str, tuple[str, list[str]]]:
    """并查集：把共享 source 或 family 的样本并入同组，返回 组键->(代表, 成员)。"""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for item in items:
        keys = item.group_keys()
        find(keys[0])
        find(keys[1])
        union(keys[0], keys[1])

    groups: dict[str, list[str]] = {}
    for item in items:
        root = min(find(k) for k in item.group_keys())
        groups.setdefault(root, []).append(item.sample_id)
    return {
        root: (root, sorted(set(members)))
        for root, members in groups.items()
    }


def _fingerprint(
    assignment: dict[str, str], sample_partition: dict[str, str], seed: int, heldout_ratio: float
) -> str:
    payload = json.dumps(
        {
            "version": SPLIT_VERSION,
            "seed": seed,
            "heldout_ratio": heldout_ratio,
            "groups": sorted(assignment.items()),
            "samples": sorted(sample_partition.items()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _stable_hash(payload)


def group_split(items: Iterable[SplitItem], seed: int = 20261029, heldout_ratio: float = 0.4) -> SplitResult:
    """整组确定性分配：按哈希(seed+version+组键) 排序后，前 round(ratio*n) 组进 heldout。

    分层保证（组数>=2 时）两分区必然非空，且结果与输入顺序无关、可复现。
    """
    if not 0.0 < heldout_ratio < 1.0:
        raise ValueError("heldout_ratio 必须在 (0,1) 开区间，否则分区必然为空")
    items = list(items)
    sample_ids = [item.sample_id for item in items]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("同一 sample_id 出现多次且字段不同？sample_id 必须唯一")
    if not items:
        raise InsufficientGroupsError("没有任何样本，拒绝切分")

    groups = _union_groups(items)
    if len(groups) < MIN_GROUPS:
        raise InsufficientGroupsError(
            f"只有 {len(groups)} 个独立来源/家族组（<{MIN_GROUPS}）："
            "dev/heldout 对比会因近重复泄漏误导，拒绝评估"
        )

    scored = sorted(
        (_stable_hash(f"{SPLIT_VERSION}|{seed}|{root}"), root, members)
        for root, (_, members) in groups.items()
    )
    n = len(scored)
    heldout_count = max(1, min(n - 1, round(n * heldout_ratio)))
    assignment: dict[str, str] = {}
    sample_partition: dict[str, str] = {}
    for index, (_, root, members) in enumerate(scored):
        partition = "heldout" if index < heldout_count else "dev"
        assignment[root] = partition
        for member in members:
            sample_partition[member] = partition

    dev = sorted(s for s, p in sample_partition.items() if p == "dev")
    heldout = sorted(s for s, p in sample_partition.items() if p == "heldout")
    return SplitResult(
        dev_sample_ids=dev,
        heldout_sample_ids=heldout,
        group_assignment=assignment,
        seed=seed,
        version=SPLIT_VERSION,
        fingerprint=_fingerprint(assignment, sample_partition, seed, heldout_ratio),
        heldout_ratio=heldout_ratio,
    )


def validate_split(result: SplitResult, items: Iterable[SplitItem]) -> None:
    """复核切分：按实际样本元数据整体重建切分，逐项核对，绝不采信自报字段。

    覆盖：版本、样本 ID 重复/元数据冲突、分区不相交且非空、成员完整性、
    同组(来源/家族)跨分区泄漏、组分配一致性、seed/ratio/指纹可复现。
    任何一项不符即抛错：同一家族跨 dev/heldout 不可接受，即使 ID 与指纹
    字符串原封不动。
    """
    if result.version != SPLIT_VERSION:
        raise ValueError(f"切分版本不是 {SPLIT_VERSION}：指纹不可信")
    items = list(items)
    counts: dict[str, int] = {}
    seen: dict[str, tuple[str, str]] = {}
    for item in items:
        counts[item.sample_id] = counts.get(item.sample_id, 0) + 1
        if item.sample_id in seen and seen[item.sample_id] != (item.source_id, item.family_id):
            raise ValueError(f"重复 sample_id {item.sample_id!r} 且来源/家族元数据冲突")
        seen[item.sample_id] = (item.source_id, item.family_id)
    duplicated = sorted(sid for sid, count in counts.items() if count > 1)
    if duplicated:
        raise ValueError(f"同一 sample_id 重复出现于样本清单：{duplicated}")
    dev = set(result.dev_sample_ids)
    heldout = set(result.heldout_sample_ids)
    if dev & heldout:
        raise ValueError(f"泄漏：样本同时出现在 dev 与 heldout：{sorted(dev & heldout)}")
    if not dev or not heldout:
        raise ValueError("分区为空：该切分不可用于对比评估")
    expected_ids = {item.sample_id for item in items}
    if dev | heldout != expected_ids:
        missing = expected_ids ^ (dev | heldout)
        raise ValueError(f"切分与样本集不一致，涉及：{sorted(missing)}")

    groups = _union_groups(items)
    for root, (_, members) in groups.items():
        partitions = {result.partition_of(m) for m in members}
        if len(partitions) > 1:
            raise ValueError(f"家族/来源泄漏：组 {root} 跨分区 {sorted(partitions)}")

    # 整体重建：用冻结 seed/ratio 从实际元数据重算切分，必须逐字段一致。
    rebuilt = group_split(items, seed=result.seed, heldout_ratio=result.heldout_ratio)
    rebuilt_partition = {s: "dev" for s in rebuilt.dev_sample_ids} | {
        s: "heldout" for s in rebuilt.heldout_sample_ids
    }
    supplied_partition = {s: "dev" for s in dev} | {s: "heldout" for s in heldout}
    mismatches: list[str] = []
    if rebuilt.fingerprint != result.fingerprint:
        mismatches.append("指纹与按实际样本元数据重建的结果不一致：可能被篡改")
    if rebuilt.group_assignment != result.group_assignment:
        mismatches.append("组分配与重建结果不一致")
    if rebuilt_partition != supplied_partition:
        mismatches.append("分区成员与重建结果不一致")
    if rebuilt.seed != result.seed or rebuilt.heldout_ratio != result.heldout_ratio:
        mismatches.append("冻结的 seed/heldout_ratio 与重建参数不一致")
    rebuilt_fp = _fingerprint(
        result.group_assignment, supplied_partition, result.seed, result.heldout_ratio
    )
    if rebuilt_fp != result.fingerprint:
        mismatches.append("指纹与切分内容不一致：结果可能被篡改")
    if mismatches:
        raise ValueError("；".join(mismatches))


__all__ = [
    "SPLIT_VERSION",
    "MIN_GROUPS",
    "InsufficientGroupsError",
    "SplitItem",
    "SplitResult",
    "group_split",
    "validate_split",
]
