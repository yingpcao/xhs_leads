"""指标与门禁测试：零分母 unknown、坏标签剔除有原因、归属错误拒绝、
合成来源不能 PASS、两臂只在同一冻结集（按指纹验证）比较、指标有显式方向、
预声明改善由不可变 Spec 承载、门禁从标注记录+切分 manifest 推导 heldout，
评估器绝不执行合并。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from xhs_discovery.evaluation.annotations import (
    ANNOTATION_SCHEMA_VERSION,
    EXTRACTION_LABEL_FIELDS,
    AnnotationProvenance,
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
)
from xhs_discovery.evaluation.splits import SplitItem, group_split
from xhs_discovery.evaluation.metrics import (
    ERROR_MERGE_RATE_MAX,
    HELDOUT_HUMAN_MIN,
    MERGE_PRECISION_MIN,
    EvaluationReport,
    ExperimentSpec,
    ExtractionPrediction,
    GateInput,
    MergePrediction,
    MetricCount,
    ScoredSet,
    build_experiment_spec,
    compare_arms,
    compute_eligible_fingerprint,
    evaluate_extraction,
    evaluate_gate,
    evaluate_merge,
    metrics_skeleton,
    register_metric_direction,
    write_metrics_skeleton,
)
from xhs_discovery.models.common import sha256_text


def _merge(left: str, right: str, *, auto: bool, human, evidence=None) -> MergePrediction:
    return MergePrediction(
        pair_left_sample_id=left,
        pair_right_sample_id=right,
        system_auto_merged=auto,
        human_relation=human,
        evidence_sample_ids=evidence or [left, right],
    )


class TestMetricCount:
    def test_zero_denominator_is_unknown_not_zero(self):
        count = MetricCount(numerator=0, denominator=0)
        assert count.value is None
        payload = count.as_dict()
        assert payload["value"] is None
        assert json.dumps(payload)  # 可序列化，unknown 保持 null

    def test_explicit_numerator_denominator(self):
        count = MetricCount(numerator=19, denominator=20)
        assert count.value == pytest.approx(0.95)


class TestMergeMetrics:
    def test_denominator_is_auto_merges_only(self):
        preds = [
            _merge("a", "b", auto=True, human="same_need"),
            _merge("c", "d", auto=True, human="different_need"),
            _merge("e", "f", auto=False, human="same_need"),  # 未自动合并不进分母
        ]
        scored = evaluate_merge(preds, {"a", "b", "c", "d", "e", "f"})
        assert scored.counts["merge_precision"].denominator == 2
        assert scored.counts["merge_precision"].numerator == 1
        assert scored.counts["error_merge_rate"].value == pytest.approx(0.5)

    def test_missing_or_unknown_labels_excluded_with_reason(self):
        preds = [
            _merge("a", "b", auto=True, human=None),
            _merge("c", "d", auto=True, human="unknown"),
        ]
        scored = evaluate_merge(preds, {"a", "b", "c", "d"})
        assert scored.counts["merge_precision"].denominator == 0
        assert scored.counts["merge_precision"].value is None
        assert len(scored.excluded) == 2
        assert all("人工" in e["reason"] for e in scored.excluded)

    def test_wrong_evidence_ownership_excluded(self):
        preds = [_merge("a", "b", auto=True, human="same_need", evidence=["a", "ghost"])]
        scored = evaluate_merge(preds, {"a", "b"})
        assert scored.counts["merge_precision"].denominator == 0
        assert "ghost" in scored.excluded[0]["reason"]

    def test_evaluator_performs_no_merge(self):
        # 评估器是纯函数：同输入两次调用结果一致，且没有合并动作可观察。
        preds = [_merge("a", "b", auto=True, human="same_need")]
        eligible = {"a", "b"}
        first = evaluate_merge(preds, eligible)
        second = evaluate_merge(preds, eligible)
        assert first.model_dump() == second.model_dump()
        assert eligible == {"a", "b"}  # 输入集合未被改动


class TestExtractionMetrics:
    def test_zero_denominator_unknown(self):
        scored = evaluate_extraction([], {"a"})
        assert scored.counts["extraction_precision"].value is None
        assert scored.counts["extraction_recall"].value is None

    def test_foreign_sample_excluded(self):
        pred = ExtractionPrediction(
            sample_id="x", label_field="explicit_problem", predicted=True, human_label=True
        )
        scored = evaluate_extraction([pred], {"a"})
        assert scored.counts["extraction_precision"].denominator == 0
        assert scored.excluded[0]["id"] == "x"

    def test_basic_precision_recall(self):
        preds = [
            ExtractionPrediction(sample_id="a", label_field="f", predicted=True, human_label=True),
            ExtractionPrediction(sample_id="b", label_field="f", predicted=True, human_label=False),
            ExtractionPrediction(sample_id="c", label_field="f", predicted=False, human_label=True),
        ]
        scored = evaluate_extraction(preds, {"a", "b", "c"})
        assert scored.counts["extraction_precision"].value == pytest.approx(0.5)
        assert scored.counts["extraction_recall"].value == pytest.approx(0.5)


def _scored(prec_num: int, prec_den: int, *, size: int = 200, fingerprint: str | None = "dataset-A") -> ScoredSet:
    err_num = prec_den - prec_num  # error = 1 - precision 的互补计数
    return ScoredSet(
        counts={
            "merge_precision": MetricCount(numerator=prec_num, denominator=prec_den),
            "error_merge_rate": MetricCount(numerator=err_num, denominator=prec_den),
        },
        eligible_size=size,
        fingerprint=fingerprint,
    )


class TestCompareArms:
    def test_different_frozen_sets_not_comparable(self):
        baseline = _scored(10, 12, size=200, fingerprint="dataset-A")
        enhanced = _scored(11, 12, size=250, fingerprint="dataset-B")
        result = compare_arms(baseline, enhanced, ["merge_precision"], ["merge_precision"])
        assert result["comparable"] is False
        assert "禁止跨数据集比较" in result["reason"]

    def test_equal_size_different_sets_not_comparable(self):
        # 同长度但不同实例集：必须按指纹拒绝，而不是按长度放行。
        ids_a = {f"a{i}" for i in range(40)}
        ids_b = {f"b{i}" for i in range(40)}
        baseline = evaluate_merge([_merge("a0", "a1", auto=True, human="same_need")], ids_a)
        enhanced = evaluate_merge([_merge("b0", "b1", auto=True, human="same_need")], ids_b)
        assert baseline.eligible_size == enhanced.eligible_size
        result = compare_arms(baseline, enhanced, ["merge_precision"], ["merge_precision"])
        assert result["comparable"] is False
        assert "指纹" in result["reason"]

    def test_missing_fingerprint_not_comparable(self):
        baseline = _scored(10, 12, fingerprint=None)
        enhanced = _scored(11, 12, fingerprint=None)
        result = compare_arms(baseline, enhanced, ["merge_precision"], [])
        assert result["comparable"] is False
        assert "指纹" in result["reason"]

    def test_reordered_ids_yield_same_fingerprint(self):
        ids = ["z1", "z2", "z3"]
        fp1 = compute_eligible_fingerprint(ids)
        fp2 = compute_eligible_fingerprint(list(reversed(ids)))
        assert fp1 == fp2
        scored_a = evaluate_merge([], set(ids))
        scored_b = evaluate_merge([], {"z3", "z2", "z1"})
        assert scored_a.fingerprint == scored_b.fingerprint

    def test_fingerprint_binds_dataset_and_split_provenance(self):
        ids = {"s1", "s2"}
        fp_plain = compute_eligible_fingerprint(ids)
        fp_ds = compute_eligible_fingerprint(ids, dataset_id="d1")
        fp_split = compute_eligible_fingerprint(ids, dataset_id="d1", split_fingerprint="sp1")
        assert len({fp_plain, fp_ds, fp_split}) == 3

    def test_predeclared_must_be_declared_metric(self):
        same = _scored(10, 12)
        with pytest.raises(ValueError, match="预声明"):
            compare_arms(same, same, ["merge_precision"], ["recall_of_dreams"])

    def test_improvement_detected_on_same_set(self):
        baseline = _scored(10, 12)
        enhanced = _scored(11, 12)
        result = compare_arms(baseline, enhanced, ["merge_precision"], ["merge_precision"])
        assert result["comparable"] is True
        assert result["improvements_met"] == ["merge_precision"]


class TestMetricDirection:
    def test_error_rate_increase_is_not_improvement(self):
        baseline = _scored(20, 20)  # error 0/20
        enhanced = _scored(19, 20)  # error 1/20 上升
        result = compare_arms(baseline, enhanced, ["error_merge_rate"], ["error_merge_rate"])
        assert result["comparable"] is True
        assert result["improvements_met"] == []
        assert result["metrics"]["error_merge_rate"]["direction"] == "lower"
        assert result["metrics"]["error_merge_rate"]["improved"] is False

    def test_error_rate_decrease_is_improvement(self):
        baseline = _scored(18, 20)  # error 2/20
        enhanced = _scored(19, 20)  # error 1/20 下降
        result = compare_arms(baseline, enhanced, ["error_merge_rate"], ["error_merge_rate"])
        assert result["improvements_met"] == ["error_merge_rate"]

    def test_unknown_metric_name_rejected(self):
        same = _scored(10, 12)
        with pytest.raises(ValueError, match="方向"):
            compare_arms(same, same, ["vibes_score"], [])

    def test_registered_direction_enables_comparison(self):
        register_metric_direction("unit_cost_rate", "lower")
        baseline = ScoredSet(
            counts={"unit_cost_rate": MetricCount(numerator=9, denominator=10)},
            eligible_size=2,
            fingerprint="fp",
        )
        enhanced = ScoredSet(
            counts={"unit_cost_rate": MetricCount(numerator=4, denominator=10)},
            eligible_size=2,
            fingerprint="fp",
        )
        result = compare_arms(baseline, enhanced, ["unit_cost_rate"], ["unit_cost_rate"])
        assert result["improvements_met"] == ["unit_cost_rate"]
        with pytest.raises(ValueError, match="方向"):
            register_metric_direction("bad_dir", "sideways")  # type: ignore[arg-type]


class TestExperimentSpec:
    def test_spec_is_frozen(self):
        spec = build_experiment_spec(
            dataset_id="d",
            eligible_sample_ids={"a", "b"},
            metric_names=["merge_precision"],
            pre_declared_improvements=["merge_precision"],
        )
        with pytest.raises(ValidationError):
            spec.pre_declared_improvements = ("error_merge_rate",)

    def test_spec_rejects_unregistered_direction(self):
        with pytest.raises(ValueError, match="方向"):
            build_experiment_spec(
                dataset_id="d",
                eligible_sample_ids={"a"},
                metric_names=["dream_metric"],
                pre_declared_improvements=[],
            )

    def test_spec_rejects_posthoc_blessing(self):
        with pytest.raises(ValueError, match="预声明"):
            build_experiment_spec(
                dataset_id="d",
                eligible_sample_ids={"a"},
                metric_names=["merge_precision"],
                pre_declared_improvements=["error_merge_rate"],
            )

    def test_compare_arms_rejects_arms_outside_spec(self):
        ids = {f"p{i}" for i in range(10)}
        other = {f"q{i}" for i in range(10)}
        spec = build_experiment_spec(
            dataset_id="d",
            eligible_sample_ids=ids,
            metric_names=["merge_precision"],
            pre_declared_improvements=["merge_precision"],
        )
        arm = evaluate_merge([_merge("q0", "q1", auto=True, human="same_need")], other)
        result = compare_arms(arm, arm, list(spec.metric_names), spec)
        assert result["comparable"] is False
        assert "Spec" in result["reason"]


class TestPairBoundaries:
    def test_self_pair_rejected_with_reason(self):
        scored = evaluate_merge(
            [MergePrediction(pair_left_sample_id="a", pair_right_sample_id="a",
                             system_auto_merged=True, human_relation="same_need")],
            {"a", "b"},
        )
        assert scored.counts["merge_precision"].denominator == 0
        assert "自配对" in scored.excluded[0]["reason"]

    def test_foreign_endpoints_never_counted(self):
        scored = evaluate_merge(
            [MergePrediction(pair_left_sample_id="foreign1", pair_right_sample_id="foreign2",
                             system_auto_merged=True, human_relation="same_need")],
            {"a", "b"},
        )
        assert scored.counts["merge_precision"].denominator == 0
        assert "端点" in scored.excluded[0]["reason"]

    def test_half_foreign_pair_excluded(self):
        scored = evaluate_merge(
            [_merge("a", "ghost", auto=True, human="same_need", evidence=[])],
            {"a", "b"},
        )
        assert scored.counts["merge_precision"].denominator == 0
        assert "ghost" in scored.excluded[0]["reason"]

    def test_duplicate_unordered_pair_counted_once(self):
        preds = [
            _merge("a", "b", auto=True, human="same_need", evidence=[]),
            _merge("a", "b", auto=True, human="same_need", evidence=[]),
            _merge("b", "a", auto=True, human="same_need", evidence=[]),  # 反序也算同一对
        ]
        scored = evaluate_merge(preds, {"a", "b"})
        assert scored.counts["merge_precision"].denominator == 1
        assert scored.counts["merge_precision"].numerator == 1
        assert len(scored.excluded) == 2
        assert all("重复" in e["reason"] for e in scored.excluded)

    def test_conflicting_duplicate_pair_excluded_entirely(self):
        preds = [
            _merge("a", "b", auto=True, human="same_need", evidence=[]),
            _merge("b", "a", auto=True, human="different_need", evidence=[]),
        ]
        scored = evaluate_merge(preds, {"a", "b"})
        assert scored.counts["merge_precision"].denominator == 0
        assert scored.counts["error_merge_rate"].denominator == 0
        conflict = [e for e in scored.excluded if "冲突" in e["reason"]]
        assert len(conflict) == 1
        blob = json.dumps(scored.model_dump(), ensure_ascii=False)
        assert "same_need" not in blob and "different_need" not in blob  # 冲突标签不泄漏
        # 冲突标签不得泄漏进任何计数
        assert all(c.denominator == 0 for c in scored.counts.values())


class TestExtractionBoundaries:
    def _pred(self, sample_id, predicted, human):
        return ExtractionPrediction(
            sample_id=sample_id, label_field="f", predicted=predicted, human_label=human
        )

    def test_duplicate_prediction_counted_once(self):
        scored = evaluate_extraction([self._pred("a", True, True), self._pred("a", True, True)], {"a"})
        assert scored.counts["extraction_precision"].denominator == 1
        assert any("重复" in e["reason"] for e in scored.excluded)

    def test_conflicting_human_labels_excluded(self):
        scored = evaluate_extraction([self._pred("a", True, True), self._pred("a", True, False)], {"a"})
        assert scored.counts["extraction_precision"].denominator == 0
        assert any("冲突" in e["reason"] for e in scored.excluded)


def _human_provenance() -> AnnotationProvenance:
    return AnnotationProvenance(
        source="human",
        dataset_id=GATE_DATASET_ID,
        producer_id="annotator-gate-unit",
        produced_at=None,
        synthetic=False,
    )


def _synthetic_provenance() -> AnnotationProvenance:
    return AnnotationProvenance(
        source="synthetic_fixture",
        dataset_id=GATE_DATASET_ID,
        producer_id="synthetic:gate-unit",
        produced_at=None,
        synthetic=True,
    )


def _records(n: int, provenance: AnnotationProvenance, *, all_unknown_labels: bool = False) -> list[ExtractionAnnotationRecord]:
    """模拟人工契约取值的合成 UNIT 记录（非真实收集的人工标注）。

    默认携带明确的抽取标签；all_unknown_labels=True 生成合法在途标注（全 unknown），
    按契约不构成金标。
    """
    records = []
    for i in range(n):
        label = {name: None for name in EXTRACTION_LABEL_FIELDS}
        if not all_unknown_labels:
            label["explicit_problem"] = i % 2 == 0
            label["region"] = True
        records.append(
            ExtractionAnnotationRecord(
                schema_version=ANNOTATION_SCHEMA_VERSION,
                sample_id=f"h{i}",
                source_id=f"hsrc{i}",
                family_id=f"hfam{i}",
                content_sha256=sha256_text(f"gate-{i}"),
                text=f"gate-{i}",
                provenance=provenance,
                reviewer_state="reviewed",
                reviewer_id="gate-reviewer" if provenance.source == "human" else "synthetic-reviewer",
                label=label,
            )
        )
    return records


GATE_DATASET_ID = "p2-gate-unit-ds"


def _gate_env(n: int = 200, *, synthetic_labels: bool = False, all_unknown_labels: bool = False):
    """构造合法的门禁环境：n 条带明确标签的模拟人工金标 + 分组切分 + 绑定语义对金标的两臂打分。

    仅为单元测试的 UNIT 数据（模拟人工契约取值），不是真实收集的人工标注。
    金标关系：第 i 对 same_need 当且仅当 i < 可配对数-2；最后两对为 different_need，
    多合并且错误臂因此受罚。
    """
    provenance = _synthetic_provenance() if synthetic_labels else _human_provenance()
    records = _records(n, provenance, all_unknown_labels=all_unknown_labels)
    split = group_split(
        [SplitItem(sample_id=f"h{i}", source_id=f"hsrc{i}", family_id=f"hfam{i}") for i in range(n)]
    )
    pool = {r.sample_id for r in records if r.is_human_gold()}
    heldout = sorted(pool & set(split.heldout_sample_ids))
    n_pairs = min(20, len(heldout) // 2)
    gold_same_until = max(n_pairs - 2, 0)
    meta = {r.sample_id: r for r in records}
    pair_ids = heldout[: n_pairs * 2]
    pair_gold = [
        PairAnnotationRecord(
            schema_version=ANNOTATION_SCHEMA_VERSION,
            sample_id=pair_ids[2 * i],
            right_sample_id=pair_ids[2 * i + 1],
            source_id=meta[pair_ids[2 * i]].source_id,
            family_id=meta[pair_ids[2 * i]].family_id,
            content_sha256=meta[pair_ids[2 * i]].content_sha256,
            text=meta[pair_ids[2 * i]].text,
            provenance=provenance,
            reviewer_state="reviewed",
            reviewer_id="gate-reviewer" if provenance.source == "human" else "synthetic-reviewer",
            label=None,
            pair_relation="same_need" if i < gold_same_until else "different_need",
        )
        for i in range(n_pairs)
    ]

    def arm(merged: int) -> ScoredSet:
        """系统自动合并前 merged 对；对错以金标为准，预测不自报任何关系。"""
        ids = pair_ids[: merged * 2]
        preds = [
            MergePrediction(
                pair_left_sample_id=ids[2 * i],
                pair_right_sample_id=ids[2 * i + 1],
                system_auto_merged=True,
                human_relation=None,
            )
            for i in range(merged)
        ]
        return evaluate_merge(
            preds,
            set(heldout),
            dataset_id=GATE_DATASET_ID,
            split_fingerprint=split.fingerprint,
            gold_pairs=pair_gold,
        )

    return records + pair_gold, split, heldout, arm


def _gate(
    records,
    split,
    heldout,
    baseline_set: ScoredSet,
    enhanced_set: ScoredSet,
    *,
    dataset_id: str = GATE_DATASET_ID,
    pre_declared: list[str] | None = None,
) -> GateInput:
    spec = build_experiment_spec(
        dataset_id=dataset_id,
        eligible_sample_ids=heldout,
        metric_names=["merge_precision", "error_merge_rate"],
        pre_declared_improvements=pre_declared if pre_declared is not None else ["merge_precision"],
        split_fingerprint=split.fingerprint,
    )
    return GateInput(
        spec=spec,
        baseline=baseline_set,
        enhanced=enhanced_set,
        annotation_records=records,
        heldout_manifest=split,
    )


class TestGate:
    def test_pass_requires_all_conditions(self):
        records, split, heldout, arm = _gate_env()
        gate = _gate(records, split, heldout, arm(19), arm(18))
        result = evaluate_gate(gate)
        assert result["decision"] == "PASS", result["reasons"]
        assert result["reasons"] == []
        assert result["thresholds"]["merge_precision_min"] == MERGE_PRECISION_MIN
        assert result["thresholds"]["error_merge_rate_max"] == ERROR_MERGE_RATE_MAX
        assert result["thresholds"]["human_total_min"] == 200
        assert result["thresholds"]["heldout_human_min"] == HELDOUT_HUMAN_MIN
        assert result["heldout"]["human_total"] == 220  # 200 抽取金标 + 20 语义对金标，仍在 200-300 口径内
        assert result["heldout"]["heldout_eligible_size"] == len(heldout)
        assert result["gold"]["merge_pair_gold_records"] == 20
        assert result["gold"]["merge_gold_bound"] is True

    def test_synthetic_records_never_pass_even_with_perfect_numbers(self):
        records, split, heldout, arm = _gate_env(200, synthetic_labels=True)
        gate = _gate(records, split, heldout, _scored(20, 20), _scored(20, 20))
        result = evaluate_gate(gate)
        assert result["decision"] == "FAIL"
        assert any("人工金标" in reason for reason in result["reasons"])

    def test_insufficient_human_total_fails(self):
        records, split, heldout, arm = _gate_env(50)
        gate = _gate(records, split, heldout, arm(9), arm(8))
        result = evaluate_gate(gate)
        assert result["decision"] == "FAIL"
        assert any("200" in reason for reason in result["reasons"])

    def test_missing_artifacts_are_not_evaluated_not_fabricated_pass(self):
        records, split, heldout, arm = _gate_env()
        spec = build_experiment_spec(
            dataset_id="d-missing",
            eligible_sample_ids=heldout,
            metric_names=["merge_precision"],
            pre_declared_improvements=["merge_precision"],
            split_fingerprint=split.fingerprint,
        )
        gate = GateInput(
            spec=spec,
            baseline=arm(19),
            enhanced=arm(18),
            annotation_records=[],
            heldout_manifest=None,
        )
        result = evaluate_gate(gate)
        assert result["decision"] == "NOT_EVALUATED"
        assert any("标注记录" in reason for reason in result["reasons"])
        assert any("manifest" in reason for reason in result["reasons"])

    def test_zero_denominator_with_valid_artifacts_is_not_evaluated(self):
        records, split, heldout, arm = _gate_env()
        ids = heldout[:40]
        empty_preds = [
            MergePrediction(
                pair_left_sample_id=ids[2 * i],
                pair_right_sample_id=ids[2 * i + 1],
                system_auto_merged=False,
                human_relation=None,
            )
            for i in range(20)
        ]
        scored = evaluate_merge(
            empty_preds,
            set(heldout),
            dataset_id=GATE_DATASET_ID,
            split_fingerprint=split.fingerprint,
        )
        gate = _gate(records, split, heldout, scored, scored)
        result = evaluate_gate(gate)
        assert result["decision"] == "NOT_EVALUATED"
        assert any("unknown" in reason for reason in result["reasons"])

    def test_wrong_arm_scored_on_other_set_fails(self):
        records, split, heldout, arm = _gate_env()
        foreign_arm = evaluate_merge(
            [_merge("h0", "h1", auto=True, human="same_need", evidence=[])],
            {f"h{i}" for i in range(500, 599)},
            split_fingerprint=split.fingerprint,
        )
        gate = _gate(records, split, heldout, arm(19), foreign_arm)
        result = evaluate_gate(gate)
        assert result["decision"] == "FAIL"
        assert any("臂打分集指纹" in reason for reason in result["reasons"])

    def test_no_improvement_cannot_bless_gate(self):
        records, split, heldout, arm = _gate_env()
        gate = _gate(records, split, heldout, arm(18), arm(18))
        result = evaluate_gate(gate)
        assert result["decision"] == "FAIL"
        assert any("预声明指标" in reason for reason in result["reasons"])

    def test_error_rate_worsening_fails_even_if_other_conditions_pass(self):
        records, split, heldout, arm = _gate_env()
        # baseline 只合并金标一致的前 18 对（error 0）；enhanced 多合并两对
        # 金标为 different_need 的对：error 2/20=0.10 > 阈值（越低越好）。
        gate2 = _gate(records, split, heldout, arm(18), arm(20))
        result2 = evaluate_gate(gate2)
        assert result2["decision"] == "FAIL"
        assert any("error_merge_rate" in reason for reason in result2["reasons"])

    def test_posthoc_free_form_comparison_dict_rejected(self):
        records, split, heldout, arm = _gate_env()
        gate = _gate(records, split, heldout, arm(19), arm(18))
        payload = gate.model_dump()
        payload["comparison"] = {"comparable": True, "improvements_met": ["merge_precision"]}
        payload["heldout_provenance_source"] = "human"
        payload["heldout_sample_count"] = 250
        with pytest.raises(ValidationError):
            GateInput.model_validate(payload)
        with pytest.raises(ValidationError):
            GateInput(spec=gate.spec, baseline=gate.baseline, enhanced=gate.enhanced,  # type: ignore[call-arg]
                      comparison={"comparable": True})

    def test_spec_mismatch_for_actual_heldout_fails(self):
        records, split, heldout, arm = _gate_env()
        gate = _gate(records, split, heldout, arm(19), arm(18))
        bad_spec = build_experiment_spec(
            dataset_id="other-dataset",
            eligible_sample_ids=heldout,
            metric_names=["merge_precision", "error_merge_rate"],
            pre_declared_improvements=["merge_precision"],
            split_fingerprint=split.fingerprint,
        )
        result = evaluate_gate(
            GateInput(
                spec=bad_spec,
                baseline=gate.baseline,
                enhanced=gate.enhanced,
                annotation_records=records,
                heldout_manifest=split,
            )
        )
        assert result["decision"] == "FAIL"
        assert any("Spec" in reason for reason in result["reasons"])

    def test_gate_does_not_double_require_200_heldout(self):
        # 计划口径：总量 200-300，按组切分；heldout 只需实验下限而非再 200。
        records, split, heldout, arm = _gate_env()
        assert len(heldout) < 200
        assert len(heldout) >= HELDOUT_HUMAN_MIN
        gate = _gate(records, split, heldout, arm(19), arm(18))
        assert evaluate_gate(gate)["decision"] == "PASS"


class TestSkeleton:
    def test_skeleton_has_no_flattering_numbers(self):
        skeleton = metrics_skeleton()
        assert skeleton["status"] == "NOT_EVALUATED"
        blob = json.dumps(skeleton)
        assert "0.9" not in blob.replace('"merge_precision_min"', "")
        for section in ("merge", "extraction"):
            for metric in skeleton[section].values():
                assert metric["value"] is None
                assert metric["denominator"] == 0
        assert skeleton["human_dataset"]["status"] == "NOT_MET"
        assert skeleton["human_dataset"]["provided"] is False

    def test_skeleton_write_roundtrip(self, tmp_path):
        path = write_metrics_skeleton(tmp_path / "evaluation" / "metrics.json")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == metrics_skeleton()
        assert EvaluationReport().status == "NOT_EVALUATED"

    def test_shipped_metrics_json_is_unevaluated_skeleton(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        shipped = json.loads((repo_root / "evaluation" / "metrics.json").read_text(encoding="utf-8"))
        assert shipped == metrics_skeleton()
        templates = repo_root / "evaluation" / "templates"
        assert (templates / "annotation_extraction_template.jsonl").read_text(encoding="utf-8") == ""
        assert (templates / "annotation_pair_template.jsonl").read_text(encoding="utf-8") == ""
        assert (templates / "ANNOTATION_INSTRUCTIONS.md").stat().st_size > 0
