"""标注契约测试：合成来源永远不能满足人工门槛；未知/未标注合法。"""

from __future__ import annotations

import pytest

from xhs_discovery.evaluation.annotations import (
    ANNOTATION_SCHEMA_VERSION,
    EXTRACTION_LABEL_FIELDS,
    AnnotationProvenance,
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
    empty_extraction_records,
    empty_pair_records,
    human_dataset_status,
    load_annotation_records,
    synthetic_test_fixtures,
)
from xhs_discovery.models.common import sha256_text


def _human_provenance() -> AnnotationProvenance:
    return AnnotationProvenance(
        source="human",
        dataset_id="p2-human-unit",
        producer_id="annotator-unit-test",
        produced_at=None,
        synthetic=False,
    )


def _human_records(n: int, *, all_unknown_labels: bool = False) -> list[ExtractionAnnotationRecord]:
    """单元测试内模拟人工契约取值的合成 UNIT 记录，不是真实收集的人工标注。

    all_unknown_labels=True 生成在途标注（reviewed 但全 unknown）：合法但不是金标。
    """
    records = []
    for i in range(n):
        text = f"unit-test-human-{i}"
        if all_unknown_labels:
            label = {name: None for name in EXTRACTION_LABEL_FIELDS}
        else:
            label = {
                name: None for name in EXTRACTION_LABEL_FIELDS
            } | {
                "explicit_problem": i % 2 == 0,
                "region": True,
            }
        records.append(
            ExtractionAnnotationRecord(
                schema_version=ANNOTATION_SCHEMA_VERSION,
                sample_id=f"h{i}",
                source_id=f"hsrc{i}",
                family_id=f"hfam{i}",
                content_sha256=sha256_text(text),
                text=text,
                provenance=_human_provenance(),
                reviewer_state="reviewed",
                reviewer_id="reviewer-unit",
                label=label,
            )
        )
    return records


class TestProvenance:
    def test_synthetic_must_be_flagged(self):
        with pytest.raises(ValueError, match="synthetic"):
            AnnotationProvenance(
                source="synthetic_fixture", dataset_id="d", producer_id="p", synthetic=False
            )

    def test_model_producer_cannot_claim_human(self):
        with pytest.raises(ValueError, match="不能声明"):
            AnnotationProvenance(
                source="human", dataset_id="d", producer_id="model:qwen", synthetic=False
            )

    def test_human_cannot_be_marked_synthetic(self):
        with pytest.raises(ValueError):
            AnnotationProvenance(
                source="human", dataset_id="d", producer_id="a", synthetic=True
            )


class TestHumanDatasetGate:
    def test_synthetic_fixtures_never_count_as_human_gold(self):
        extraction, pairs = synthetic_test_fixtures()
        assert all(not r.is_human_gold() for r in extraction)
        assert all(not r.is_human_gold() for r in pairs)
        status = human_dataset_status(list(extraction) + list(pairs))
        assert status.status == "NOT_MET"
        assert status.human_reviewed_count == 0
        assert any("合成" in reason for reason in status.reasons)

    def test_empty_dataset_is_not_met_not_auto_created(self):
        status = human_dataset_status([])
        assert status.status == "NOT_MET"
        assert status.human_reviewed_count == 0
        assert any("不允许自动生成" in reason for reason in status.reasons)

    def test_met_only_with_200_reviewed_human_records(self):
        # 单元测试内构造 200 条带明确标签的模拟人工记录仅验证计数契约，不代表真实数据集存在。
        status = human_dataset_status(_human_records(200))
        assert status.status == "MET"
        assert status.human_reviewed_count == 200
        status_below = human_dataset_status(_human_records(199))
        assert status_below.status == "NOT_MET"

    def test_reviewed_with_all_unknown_labels_is_not_gold(self):
        records = _human_records(200, all_unknown_labels=True)
        assert all(not r.is_human_gold() for r in records)
        status = human_dataset_status(records)
        assert status.status == "NOT_MET"
        assert status.human_reviewed_count == 0
        assert any("unknown" in reason for reason in status.reasons)


