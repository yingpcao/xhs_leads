"""离线指标契约与评估器：只消费已带人工标签的预测，绝不执行合并。

设计约束（DEVELOPMENT_PLAN.md:211-212、G2-01、P2-A-EVAL-BOUNDARIES-r2）：
- 所有比率显式给出分子/分母；分母为 0 时结果为 None（unknown），不是 0；
- 坏标签/unknown/归属错误的预测一律 excluded 并带原因，不悄悄丢弃；
- 合并指标：无序对的两个端点都必须属于冻结 eligible 集且互不相同；
  自配对、外来端点一律拒绝并带原因；同一无序对重复出现只计一次，
  人工标签冲突则整对剔除，既不虚增分母也不泄漏冲突标签；
- ScoredSet 携带稳定 eligible 指纹（排序后的样本 ID + dataset_id +
  split_fingerprint + 指纹版本）；两臂比较要求指纹一致，仅长度一致
  不构成"同一冻结集"；
- 指标必须有显式方向（error/成本类越低越好，precision/recall 类越高
  越好）；未注册方向的指标名一律拒绝比较；
- 预声明改善必须由不可变 ExperimentSpec 在评估各臂之前承载并校验，
  不接受事后补一个字符串/字典来"祝福"任意结果；
- 门禁的 human heldout 资格与数量由已校验标注记录 + 切分 manifest 推导，不再采信自报来源字段与另行提交的计数；
- 门禁按实际记录元数据（sample/source/family/content）整体重建并复核 SplitResult：
  同家族/来源跨 dev/heldout、篡改指纹/分配、成员不完整一律拒绝，即使提交的 ID 与指纹字符串未变；
- 合并指标必须由已校验 PairAnnotationRecord 人工金标祝福：预测自报 human_relation 不能替代金标，
  抽取金标不能祝福合并质量；打分集通过金标绑定指纹（含内容哈希、任务标签、复核人、来源、切分参数）
  与当前金标集逐项绑定，打分后改动金标/记录身份无法延续旧 PASS；
- 全 unknown 标签的 reviewed 记录是合法在途标注，但不是金标，不能支撑 PASS；
- 计划口径是总共 200-300 条人工标注并按组切 dev/heldout；门禁检查
  人工总量达标（200-300）且 heldout 内至少 HELDOUT_HUMAN_MIN 条人工
  金标样本（实验下限，不在 200 总量之上再叠加 200 heldout）；
- 合成 fixture / 模型生成标签永远不能通过产品门禁；
- 缺失合法工件判 NOT_EVALUATED 并给出原因；任何缺失/不符都不以任意 PASS 兜底。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Literal

from pydantic import ConfigDict, Field, model_validator

from xhs_discovery.models.common import StrictModel
from xhs_discovery.evaluation.annotations import (
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
    REQUIRED_HUMAN_DATASET_MIN,
    human_dataset_status,
)
from xhs_discovery.evaluation.splits import SPLIT_VERSION, SplitItem, SplitResult, validate_split

METRICS_REPORT_VERSION = "p2-metrics-v1"
FINGERPRINT_VERSION = "p2-eligible-fingerprint-v1"
EXPERIMENT_SPEC_VERSION = "p2-experiment-spec-v1"
MERGE_PRECISION_MIN = 0.90
ERROR_MERGE_RATE_MAX = 0.05
# 实验下限：heldout 分区内至少要有这么多"人工金标"样本才允许出门禁结论。
# 计划固定的是总量 200-300 条人工标注（REQUIRED_HUMAN_DATASET_MIN/MAX），
# 不是在总量之上再要求 200 条 heldout。
HELDOUT_HUMAN_MIN = 20

GateDecision = Literal["PASS", "FAIL", "NOT_EVALUATED"]
MetricDirection = Literal["higher", "lower"]

# 指标方向登记表：未知名字必须先显式注册方向，否则拒绝比较。
METRIC_DIRECTIONS: dict[str, str] = {
    "merge_precision": "higher",
    "extraction_precision": "higher",
    "extraction_recall": "higher",
    "error_merge_rate": "lower",
}


def register_metric_direction(name: str, direction: MetricDirection) -> None:
    if not name or not isinstance(name, str):
        raise ValueError("指标名不能为空")
    if direction not in ("higher", "lower"):
        raise ValueError(f"指标方向只能是 higher 或 lower，收到 {direction!r}")
    METRIC_DIRECTIONS[name] = direction


class MetricCount(StrictModel):
    """显式分子/分母；分母为 0 时 value=None（unknown），禁止把 unknown 写成 0。"""

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @property
    def value(self) -> float | None:
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    def as_dict(self) -> dict[str, object]:
        return {"numerator": self.numerator, "denominator": self.denominator, "value": self.value}


class MergePrediction(StrictModel):
    """一条"系统判定为自动合并"的预测及其人工金标；本模块不执行任何合并。"""

    pair_left_sample_id: str = Field(min_length=1)
    pair_right_sample_id: str = Field(min_length=1)
    system_auto_merged: bool
    human_relation: Literal["same_need", "different_need", "unknown"] | None = None
    evidence_sample_ids: list[str] = Field(default_factory=list)


class ExtractionPrediction(StrictModel):
    """一条针对某样本的抽取预测（单一布尔标签维度）。"""

    sample_id: str = Field(min_length=1)
    label_field: str = Field(min_length=1)
    predicted: bool
    human_label: bool | None = None
    evidence_sample_ids: list[str] = Field(default_factory=list)


def compute_eligible_fingerprint(
    eligible_sample_ids: Iterable[str],
    *,
    dataset_id: str | None = None,
    split_fingerprint: str | None = None,
) -> str:
    """冻结 eligible 集指纹：与提交顺序无关，能区分同一长度的不同实例集。"""
    payload = json.dumps(
        {
            "version": FINGERPRINT_VERSION,
            "dataset_id": dataset_id,
            "split_fingerprint": split_fingerprint,
            "eligible_ids": sorted(set(eligible_sample_ids)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GOLD_BINDING_VERSION = "p2-gold-binding-v1"
MERGE_METRIC_NAMES = ("merge_precision", "error_merge_rate")
EXTRACTION_METRIC_NAMES = ("extraction_precision", "extraction_recall")


def compute_gold_fingerprint(
    *,
    task: str,
    entries: Iterable[str],
    dataset_id: str | None = None,
    split_fingerprint: str | None = None,
) -> str:
    """金标绑定指纹：绑定逐条金标内容（内容哈希/任务标签/复核人/来源）
    与评估相关切分参数；打分后改动金标或记录身份必然失配。"""
    payload = json.dumps(
        {
            "version": GOLD_BINDING_VERSION,
            "task": task,
            "dataset_id": dataset_id,
            "split_fingerprint": split_fingerprint,
            "entries": sorted(entries),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provenance_fields(provenance) -> str:
    produced_at = provenance.produced_at.isoformat() if provenance.produced_at else None
    return json.dumps(
        {
            "source": provenance.source,
            "dataset_id": provenance.dataset_id,
            "producer_id": provenance.producer_id,
            "produced_at": produced_at,
            "synthetic": provenance.synthetic,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pair_gold_entry(record: PairAnnotationRecord) -> str:
    return json.dumps(
        {
            "pair": sorted((record.sample_id, record.right_sample_id)),
            "relation": record.pair_relation,
            "left_source_id": record.source_id,
            "left_family_id": record.family_id,
            "content_sha256": record.content_sha256,
            "reviewer_state": record.reviewer_state,
            "reviewer_id": record.reviewer_id,
            "provenance": _provenance_fields(record.provenance),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _extraction_gold_entry(record: ExtractionAnnotationRecord) -> str:
    return json.dumps(
        {
            "sample_id": record.sample_id,
            "source_id": record.source_id,
            "family_id": record.family_id,
            "content_sha256": record.content_sha256,
            "label": json.dumps(record.label, sort_keys=True, separators=(",", ":"))
            if record.label
            else None,
            "reviewer_state": record.reviewer_state,
            "reviewer_id": record.reviewer_id,
            "provenance": _provenance_fields(record.provenance),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pair_gold_index(
    gold_pairs: "Iterable[PairAnnotationRecord] | None",
) -> "dict[tuple[str, str], dict[str, object]] | None":
    """无序对键 -> 金标信息；完全重复的金标合并计数，冲突 relation 标记剔除。"""
    if gold_pairs is None:
        return None
    index: dict[tuple[str, str], dict[str, object]] = {}
    for record in gold_pairs:
        key = tuple(sorted((record.sample_id, record.right_sample_id)))
        entry = _pair_gold_entry(record)
        existing = index.get(key)
        if existing is None:
            index[key] = {"relation": record.pair_relation, "conflict": False}
        elif existing["relation"] != record.pair_relation:
            existing["conflict"] = True
        # 同键同 relation 的重复金标：折叠，不虚增
    return index


class ScoredSet(StrictModel):
    counts: dict[str, MetricCount]
    excluded: list[dict[str, str]] = Field(default_factory=list)
    eligible_size: int = Field(ge=0)
    fingerprint: str | None = None
    # 金标绑定指纹：仅当打分显式绑定已校验金标集时存在；自报标签不构成绑定。
    gold_fingerprint: str | None = None


class EvaluationReport(StrictModel):
    version: str = METRICS_REPORT_VERSION
    status: GateDecision = "NOT_EVALUATED"
    arm: str | None = None
    merge: ScoredSet | None = None
    extraction: ScoredSet | None = None
    baseline_vs_enhanced: dict[str, object] | None = None
    gate: dict[str, object] | None = None
    notes: list[str] = Field(default_factory=list)


def _score_merges(
    predictions: list[MergePrediction],
    eligible_sample_ids: set[str],
    *,
    dataset_id: str | None = None,
    split_fingerprint: str | None = None,
    gold_pairs: "Iterable[PairAnnotationRecord] | None" = None,
) -> ScoredSet:
    tp = fp = counted = 0
    excluded: list[dict[str, str]] = []
    groups: dict[tuple[str, str], list[str]] = {}
    relations: dict[tuple[str, str], str] = {}
    gold_index = _pair_gold_index(gold_pairs)
    gold_fingerprint: str | None = None
    if gold_pairs is not None:
        gold_fingerprint = compute_gold_fingerprint(
            task="merge",
            entries=sorted({_pair_gold_entry(record) for record in gold_pairs}),
            dataset_id=dataset_id,
            split_fingerprint=split_fingerprint,
        )
    for pred in predictions:
        pair_id = f"{pred.pair_left_sample_id}|{pred.pair_right_sample_id}"
        if pred.pair_left_sample_id == pred.pair_right_sample_id:
            excluded.append({"id": pair_id, "reason": "自配对：两端为同一样本，不构成合法语义对"})
            continue
        outside = sorted(
            {s for s in (pred.pair_left_sample_id, pred.pair_right_sample_id) if s not in eligible_sample_ids}
        )
        if outside:
            excluded.append({"id": pair_id, "reason": f"对端点不属于冻结 eligible 评估集：{outside}"})
            continue
        unknown_evidence = [s for s in pred.evidence_sample_ids if s not in eligible_sample_ids]
        if unknown_evidence:
            excluded.append({"id": pair_id, "reason": f"证据样本不属于冻结评估集：{unknown_evidence}"})
            continue
        key = tuple(sorted((pred.pair_left_sample_id, pred.pair_right_sample_id)))
        if gold_index is not None:
            info = gold_index.get(key)
            if info is None:
                excluded.append(
                    {"id": pair_id, "reason": "评估对缺少 PairAnnotationRecord 人工金标：抽取金标与自报关系不能替代"}
                )
                continue
            if info["conflict"]:
                excluded.append(
                    {"id": pair_id, "reason": "同一无序金标对存在冲突 pair_relation：整对剔除，不进入任何指标"}
                )
                continue
            gold_relation = str(info["relation"])
            if gold_relation == "unknown":
                excluded.append({"id": pair_id, "reason": "金标对关系为 unknown：在途标注不进入指标"})
                continue
            if (
                pred.human_relation is not None
                and pred.human_relation != "unknown"
                and pred.human_relation != gold_relation
            ):
                excluded.append(
                    {"id": pair_id, "reason": "预测自报 human_relation 与已校验金标 pair_relation 不符，不进入指标"}
                )
                continue
            relation: str = gold_relation
        else:
            if pred.human_relation is None or pred.human_relation == "unknown":
                excluded.append({"id": pair_id, "reason": "缺少人工金标或为 unknown，不进入指标"})
                continue
            relation = pred.human_relation
        if not pred.system_auto_merged:
            # 未自动合并的对不进入 precision 分母（分母=自动合并对）。
            continue
        groups.setdefault(key, []).append(pair_id)
        existing = relations.get(key)
        if existing is not None and existing != relation:
            relations[key] = "__conflict__"
        else:
            relations[key] = relation
    for key, ids in groups.items():
        relation = relations[key]
        if relation == "__conflict__":
            excluded.append(
                {
                    "id": f"{key[0]}|{key[1]}",
                    "reason": "同一无序对出现冲突的人工金标，整对剔除，不进入任何指标",
                }
            )
            continue
        counted += 1
        if relation == "same_need":
            tp += 1
        else:
            fp += 1
        for dup_id in ids[1:]:
            excluded.append({"id": dup_id, "reason": "重复的无序对（含反序）：同一对只计一次"})
    return ScoredSet(
        counts={
            "merge_precision": MetricCount(numerator=tp, denominator=counted),
            "error_merge_rate": MetricCount(numerator=fp, denominator=counted),
        },
        excluded=excluded,
        eligible_size=len(eligible_sample_ids),
        fingerprint=compute_eligible_fingerprint(
            eligible_sample_ids, dataset_id=dataset_id, split_fingerprint=split_fingerprint
        ),
        gold_fingerprint=gold_fingerprint,
    )


def _extraction_gold_map(
    gold_records: "Iterable[ExtractionAnnotationRecord] | None",
) -> tuple[dict[tuple[str, str], bool], set[tuple[str, str]]]:
    """(sample_id, label_field) -> 金标布尔值；同键冲突值记入冲突集，不采信任何一边。"""
    gold_map: dict[tuple[str, str], bool] = {}
    conflicted: set[tuple[str, str]] = set()
    if gold_records is None:
        return gold_map, conflicted
    for record in gold_records:
        if record.label is None:
            continue
        for field, value in record.label.items():
            if value is None:
                continue  # unknown 是合法在途标注：该键无金标
            key = (record.sample_id, field)
            if key in gold_map and gold_map[key] != value:
                conflicted.add(key)
            elif key not in conflicted:
                gold_map.setdefault(key, value)
    for key in conflicted:
        gold_map.pop(key, None)
    return gold_map, conflicted


def _score_extraction(
    predictions: list[ExtractionPrediction],
    eligible_sample_ids: set[str],
    *,
    dataset_id: str | None = None,
    split_fingerprint: str | None = None,
    gold_records: "Iterable[ExtractionAnnotationRecord] | None" = None,
) -> ScoredSet:
    tp = fp = fn = 0
    excluded: list[dict[str, str]] = []
    keys: dict[tuple[str, str], bool | None] = {}
    order: list[tuple[str, str]] = []
    conflicted: set[tuple[str, str]] = set()
    gold_map, gold_conflicted = _extraction_gold_map(gold_records)
    gold_fingerprint: str | None = None
    if gold_records is not None:
        gold_fingerprint = compute_gold_fingerprint(
            task="extraction",
            entries=sorted(
                _extraction_gold_entry(record)
                for record in gold_records
                if record.has_any_known_label()
            ),
            dataset_id=dataset_id,
            split_fingerprint=split_fingerprint,
        )
    for pred in predictions:
        pred_id = f"{pred.sample_id}|{pred.label_field}"
        if pred.sample_id not in eligible_sample_ids:
            excluded.append({"id": pred.sample_id, "reason": "样本不属于冻结评估集（数据归属错误）"})
            continue
        if gold_records is not None:
            gkey = (pred.sample_id, pred.label_field)
            if gkey in gold_conflicted:
                excluded.append({"id": pred_id, "reason": "同一金标键存在冲突人工标签，整键剔除"})
                continue
            if gkey not in gold_map:
                excluded.append({"id": pred_id, "reason": "该样本该字段无明确人工金标（unknown/缺失），不进入指标"})
                continue
            human = gold_map[gkey]
            if pred.human_label is not None and pred.human_label != human:
                excluded.append(
                    {"id": pred_id, "reason": "预测自报 human_label 与已校验金标不符，不进入指标"}
                )
                continue
        else:
            if pred.human_label is None:
                excluded.append({"id": pred_id, "reason": "无人工标签（unknown），不进入指标"})
                continue
            human = pred.human_label
        key = (pred.sample_id, pred.label_field)
        if key not in keys:
            keys[key] = human
            order.append(key)
        elif keys[key] != human:
            conflicted.add(key)
        elif key not in conflicted:
            excluded.append({"id": pred_id, "reason": "同一样本同一字段的重复预测：只计一次"})
    for key in order:
        if key in conflicted:
            excluded.append(
                {"id": f"{key[0]}|{key[1]}", "reason": "同一预测键出现冲突人工标签，整键剔除"}
            )
            continue
        pred_human = keys[key]
        assert pred_human is not None
        predicted = any(
            p.predicted for p in predictions if (p.sample_id, p.label_field) == key
        )
        if predicted:
            if pred_human:
                tp += 1
            else:
                fp += 1
        elif pred_human:
            fn += 1
    return ScoredSet(
        counts={
            "extraction_precision": MetricCount(numerator=tp, denominator=tp + fp),
            "extraction_recall": MetricCount(numerator=tp, denominator=tp + fn),
        },
        excluded=excluded,
        eligible_size=len(eligible_sample_ids),
        fingerprint=compute_eligible_fingerprint(
            eligible_sample_ids, dataset_id=dataset_id, split_fingerprint=split_fingerprint
        ),
        gold_fingerprint=gold_fingerprint,
    )


def evaluate_merge(
    predictions: list[MergePrediction],
    eligible_sample_ids: set[str],
    *,
    dataset_id: str | None = None,
    split_fingerprint: str | None = None,
    gold_pairs: "Iterable[PairAnnotationRecord] | None" = None,
) -> ScoredSet:
    """合并指标。传入 gold_pairs 时，对错以已校验 PairAnnotationRecord 金标为准，
    预测自报 human_relation 不能替代金标；不传时仅可用于非门禁的 exploratory 打分。"""
    return _score_merges(
        predictions,
        set(eligible_sample_ids),
        dataset_id=dataset_id,
        split_fingerprint=split_fingerprint,
        gold_pairs=gold_pairs,
    )


def evaluate_extraction(
    predictions: list[ExtractionPrediction],
    eligible_sample_ids: set[str],
    *,
    dataset_id: str | None = None,
    split_fingerprint: str | None = None,
    gold_records: "Iterable[ExtractionAnnotationRecord] | None" = None,
) -> ScoredSet:
    """抽取指标。传入 gold_records 时标签以金标为准并绑定指纹；否则仅非门禁打分。"""
    return _score_extraction(
        predictions,
        set(eligible_sample_ids),
        dataset_id=dataset_id,
        split_fingerprint=split_fingerprint,
        gold_records=gold_records,
    )


class ExperimentSpec(StrictModel):
    """预声明实验规格：在评估任何臂之前构造并校验，冻结不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: str = EXPERIMENT_SPEC_VERSION
    dataset_id: str = Field(min_length=1)
    split_fingerprint: str | None = None
    eligible_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_names: tuple[str, ...]
    pre_declared_improvements: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "ExperimentSpec":
        if self.spec_version != EXPERIMENT_SPEC_VERSION:
            raise ValueError(f"spec_version 必须是 {EXPERIMENT_SPEC_VERSION}")
        if not self.metric_names:
            raise ValueError("实验 Spec 至少要声明一个指标")
        if len(set(self.metric_names)) != len(self.metric_names):
            raise ValueError("实验 Spec 指标名重复")
        unregistered = [m for m in self.metric_names if m not in METRIC_DIRECTIONS]
        if unregistered:
            raise ValueError(f"指标 {unregistered} 未注册方向，预声明实验必须使用有明确方向的指标")
        extra = set(self.pre_declared_improvements) - set(self.metric_names)
        if extra:
            raise ValueError(f"预声明改善指标必须先在指标清单中声明：{sorted(extra)}")
        return self


