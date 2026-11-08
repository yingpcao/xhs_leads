"""P2-A-GOLD-MANIFEST-r3 门禁完整性测试。

本文件内所有记录均为模拟人工契约取值的合成 UNIT fixture（dataset_id 明确
标注 int-probe-unit），不代表、也不生成任何真实收集的人工标注数据集；
真实 200-300 条人工标注仍为 NOT_MET/待定实验策略。
"""

from __future__ import annotations

import pytest

from xhs_discovery.evaluation.annotations import (
    ANNOTATION_SCHEMA_VERSION,
    EXTRACTION_LABEL_FIELDS,
    AnnotationProvenance,
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
    human_dataset_status,
)
from xhs_discovery.evaluation.metrics import (
    GateInput,
    MergePrediction,
    build_experiment_spec,
    evaluate_gate,
    evaluate_merge,
)
from xhs_discovery.evaluation.splits import SplitItem, group_split
from xhs_discovery.models.common import sha256_text

INT_DATASET_ID = "p2-gate-int-probe-unit"


def _provenance(source: str = "human") -> AnnotationProvenance:
    return AnnotationProvenance(
        source=source,  # type: ignore[arg-type]
        dataset_id=INT_DATASET_ID,
        producer_id="synthetic:int-unit" if source != "human" else "annotator-int-unit",
        produced_at=None,
        synthetic=source != "human",
    )


def _extraction(
    i: int, prov: AnnotationProvenance, *, known: bool = True, family: str | None = None
) -> ExtractionAnnotationRecord:
    text = f"int-{i}"
    label = {name: None for name in EXTRACTION_LABEL_FIELDS}
    if known:
        label["explicit_problem"] = i % 2 == 0
        label["region"] = True
    return ExtractionAnnotationRecord(
        schema_version=ANNOTATION_SCHEMA_VERSION,
        sample_id=f"h{i}",
        source_id=f"hsrc{i}",
        family_id=family or f"hfam{i}",
        content_sha256=sha256_text(text),
        text=text,
        provenance=prov,
        reviewer_state="reviewed",
        reviewer_id="int-reviewer",
        label=label,
    )


def _pair(
    left: ExtractionAnnotationRecord,
    right: ExtractionAnnotationRecord,
    relation: str,
    prov: AnnotationProvenance,
) -> PairAnnotationRecord:
    return PairAnnotationRecord(
        schema_version=ANNOTATION_SCHEMA_VERSION,
        sample_id=left.sample_id,
        right_sample_id=right.sample_id,
        source_id=left.source_id,
        family_id=left.family_id,
        content_sha256=left.content_sha256,
        text=left.text,
        provenance=prov,
        reviewer_state="reviewed",
        reviewer_id="int-reviewer",
        label=None,
        pair_relation=relation,  # type: ignore[arg-type]
    )