class TestRecordContracts:
    def test_unlabeled_must_not_carry_label(self):
        with pytest.raises(ValueError, match="unlabeled"):
            ExtractionAnnotationRecord(
                schema_version=ANNOTATION_SCHEMA_VERSION,
                sample_id="s",
                source_id="src",
                family_id="fam",
                content_sha256=sha256_text("t"),
                text="t",
                provenance=_human_provenance(),
                reviewer_state="unlabeled",
                label={"explicit_problem": True},
            )

    def test_reviewed_requires_reviewer_id(self):
        with pytest.raises(ValueError, match="reviewer_id"):
            ExtractionAnnotationRecord(
                schema_version=ANNOTATION_SCHEMA_VERSION,
                sample_id="s",
                source_id="src",
                family_id="fam",
                content_sha256=sha256_text("t"),
                text="t",
                provenance=_human_provenance(),
                reviewer_state="reviewed",
                label={name: None for name in EXTRACTION_LABEL_FIELDS},
            )

    def test_content_hash_must_match_text(self):
        with pytest.raises(ValueError, match="content_sha256"):
            ExtractionAnnotationRecord(
                schema_version=ANNOTATION_SCHEMA_VERSION,
                sample_id="s",
                source_id="src",
                family_id="fam",
                content_sha256="0" * 64,
                text="t",
                provenance=_human_provenance(),
                reviewer_state="unlabeled",
            )

    def test_extraction_label_fields_exact(self):
        base = dict(
            schema_version=ANNOTATION_SCHEMA_VERSION,
            sample_id="s",
            source_id="src",
            family_id="fam",
            content_sha256=sha256_text("t"),
            text="t",
            provenance=_human_provenance(),
            reviewer_state="reviewed",
            reviewer_id="r",
        )
        with pytest.raises(ValueError, match="字段集"):
            ExtractionAnnotationRecord.model_validate({**base, "label": {"explicit_problem": True}})
        record = ExtractionAnnotationRecord.model_validate(
            {**base, "label": {name: None for name in EXTRACTION_LABEL_FIELDS}}
        )
        assert record.label is not None and all(v is None for v in record.label.values())

    def test_pair_relation_rules(self):
        base = dict(
            schema_version=ANNOTATION_SCHEMA_VERSION,
            sample_id="a",
            right_sample_id="b",
            source_id="src",
            family_id="fam",
            content_sha256=sha256_text("t"),
            text="t",
            provenance=_human_provenance(),
            reviewer_state="unlabeled",
        )
        with pytest.raises(ValueError, match="不能相同"):
            PairAnnotationRecord.model_validate({**base, "right_sample_id": "a", "pair_relation": "unknown"})
        reviewed = {**base, "reviewer_state": "reviewed", "reviewer_id": "r", "label": None}
        with pytest.raises(ValueError, match="unknown"):
            PairAnnotationRecord.model_validate({**reviewed, "pair_relation": "unknown"})
        ok = PairAnnotationRecord.model_validate({**reviewed, "pair_relation": "same_need"})
        assert ok.pair_relation == "same_need"


class TestTemplates:
    def test_templates_are_empty_and_not_valid_records(self):
        rows = empty_extraction_records(3) + empty_pair_records(2)
        assert len(rows) == 5
        for row in rows:
            model = PairAnnotationRecord if "pair_relation" in row else ExtractionAnnotationRecord
            with pytest.raises(Exception):
                model.model_validate(row)
        assert rows[0]["reviewer_state"] == "unlabeled"
        assert rows[0]["label"] is None

    def test_template_does_not_bulk_generate_200(self):
        assert len(empty_extraction_records()) == 0


class TestJsonlLoader:
    def test_synthetic_fixture_roundtrip_stays_not_human(self, tmp_path):
        import json

        extraction, pairs = synthetic_test_fixtures()
        fixture = tmp_path / "synthetic_annotations.jsonl"
        lines = [
            json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
            for r in list(extraction) + list(pairs)
        ]
        fixture.write_text("\n".join(lines) + "\n", encoding="utf-8")
        loaded = load_annotation_records(fixture)
        assert len(loaded) == 7
        status = human_dataset_status(loaded)
        assert status.status == "NOT_MET"
        assert status.human_reviewed_count == 0

    def test_unknown_schema_version_rejected(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"schema_version": "other-v9"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            load_annotation_records(bad)