def build_experiment_spec(
    *,
    dataset_id: str,
    eligible_sample_ids: Iterable[str],
    metric_names: list[str],
    pre_declared_improvements: list[str],
    split_fingerprint: str | None = None,
) -> ExperimentSpec:
    return ExperimentSpec(
        dataset_id=dataset_id,
        split_fingerprint=split_fingerprint,
        eligible_fingerprint=compute_eligible_fingerprint(
            eligible_sample_ids, dataset_id=dataset_id, split_fingerprint=split_fingerprint
        ),
        metric_names=tuple(metric_names),
        pre_declared_improvements=tuple(pre_declared_improvements),
    )


def _not_comparable(reason: str, pre_declared: list[str]) -> dict[str, object]:
    return {
        "comparable": False,
        "reason": reason,
        "metrics": {},
        "pre_declared_improvements": pre_declared,
        "improvements_met": [],
    }


def compare_arms(
    baseline: ScoredSet,
    enhanced: ScoredSet,
    metric_names: list[str],
    pre_declared_improvements: "ExperimentSpec | list[str] | None" = None,
) -> dict[str, object]:
    """两臂比较必须在同一冻结 eligible 集合上（按指纹验证，不是按长度）。"""
    spec: ExperimentSpec | None = None
    if isinstance(pre_declared_improvements, ExperimentSpec):
        spec = pre_declared_improvements
        pre_names = list(spec.pre_declared_improvements)
        if list(metric_names) != list(spec.metric_names):
            raise ValueError("比较指标清单与预声明实验 Spec 不一致，禁止事后改清单")
    elif pre_declared_improvements is None:
        pre_names = []
    else:
        pre_names = list(pre_declared_improvements)

    if baseline.eligible_size != enhanced.eligible_size:
        return _not_comparable(
            f"eligible 集不一致：baseline={baseline.eligible_size}, "
            f"enhanced={enhanced.eligible_size}，禁止跨数据集比较",
            pre_names,
        )
    if baseline.fingerprint is None or enhanced.fingerprint is None:
        return _not_comparable(
            "至少一臂缺少冻结 eligible 指纹，无法验证是同一实际 eligible 集，禁止跨数据集比较",
            pre_names,
        )
    if baseline.fingerprint != enhanced.fingerprint:
        return _not_comparable(
            "两臂 eligible 指纹不一致：长度相同不代表是同一冻结集，禁止跨数据集比较",
            pre_names,
        )
    if spec is not None and baseline.fingerprint != spec.eligible_fingerprint:
        return _not_comparable(
            "两臂打分集与预声明实验 Spec 的 eligible 指纹不一致，禁止跨数据集比较",
            pre_names,
        )
    if baseline.gold_fingerprint is not None and enhanced.gold_fingerprint is None:
        return _not_comparable("一臂绑定了金标集而另一臂没有：禁止与自报标签的打分混比", pre_names)
    if enhanced.gold_fingerprint is not None and baseline.gold_fingerprint is None:
        return _not_comparable("一臂绑定了金标集而另一臂没有：禁止与自报标签的打分混比", pre_names)
    if (
        baseline.gold_fingerprint is not None
        and enhanced.gold_fingerprint is not None
        and baseline.gold_fingerprint != enhanced.gold_fingerprint
    ):
        return _not_comparable("两臂绑定的金标集指纹不一致：禁止换金标比较", pre_names)
    if pre_names and not set(pre_names) <= set(metric_names):
        raise ValueError("预声明改善指标必须先在指标清单中声明，不能事后补")

    diffs: dict[str, object] = {}
    met: list[str] = []
    for name in metric_names:
        direction = METRIC_DIRECTIONS.get(name)
        if direction is None:
            raise ValueError(
                f"指标 {name} 没有注册方向，拒绝比较；请先调用 register_metric_direction"
            )
        base_count = baseline.counts.get(name)
        enh_count = enhanced.counts.get(name)
        base_val = base_count.value if base_count else None
        enh_val = enh_count.value if enh_count else None
        if base_val is None or enh_val is None:
            improved: bool | None = None
        elif direction == "higher":
            improved = enh_val > base_val
        else:
            improved = enh_val < base_val
        diffs[name] = {
            "direction": direction,
            "baseline": base_val,
            "enhanced": enh_val,
            "improved": improved,
        }
        if improved and name in pre_names:
            met.append(name)
    return {
        "comparable": True,
        "reason": None,
        "eligible_fingerprint": baseline.fingerprint,
        "metrics": diffs,
        "pre_declared_improvements": pre_names,
        "improvements_met": met,
    }