def _env(n: int = 200, *, source: str = "human", known_labels: bool = True):
    """真实形状的合法环境：显式标签抽取金标 + 显式语义对金标 + 一致切分。"""
    prov = _provenance(source)
    records = [_extraction(i, prov, known=known_labels) for i in range(n)]
    split = group_split(
        [SplitItem(sample_id=r.sample_id, source_id=r.source_id, family_id=r.family_id) for r in records]
    )
    by_id = {r.sample_id: r for r in records}
    heldout = sorted(by_id.keys() & set(split.heldout_sample_ids))
    n_pairs = min(20, len(heldout) // 2)
    same_until = max(n_pairs - 2, 0)
    pair_ids = heldout[: n_pairs * 2]
    pairs = [
        _pair(by_id[pair_ids[2 * i]], by_id[pair_ids[2 * i + 1]],
              "same_need" if i < same_until else "different_need", prov)
        for i in range(n_pairs)
    ]

    def arm(merged: int, *, bind: bool = True):
        preds = [
            MergePrediction(
                pair_left_sample_id=pair_ids[2 * i],
                pair_right_sample_id=pair_ids[2 * i + 1],
                system_auto_merged=True,
                human_relation=None,
            )
            for i in range(merged)
        ]
        if not bind:
            # 模拟旧口径：预测自报 human_relation，无金标绑定（不得祝福门禁）。
            preds = [
                MergePrediction(
                    pair_left_sample_id=p.pair_left_sample_id,
                    pair_right_sample_id=p.pair_right_sample_id,
                    system_auto_merged=True,
                    human_relation="same_need" if i < same_until else "different_need",
                )
                for i, p in enumerate(preds)
            ]
        return evaluate_merge(
            preds,
            set(heldout),
            dataset_id=INT_DATASET_ID,
            split_fingerprint=split.fingerprint,
            gold_pairs=pairs if bind else None,
        )

    def gate(records_in, manifest, baseline, enhanced, eligible) -> GateInput:
        spec = build_experiment_spec(
            dataset_id=INT_DATASET_ID,
            eligible_sample_ids=eligible,
            metric_names=["merge_precision", "error_merge_rate"],
            pre_declared_improvements=["merge_precision"],
            split_fingerprint=manifest.fingerprint,
        )
        return GateInput(
            spec=spec,
            baseline=baseline,
            enhanced=enhanced,
            annotation_records=records_in,
            heldout_manifest=manifest,
        )

    return {
        "records": records,
        "pairs": pairs,
        "split": split,
        "heldout": heldout,
        "same_until": same_until,
        "prov": prov,
        "arm": arm,
        "gate": gate,
    }


class TestGoldEligibility:
    def test_all_null_reviewed_extraction_records_are_not_gold(self):
        env = _env(known_labels=False)
        assert all(not r.is_human_gold() for r in env["records"])
        status = human_dataset_status(list(env["records"]))
        assert status.status == "NOT_MET"
        assert status.human_reviewed_count == 0

    def test_all_null_reviewed_gate_fails_even_with_binding_arms(self):
        env = _env(known_labels=False)
        result = evaluate_gate(
            env["gate"](env["records"] + env["pairs"], env["split"], env["arm"](19), env["arm"](18), env["heldout"])
        )
        assert result["decision"] == "FAIL"
        assert any("人工金标" in reason or "200" in reason for reason in result["reasons"])

    def test_partial_labels_are_gold_only_on_known_fields(self):
        record = _extraction(0, _provenance(), known=True)
        assert record.has_any_known_label() and record.is_human_gold()
        unknown = _extraction(1, _provenance(), known=False)
        assert not unknown.has_any_known_label() and not unknown.is_human_gold()


class TestMergeGoldBlessing:
    def test_extraction_gold_alone_cannot_bless_merge(self):
        env = _env()
        # 臂未绑定金标：像旧口径那样只靠预测自报 human_relation。
        result = evaluate_gate(
            env["gate"](env["records"], env["split"], env["arm"](19, bind=False), env["arm"](18, bind=False), env["heldout"])
        )
        assert result["decision"] == "FAIL"
        assert any("PairAnnotationRecord" in reason for reason in result["reasons"])
        assert any("自报" in reason for reason in result["reasons"])

    def test_self_reported_relation_conflicting_with_gold_is_excluded(self):
        env = _env()
        gold = env["pairs"][0]
        assert gold.pair_relation == "same_need"
        wrong_pred = MergePrediction(
            pair_left_sample_id=gold.sample_id,
            pair_right_sample_id=gold.right_sample_id,
            system_auto_merged=True,
            human_relation="different_need",
        )
        scored = evaluate_merge(
            [wrong_pred],
            {gold.sample_id, gold.right_sample_id},
            gold_pairs=[gold],
        )
        assert scored.counts["merge_precision"].denominator == 0
        assert any("不符" in e["reason"] for e in scored.excluded)

    def test_gold_relation_decides_not_prediction(self):
        env = _env()
        gold = env["pairs"][0]  # same_need
        pred = MergePrediction(
            pair_left_sample_id=gold.sample_id,
            pair_right_sample_id=gold.right_sample_id,
            system_auto_merged=True,
            human_relation=None,
        )
        scored = evaluate_merge([pred], {gold.sample_id, gold.right_sample_id}, gold_pairs=[gold])
        assert scored.counts["merge_precision"].numerator == 1
        assert scored.gold_fingerprint is not None

    def test_conflicting_duplicate_pair_gold_excluded_entirely(self):
        env = _env()
        gold = env["pairs"][0]
        twin = gold.model_copy(update={"pair_relation": "different_need"})
        scored = evaluate_merge(
            [
                MergePrediction(
                    pair_left_sample_id=gold.sample_id,
                    pair_right_sample_id=gold.right_sample_id,
                    system_auto_merged=True,
                    human_relation=None,
                )
            ],
            {gold.sample_id, gold.right_sample_id},
            gold_pairs=[gold, twin],
        )
        assert scored.counts["merge_precision"].denominator == 0
        assert any("冲突" in e["reason"] for e in scored.excluded)

    def test_missing_endpoint_metadata_refuses_evaluation(self):
        env = _env()
        ghost_pair = _pair(env["records"][0], env["records"][1], "same_need", env["prov"]).model_copy(
            update={"right_sample_id": "ghostx"}
        )
        result = evaluate_gate(
            env["gate"](
                env["records"] + env["pairs"] + [ghost_pair],
                env["split"],
                env["arm"](19),
                env["arm"](18),
                env["heldout"],
            )
        )
        assert result["decision"] == "NOT_EVALUATED"
        assert any("端点缺少显式样本元数据" in reason for reason in result["reasons"])
        assert any("ghostx" in reason for reason in result["reasons"])


class TestManifestIntegrity:
    def test_shared_family_across_partitions_rejected(self):
        env = _env()
        dev_ids = env["split"].dev_sample_ids
        held_ids = env["split"].heldout_sample_ids
        mutated = {dev_ids[0], dev_ids[1], held_ids[50], held_ids[51]}
        records = [
            r.model_copy(update={"family_id": "sharedfam"}) if r.sample_id in mutated else r
            for r in env["records"]
        ]
        result = evaluate_gate(
            env["gate"](records + env["pairs"], env["split"], env["arm"](19), env["arm"](18), env["heldout"])
        )
        assert result["decision"] == "FAIL"
        assert any("整体复核" in reason for reason in result["reasons"])

    def test_tampered_split_fingerprint_rejected(self):
        env = _env()
        tampered = env["split"].model_copy(update={"fingerprint": "0" * 64})
        result = evaluate_gate(
            env["gate"](env["records"] + env["pairs"], tampered, env["arm"](19), env["arm"](18), env["heldout"])
        )
        assert result["decision"] == "FAIL"
        assert any("整体复核" in reason or "指纹" in reason for reason in result["reasons"])

    def test_stale_pair_gold_after_scoring_cannot_keep_old_pass(self):
        env = _env()
        baseline, enhanced = env["arm"](19), env["arm"](18)
        flipped = [
            p.model_copy(update={"pair_relation": "different_need" if p.pair_relation == "same_need" else "same_need"})
            for p in env["pairs"]
        ]
        result = evaluate_gate(
            env["gate"](env["records"] + flipped, env["split"], baseline, enhanced, env["heldout"])
        )
        assert result["decision"] == "FAIL"
        assert any("金标绑定指纹" in reason for reason in result["reasons"])

    def test_stale_content_hash_after_scoring_cannot_keep_old_pass(self):
        env = _env()
        baseline, enhanced = env["arm"](19), env["arm"](18)
        tampered_text = "tampered-after-scoring"
        tampered_pairs = [
            p.model_copy(update={"content_sha256": sha256_text(tampered_text), "text": tampered_text})
            if idx == 0
            else p
            for idx, p in enumerate(env["pairs"])
        ]
        result = evaluate_gate(
            env["gate"](env["records"] + tampered_pairs, env["split"], baseline, enhanced, env["heldout"])
        )
        assert result["decision"] == "FAIL"
        assert any("金标绑定指纹" in reason for reason in result["reasons"])

    def test_duplicate_annotation_id_with_conflicting_labels_fails(self):
        env = _env()
        dup = env["records"][0].model_copy(
            update={"label": {**{name: None for name in EXTRACTION_LABEL_FIELDS}, "region": False}}
        )
        result = evaluate_gate(
            env["gate"](
                env["records"] + [dup] + env["pairs"], env["split"], env["arm"](19), env["arm"](18), env["heldout"]
            )
        )
        assert result["decision"] == "FAIL"
        assert any("冲突的抽取标注" in reason for reason in result["reasons"])


class TestGatePositiveAndSynthetic:
    def test_valid_labeled_gold_with_validated_split_passes(self):
        env = _env()
        result = evaluate_gate(
            env["gate"](env["records"] + env["pairs"], env["split"], env["arm"](19), env["arm"](18), env["heldout"])
        )
        assert result["decision"] == "PASS", result["reasons"]
        assert result["reasons"] == []
        assert result["gold"]["merge_gold_bound"] is True

    def test_synthetic_provenance_gold_still_never_passes(self):
        env = _env(source="synthetic_fixture")
        result = evaluate_gate(
            env["gate"](env["records"] + env["pairs"], env["split"], env["arm"](19), env["arm"](18), env["heldout"])
        )
        assert result["decision"] != "PASS"
        assert any("人工金标" in reason for reason in result["reasons"])

    def test_arm_bound_to_different_gold_set_fails(self):
        env = _env()
        baseline = env["arm"](19)
        other_gold = env["pairs"][:-1] + [
            env["pairs"][-1].model_copy(update={"reviewer_id": "someone-else"})
        ]
        preds = [
            MergePrediction(
                pair_left_sample_id=env["heldout"][2 * i],
                pair_right_sample_id=env["heldout"][2 * i + 1],
                system_auto_merged=True,
                human_relation=None,
            )
            for i in range(18)
        ]
        from xhs_discovery.evaluation.metrics import evaluate_merge as em

        enhanced = em(
            preds,
            set(env["heldout"]),
            dataset_id=INT_DATASET_ID,
            split_fingerprint=env["split"].fingerprint,
            gold_pairs=other_gold,
        )
        result = evaluate_gate(env["gate"](env["records"] + env["pairs"], env["split"], baseline, enhanced, env["heldout"]))
        assert result["decision"] == "FAIL"
        assert any("金标绑定指纹" in reason for reason in result["reasons"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