class GateInput(StrictModel):
    """门禁输入：human heldout 资格与数量只能由已校验标注记录 + 切分
    manifest 推导；不接受自报来源字段，也不接受事后拼装的 comparison 字典。"""

    spec: ExperimentSpec
    baseline: ScoredSet
    enhanced: ScoredSet
    annotation_records: list[ExtractionAnnotationRecord | PairAnnotationRecord] = Field(
        default_factory=list
    )
    heldout_manifest: SplitResult | None = None


def _arm_metric_denominator(arm: ScoredSet, names: tuple[str, ...]) -> int:
    total = 0
    for name in names:
        count = arm.counts.get(name)
        if count is not None:
            total += count.denominator
    return total


def evaluate_gate(gate: GateInput) -> dict[str, object]:
    """G2 质量门禁。缺失合法工件 -> NOT_EVALUATED；实质不达标 -> FAIL；
    全部条件满足且预声明指标在已校验金标上真实改善才 PASS；合成数据永不 PASS。"""
    reasons: list[str] = []
    pending: list[str] = []

    records = gate.annotation_records
    manifest = gate.heldout_manifest
    spec = gate.spec
    heldout_ids: list[str] = []
    human_total = 0
    pair_gold_count = 0
    merge_gold_fp: str | None = None
    extraction_gold_fp: str | None = None
    manifest_consistent = False

    if not records:
        pending.append("未提供任何已校验标注记录：缺少人工金标工件，门禁无法评估")
    if manifest is None:
        pending.append("未提供切分 manifest 工件：无法推导 dev/heldout 归属")

    if records and manifest is not None:
        if manifest.version != SPLIT_VERSION:
            reasons.append(f"切分 manifest 版本 {manifest.version!r} 不是 {SPLIT_VERSION}，指纹不可信")
        dev_ids = set(manifest.dev_sample_ids)
        held_ids = set(manifest.heldout_sample_ids)

        # 数据集身份：Spec 必须与记录实际来源数据集一致，不采信自报计数。
        record_dataset_ids = {record.provenance.dataset_id for record in records}
        if record_dataset_ids != {spec.dataset_id}:
            reasons.append(
                f"标注记录数据集 {sorted(record_dataset_ids)} 与实验 Spec dataset_id "
                f"{spec.dataset_id!r} 不一致：数据集身份不可信"
            )

        # 显式样本清单：由不可变记录元数据构建；重复/冲突标注拒绝。
        sample_meta: dict[str, tuple[str, str]] = {}
        meta_conflicts: set[str] = set()
        extraction_by_sample: dict[str, ExtractionAnnotationRecord] = {}
        label_conflicts: set[str] = set()
        duplicate_annotation_ids: set[str] = set()
        for record in records:
            meta = (record.source_id, record.family_id)
            if record.sample_id in sample_meta and sample_meta[record.sample_id] != meta:
                meta_conflicts.add(record.sample_id)
            sample_meta[record.sample_id] = meta
            if isinstance(record, ExtractionAnnotationRecord):
                prev = extraction_by_sample.get(record.sample_id)
                if prev is not None:
                    if prev.label != record.label:
                        label_conflicts.add(record.sample_id)
                    else:
                        duplicate_annotation_ids.add(record.sample_id)
                extraction_by_sample.setdefault(record.sample_id, record)
        if meta_conflicts:
            reasons.append(f"同一样本 ID 出现冲突的来源/家族元数据：{sorted(meta_conflicts)}")
        if label_conflicts:
            reasons.append(f"同一样本出现重复且冲突的抽取标注：{sorted(label_conflicts)}")
        if duplicate_annotation_ids:
            reasons.append(f"重复的抽取标注 ID（同一标注被提交多次）：{sorted(duplicate_annotation_ids)}")

        # 用实际记录元数据整体重建并复核 SplitResult（泄漏/篡改/成员完整性）。
        items = [
            SplitItem(sample_id=sid, source_id=src, family_id=fam)
            for sid, (src, fam) in sorted(sample_meta.items())
        ]
        try:
            validate_split(manifest, items)
            manifest_consistent = True
        except ValueError as error:
            reasons.append(f"切分 manifest 未通过与实际记录元数据的整体复核：{error}")

        status = human_dataset_status(list(records))
        human_total = status.human_reviewed_count
        if human_total == 0:
            reasons.append(
                "标注记录中没有人工金标（合成/模型来源或全 unknown 的在途标注不能充当 human 门禁数据）"
            )
        elif status.status != "MET":
            reasons.extend(status.reasons)
        pool: set[str] = set()
        for record in records:
            if not record.is_human_gold():
                continue
            pool.add(record.sample_id)
            if isinstance(record, PairAnnotationRecord):
                pool.add(record.right_sample_id)
        heldout_ids = sorted(pool & held_ids)
        if 0 < len(heldout_ids) < HELDOUT_HUMAN_MIN:
            reasons.append(
                f"heldout 分区人工金标样本 {len(heldout_ids)} 条 < 实验下限 {HELDOUT_HUMAN_MIN} 条"
                "（计划口径为总量 200-300 条按组切分，不在总量之上叠加 200 heldout）"
            )
        elif len(heldout_ids) == 0 and not (dev_ids & pool):
            reasons.append("人工金标样本没有落入任何分区：切分与记录不一致")

        # 金标祝福：合并指标必须由覆盖评估对的 PairAnnotationRecord 人工金标支撑。
        gold_pairs = [
            record for record in records
            if isinstance(record, PairAnnotationRecord) and record.is_human_gold()
        ]
        pair_gold_count = len(gold_pairs)
        merge_declared = any(name in spec.metric_names for name in MERGE_METRIC_NAMES)
        unknown_endpoints: set[str] = set()
        for record in gold_pairs:
            for endpoint in (record.sample_id, record.right_sample_id):
                if endpoint not in sample_meta:
                    unknown_endpoints.add(endpoint)
        if unknown_endpoints:
            pending.append(
                f"金标语义对端点缺少显式样本元数据记录：{sorted(unknown_endpoints)}，拒绝评估不猜测"
            )
        bound_gold_pairs = [
            record
            for record in gold_pairs
            if record.sample_id in sample_meta and record.right_sample_id in sample_meta
        ]
        if merge_declared and not bound_gold_pairs:
            reasons.append(
                "声明了合并指标但没有任何端点元数据齐备的 PairAnnotationRecord 人工金标："
                "抽取金标与预测自报 human_relation 都不能祝福合并质量"
            )
        gold_extractions = [
            record for record in records
            if isinstance(record, ExtractionAnnotationRecord) and record.is_human_gold()
        ]
        if manifest_consistent:
            if bound_gold_pairs:
                merge_gold_fp = compute_gold_fingerprint(
                    task="merge",
                    entries=sorted({_pair_gold_entry(record) for record in bound_gold_pairs}),
                    dataset_id=spec.dataset_id,
                    split_fingerprint=manifest.fingerprint,
                )
            if gold_extractions:
                extraction_gold_fp = compute_gold_fingerprint(
                    task="extraction",
                    entries=sorted({_extraction_gold_entry(record) for record in gold_extractions}),
                    dataset_id=spec.dataset_id,
                    split_fingerprint=manifest.fingerprint,
                )

        if human_total > 0 and manifest_consistent and heldout_ids:
            expected_fp = compute_eligible_fingerprint(
                heldout_ids, dataset_id=spec.dataset_id, split_fingerprint=manifest.fingerprint
            )
            if spec.eligible_fingerprint != expected_fp:
                reasons.append(
                    "实验 Spec 的 eligible 指纹与标注记录+切分 manifest 推导结果不一致：疑似事后换 Spec"
                )
            for arm_name, arm in (("baseline", gate.baseline), ("enhanced", gate.enhanced)):
                if arm.fingerprint != expected_fp:
                    reasons.append(
                        f"{arm_name} 臂打分集指纹与推导出的冻结 heldout 集不一致：禁止换臂/归属错误比较"
                    )

        # 打分与金标绑定：任何进入指标的合并/抽取计数都必须绑定当前金标集。
        for arm_name, arm in (("baseline", gate.baseline), ("enhanced", gate.enhanced)):
            if _arm_metric_denominator(arm, MERGE_METRIC_NAMES) > 0:
                if merge_gold_fp is None:
                    reasons.append(
                        f"{arm_name} 臂携带合并计数但未绑定已校验金标集："
                        "预测自报 human_relation 不能替代 PairAnnotationRecord 金标"
                    )
                elif arm.gold_fingerprint != merge_gold_fp:
                    reasons.append(
                        f"{arm_name} 臂金标绑定指纹与当前人工金标集不一致："
                        "打分后金标/记录身份被改动，旧 PASS 不可延续"
                    )
            if _arm_metric_denominator(arm, EXTRACTION_METRIC_NAMES) > 0:
                if extraction_gold_fp is None:
                    reasons.append(f"{arm_name} 臂携带抽取计数但未绑定人工抽取金标集")
                elif arm.gold_fingerprint != extraction_gold_fp:
                    reasons.append(
                        f"{arm_name} 臂抽取金标绑定指纹与当前人工抽取金标集不一致：旧 PASS 不可延续"
                    )
            if (
                _arm_metric_denominator(arm, MERGE_METRIC_NAMES) > 0
                and _arm_metric_denominator(arm, EXTRACTION_METRIC_NAMES) > 0
            ):
                reasons.append(
                    f"{arm_name} 臂同时携带合并与抽取计数：单一金标绑定无法同时祝福两类指标，请分开打分"
                )

        # 计数一致性/有限区间：绝不采信明显矛盾的分子分母。
        for arm_name, arm in (("baseline", gate.baseline), ("enhanced", gate.enhanced)):
            for metric_name, count in arm.counts.items():
                if count.denominator > 0 and count.numerator > count.denominator:
                    reasons.append(
                        f"{arm_name} 臂 {metric_name} 分子 {count.numerator} > 分母 {count.denominator}：计数矛盾"
                    )
                value = count.value
                if value is not None and not 0.0 <= value <= 1.0:
                    reasons.append(f"{arm_name} 臂 {metric_name}={value} 超出 [0,1] 有限区间")
            mp = arm.counts.get("merge_precision")
            emr = arm.counts.get("error_merge_rate")
            if mp is not None and emr is not None and mp.denominator != emr.denominator:
                reasons.append(
                    f"{arm_name} 臂 merge_precision 与 error_merge_rate 分母不一致："
                    f"{mp.denominator} vs {emr.denominator}"
                )

    metrics_known = True
    precision_count = gate.enhanced.counts.get("merge_precision")
    error_count = gate.enhanced.counts.get("error_merge_rate")
    precision = precision_count.value if precision_count else None
    error_rate = error_count.value if error_count else None
    if precision is None or error_rate is None:
        pending.append("自动合并分母为 0 或指标缺失：unknown，不能判 PASS")
        metrics_known = False
    else:
        if precision < MERGE_PRECISION_MIN:
            reasons.append(f"merge_precision={precision:.4f} < {MERGE_PRECISION_MIN}")
        if error_rate > ERROR_MERGE_RATE_MAX:
            reasons.append(f"error_merge_rate={error_rate:.4f} > {ERROR_MERGE_RATE_MAX}")

    comparison: dict[str, object] | None = None
    if records and manifest is not None and human_total > 0 and metrics_known:
        comparison = compare_arms(gate.baseline, gate.enhanced, list(spec.metric_names), spec)
        if comparison["comparable"] is not True:
            reasons.append(f"两臂比较不可执行：{comparison['reason']}")
        elif not comparison["improvements_met"]:
            reasons.append("没有任何预声明指标在冻结 heldout 上按方向改善")

    if reasons:
        decision: GateDecision = "FAIL"
    elif pending:
        decision = "NOT_EVALUATED"
    else:
        decision = "PASS"
    return {
        "version": METRICS_REPORT_VERSION,
        "decision": decision,
        "reasons": reasons + pending,
        "thresholds": {
            "merge_precision_min": MERGE_PRECISION_MIN,
            "error_merge_rate_max": ERROR_MERGE_RATE_MAX,
            "human_total_min": REQUIRED_HUMAN_DATASET_MIN,
            "heldout_human_min": HELDOUT_HUMAN_MIN,
        },
        "counts": {name: count.as_dict() for name, count in gate.enhanced.counts.items()},
        "heldout": {
            "human_total": human_total,
            "heldout_eligible_size": len(heldout_ids),
            "derived_from": "validated annotation_records + split manifest revalidated against record metadata",
        },
        "gold": {
            "merge_pair_gold_records": pair_gold_count,
            "merge_gold_bound": merge_gold_fp is not None,
            "gold_binding_version": GOLD_BINDING_VERSION,
        },
        "comparison": comparison,
    }


def metrics_skeleton() -> dict[str, object]:
    """evaluation/metrics.json 骨架：所有指标 unknown/null，状态 NOT_EVALUATED。

    数值只能由真实人工标注集评估后填入；禁止预填好看的结果。
    """
    zero = MetricCount(numerator=0, denominator=0)
    return {
        "version": METRICS_REPORT_VERSION,
        "status": "NOT_EVALUATED",
        "generated_by": "xhs_discovery.evaluation.metrics.metrics_skeleton",
        "human_dataset": {
            "required_min": REQUIRED_HUMAN_DATASET_MIN,
            "required_max": 300,
            "provided": False,
            "status": "NOT_MET",
        },
        "merge": {
            name: zero.as_dict()
            for name in ("merge_precision", "error_merge_rate")
        },
        "extraction": {
            name: zero.as_dict()
            for name in ("extraction_precision", "extraction_recall")
        },
        "baseline_vs_enhanced": {"comparable": False, "reason": "尚无冻结 eligible 集与两臂预测"},
        "gate": {"decision": "NOT_EVALUATED", "reasons": ["真实人工标注与模型预测均未提供"]},
    }


def write_metrics_skeleton(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics_skeleton(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "METRICS_REPORT_VERSION",
    "FINGERPRINT_VERSION",
    "GOLD_BINDING_VERSION",
    "EXPERIMENT_SPEC_VERSION",
    "MERGE_PRECISION_MIN",
    "ERROR_MERGE_RATE_MAX",
    "HELDOUT_HUMAN_MIN",
    "METRIC_DIRECTIONS",
    "GateDecision",
    "MetricCount",
    "MergePrediction",
    "ExtractionPrediction",
    "ScoredSet",
    "EvaluationReport",
    "ExperimentSpec",
    "GateInput",
    "compute_eligible_fingerprint",
    "compute_gold_fingerprint",
    "register_metric_direction",
    "build_experiment_spec",
    "evaluate_merge",
    "evaluate_extraction",
    "compare_arms",
    "evaluate_gate",
    "metrics_skeleton",
    "write_metrics_skeleton",
]
