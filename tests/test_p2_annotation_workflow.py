"""P2-B 标注工作流测试：packet 生成/往返导入/安全边界。

本文件内所有"人工/复核"记录都是模拟既有契约取值的合成 UNIT fixture
（dataset_id/producer 均带 unit 字样），不代表也不构成任何真实收集的人工标注；
真实 200–300 条人工标注仍由用户提供，human_dataset_status 维持 NOT_MET。
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from xhs_discovery.evaluation import annotation_workflow as wf
from xhs_discovery.evaluation.annotations import (
    EXTRACTION_LABEL_FIELDS,
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
    load_annotation_records,
)
from xhs_discovery.models.common import sha256_text

FIXTURES = Path(__file__).parent / "fixtures" / "p2" / "workflow_demo"
LABEL_KEYS = list(EXTRACTION_LABEL_FIELDS)


def _samples() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "samples_manifest.jsonl").read_text("utf-8").splitlines() if line.strip()]


def _pairs() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "candidate_pairs.jsonl").read_text("utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def sample_manifest(tmp_path: Path) -> Path:
    return _write_jsonl(tmp_path / "samples.jsonl", _samples())


@pytest.fixture
def pairs_manifest(tmp_path: Path) -> Path:
    return _write_jsonl(tmp_path / "pairs.jsonl", _pairs())


@pytest.fixture
def packet(tmp_path: Path, sample_manifest: Path, pairs_manifest: Path) -> Path:
    out = tmp_path / "packet"
    wf.prepare_packet(sample_manifest, out, pairs_manifest)
    return out


def _worksheet_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _set_row_value(packet_dir: Path, name: str, mutate) -> None:
    rows = _worksheet_rows(packet_dir / name)
    mutate(rows)
    _write_jsonl(packet_dir / name, rows)


def _quarantine_rows(import_dir: Path) -> list[dict]:
    """读取隔离 sidecar：每条是原始记录 + `_quarantine` 原因（不丢弃、不重新标记）。"""
    path = Path(import_dir) / wf.QUARANTINED_ANNOTATIONS_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


class TestPrepare:
    def test_packet_layout_and_fingerprint(self, tmp_path: Path, sample_manifest: Path, pairs_manifest: Path):
        out = tmp_path / "pkt"
        manifest = wf.prepare_packet(sample_manifest, out, pairs_manifest)
        assert manifest.version == wf.WORKFLOW_VERSION
        assert manifest.n_samples == 5 and manifest.n_pairs == 2
        assert manifest.origin_counts == {"actual_source": 3, "model_generated": 1, "synthetic_fixture": 1}
        for name in (
            wf.PACKET_MANIFEST_NAME, wf.SAMPLES_NAME, wf.EXTRACTION_WORKSHEET_NAME,
            wf.EXTRACTION_WORKSHEET_CSV, wf.PAIR_WORKSHEET_NAME, wf.PAIR_WORKSHEET_CSV,
        ):
            assert (out / name).is_file(), name
        for name, digest in manifest.files.items():
            assert sha256_text((out / name).read_text("utf-8")) == digest, name

    def test_fingerprint_stable_across_dirs(self, tmp_path: Path, sample_manifest: Path, pairs_manifest: Path):
        m1 = wf.prepare_packet(sample_manifest, tmp_path / "a", pairs_manifest)
        m2 = wf.prepare_packet(sample_manifest, tmp_path / "b", pairs_manifest)
        assert m1.packet_fingerprint == m2.packet_fingerprint
        text1 = (tmp_path / "a" / wf.EXTRACTION_WORKSHEET_NAME).read_text("utf-8")
        text2 = (tmp_path / "b" / wf.EXTRACTION_WORKSHEET_NAME).read_text("utf-8")
        assert text1 == text2
        m3 = wf.prepare_packet(sample_manifest, tmp_path / "c")
        assert m3.pairs_fingerprint != m1.pairs_fingerprint
        assert m3.packet_fingerprint != m1.packet_fingerprint

    def test_worksheet_rows_start_unlabeled_with_placeholders(self, packet: Path):
        rows = _worksheet_rows(packet / wf.EXTRACTION_WORKSHEET_NAME)
        assert len(rows) == 5
        for row in rows:
            assert row["reviewer_state"] == "unlabeled"
            assert row["label"] is None
            assert row["reviewer_id"] is None
            assert row["provenance"]["producer_id"] == wf.PLACEHOLDER_PRODUCER
            assert row["provenance"]["dataset_id"] == wf.PLACEHOLDER_DATASET_EXTRACTION
            record = ExtractionAnnotationRecord.model_validate(row)
            assert not record.is_human_gold()
        pair_rows = _worksheet_rows(packet / wf.PAIR_WORKSHEET_NAME)
        assert len(pair_rows) == 2
        for row in pair_rows:
            assert row["reviewer_state"] == "unlabeled"
            assert row["pair_relation"] == "unknown"
            assert PairAnnotationRecord.model_validate(row).pair_relation == "unknown"

    def test_text_hash_source_roundtrip(self, packet: Path, tmp_path: Path):
        original = {row["sample_id"]: row for row in _samples()}
        rows = _worksheet_rows(packet / wf.EXTRACTION_WORKSHEET_NAME)
        for row in rows:
            src = original[row["sample_id"]]
            assert row["text"] == src["text"]
            assert row["content_sha256"] == src["content_sha256"] == sha256_text(src["text"])
            assert row["source_id"] == src["source_id"] and row["family_id"] == src["family_id"]
        report = wf.import_worksheet(packet, tmp_path / "imp")
        loaded = load_annotation_records(tmp_path / "imp" / wf.VALIDATED_ANNOTATIONS_NAME)
        assert {r.sample_id for r in loaded} == set(original)
        for record in loaded:
            if isinstance(record, ExtractionAnnotationRecord):
                assert record.text == original[record.sample_id]["text"]
        assert report["ready"] == "NOT_READY"

    def test_formula_safe_csv_jsonl_exact(self, packet: Path):
        formula_sample = next(s for s in _samples() if s["text"].startswith("="))
        csv_text = (packet / wf.EXTRACTION_WORKSHEET_CSV).read_text("utf-8-sig")
        rows = list(csv.reader(csv_text.splitlines()))
        header = rows[0]
        data = next(r for r in rows[1:] if r[0] == formula_sample["sample_id"])
        guarded = data[header.index("text")]
        assert guarded.startswith("'") and guarded[1:] == formula_sample["text"]
        jsonl_texts = {r["sample_id"]: r["text"] for r in _worksheet_rows(packet / wf.EXTRACTION_WORKSHEET_NAME)}
        assert jsonl_texts[formula_sample["sample_id"]] == formula_sample["text"]
        assert not jsonl_texts[formula_sample["sample_id"]].startswith("'")

    def test_refuses_overwrite_nonempty(self, tmp_path: Path, sample_manifest: Path):
        out = tmp_path / "pkt"
        wf.prepare_packet(sample_manifest, out)
        with pytest.raises(wf.PacketExistsError):
            wf.prepare_packet(sample_manifest, out)
        with pytest.raises(wf.PacketExistsError):
            wf.import_worksheet(out, out)

    def test_refuses_empty_manifest(self, tmp_path: Path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(wf.WorkflowError, match="为空"):
            wf.prepare_packet(empty, tmp_path / "pkt")


class TestManifestValidation:
    def _row(self, **overrides):
        row = _samples()[0]
        row.update(overrides)
        if "text" in overrides and "content_sha256" not in overrides:
            row["content_sha256"] = sha256_text(overrides["text"])
        return row

    def test_rejects_duplicate_and_conflicting_ids(self, tmp_path: Path):
        dup = _write_jsonl(tmp_path / "dup.jsonl", [self._row(), self._row(family_id="fam-other")])
        with pytest.raises(wf.WorkflowError, match="重复 sample_id.*元数据冲突"):
            wf.load_sample_manifest(dup)
        same = _write_jsonl(tmp_path / "same.jsonl", [self._row(), self._row()])
        with pytest.raises(wf.WorkflowError, match="重复 sample_id"):
            wf.load_sample_manifest(same)

    def test_rejects_bad_hash_missing_meta_unknown_field(self, tmp_path: Path):
        bad_hash = _write_jsonl(tmp_path / "h.jsonl", [self._row(content_sha256="0" * 64)])
        with pytest.raises(wf.WorkflowError, match="content_sha256"):
            wf.load_sample_manifest(bad_hash)
        missing = self._row()
        missing.pop("family_id")
        with pytest.raises(wf.WorkflowError, match="被拒绝"):
            wf.load_sample_manifest(_write_jsonl(tmp_path / "m.jsonl", [missing]))
        unknown = self._row()
        unknown["surprise"] = 1
        with pytest.raises(wf.WorkflowError, match="被拒绝"):
            wf.load_sample_manifest(_write_jsonl(tmp_path / "u.jsonl", [unknown]))

    def test_origin_classification_is_explicit_not_guessed(self, tmp_path: Path):
        lying = self._row(origin="actual_source", origin_producer_id="model:generator")
        with pytest.raises(wf.WorkflowError, match="不能声明 actual_source"):
            wf.load_sample_manifest(_write_jsonl(tmp_path / "l.jsonl", [lying]))
        lazy = self._row(origin="model_generated", origin_producer_id="collector:x")
        with pytest.raises(wf.WorkflowError, match="model: 前缀"):
            wf.load_sample_manifest(_write_jsonl(tmp_path / "g.jsonl", [lazy]))

    def test_pairs_reference_unknown_or_duplicate_rejected(self, tmp_path: Path):
        samples = _write_jsonl(tmp_path / "s.jsonl", _samples())
        ids = {s["sample_id"] for s in _samples()}
        ghost = _write_jsonl(tmp_path / "p1.jsonl", [dict(left_sample_id="demo-1", right_sample_id="nope")])
        with pytest.raises(wf.WorkflowError, match="样本清单之外"):
            wf.load_candidate_pairs(ghost, ids)
        dup = _write_jsonl(tmp_path / "p2.jsonl", _pairs() + [dict(left_sample_id="demo-2", right_sample_id="demo-1")])
        with pytest.raises(wf.WorkflowError, match="重复无序对"):
            wf.load_candidate_pairs(dup, ids)
        self_pair = _write_jsonl(tmp_path / "p3.jsonl", [dict(left_sample_id="demo-1", right_sample_id="demo-1")])
        with pytest.raises(wf.WorkflowError, match="两端不能相同"):
            wf.load_candidate_pairs(self_pair, ids)


class TestImportGuards:
    def test_unlabeled_rows_never_count_as_human(self, packet: Path, tmp_path: Path):
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["ready"] == "NOT_READY"
        assert report["human_dataset_status"]["status"] == "NOT_MET"
        assert report["human_dataset_status"]["human_reviewed_count"] == 0
        assert report["counts"]["rejected_rows"] == 0

    def test_reviewed_with_placeholder_identity_rejected(self, packet: Path, tmp_path: Path):
        def promote(rows):
            rows[0]["reviewer_state"] = "reviewed"
            rows[0]["reviewer_id"] = "someone"
            rows[0]["label"] = {k: False for k in LABEL_KEYS} | {"explicit_problem": True}
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, promote)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["counts"]["rejected_rows"] == 1
        reason = report["rejections"][0]["reason"]
        assert "占位符" in reason and "拒绝静默升格" in reason

    def test_reviewed_requires_meaningful_labels(self, packet: Path, tmp_path: Path):
        def promote_unknown(rows):
            rows[0]["reviewer_state"] = "reviewed"
            rows[0]["reviewer_id"] = "unit-reviewer"
            rows[0]["provenance"]["producer_id"] = "annotator-unit"
            rows[0]["provenance"]["dataset_id"] = "p2-human-unit-workflow"
            rows[0]["label"] = {k: None for k in LABEL_KEYS}
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, promote_unknown)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["counts"]["rejected_rows"] == 1
        assert "全为 unknown" in report["rejections"][0]["reason"]

    def test_draft_with_all_unknown_is_legal_wip_not_gold(self, packet: Path, tmp_path: Path):
        def draft(rows):
            rows[0]["reviewer_state"] = "draft"
            rows[0]["provenance"]["producer_id"] = "annotator-unit"
            rows[0]["provenance"]["dataset_id"] = "p2-human-unit-workflow"
            rows[0]["label"] = {k: None for k in LABEL_KEYS}
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, draft)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["counts"]["rejected_rows"] == 0
        assert report["human_dataset_status"]["human_reviewed_count"] == 0
        assert report["ready"] == "NOT_READY"

    def test_synthetic_or_model_review_never_becomes_human(self, packet: Path, tmp_path: Path):
        def synth(rows):
            for row in rows:
                row["reviewer_state"] = "reviewed"
                row["reviewer_id"] = "synthetic-reviewer"
                row["provenance"] = {
                    "source": "synthetic_fixture", "dataset_id": "p2-synth-unit",
                    "producer_id": "synthetic:unit", "produced_at": None, "synthetic": True,
                }
                row["label"] = {k: False for k in LABEL_KEYS} | {"explicit_problem": True}
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, synth)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        status = report["human_dataset_status"]
        assert status["human_reviewed_count"] == 0
        assert any("合成" in reason for reason in status["reasons"])
        assert report["counts"]["non_human_provenance_records"] == 5
        assert report["synthetic_marked_sample_ids"]

    def test_human_gold_on_model_origin_sample_surfaced(self, packet: Path, tmp_path: Path):
        def annotate_model_sample(rows):
            target = next(r for r in rows if r["sample_id"] == "demo-4")
            target["reviewer_state"] = "reviewed"
            target["reviewer_id"] = "unit-reviewer"
            target["provenance"] = {
                "source": "human", "dataset_id": "p2-human-unit-workflow",
                "producer_id": "annotator-unit", "produced_at": None, "synthetic": False,
            }
            target["label"] = {k: False for k in LABEL_KEYS} | {"region": True}
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, annotate_model_sample)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["human_gold_on_non_actual_samples"]
        assert report["ready"] == "NOT_READY"
        # demo-4 是 model_generated 样本：其上的 human gold 被隔离出产品集合，
        # 产品 human_dataset_status 不再把它计为金标（既有 gold API 规则一字未改，
        # 变的只是这条记录根本不进入产品集合）。
        assert report["human_dataset_status"]["human_reviewed_count"] == 0
        assert report["counts"]["quarantined_records"] == 1
        # 产品规范文件不得再独立重载出这条伪造 gold（canonical reload 绕路已被阻断）。
        kept = load_annotation_records(tmp_path / "imp" / wf.VALIDATED_ANNOTATIONS_NAME)
        assert "demo-4" not in {r.sample_id for r in kept}
        assert sum(1 for r in kept if r.is_human_gold() and r.sample_id == "demo-4") == 0
        # 原始记录连同原因完整保留在隔离 sidecar：真实评审者未被重新标记为 synthetic，也未被丢弃。
        quarantine = _quarantine_rows(tmp_path / "imp")
        assert len(quarantine) == 1
        assert quarantine[0]["sample_id"] == "demo-4"
        assert quarantine[0]["provenance"]["source"] == "human"
        assert quarantine[0]["_quarantine"]["stage"] == "origin"
        assert "demo-4" in quarantine[0]["_quarantine"]["involved_samples"]

    def test_text_drift_rejected(self, packet: Path, tmp_path: Path):
        def drift(rows):
            rows[0]["text"] += "改了原文"
            rows[0]["content_sha256"] = sha256_text(rows[0]["text"])
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, drift)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["counts"]["rejected_rows"] == 1
        assert "text/hash" in report["rejections"][0]["reason"] or "hash" in report["rejections"][0]["reason"]

    def test_samples_jsonl_tampering_refused(self, packet: Path, tmp_path: Path):
        rows = _worksheet_rows(packet / wf.SAMPLES_NAME)
        rows[0]["text"] = "tampered"
        rows[0]["content_sha256"] = sha256_text("tampered")
        _write_jsonl(packet / wf.SAMPLES_NAME, rows)
        with pytest.raises(wf.WorkflowError, match="拒绝导入|指纹"):
            wf.import_worksheet(packet, tmp_path / "imp")

    def test_conflicting_pair_labels_surfaced_not_dropped(self, packet: Path, tmp_path: Path):
        def both_relations(rows):
            def mk(row, relation):
                row["reviewer_state"] = "reviewed"
                row["reviewer_id"] = "unit-reviewer"
                row["provenance"] = {
                    "source": "human", "dataset_id": "p2-human-unit-workflow",
                    "producer_id": "annotator-unit", "produced_at": None, "synthetic": False,
                }
                row["pair_relation"] = relation
            mk(rows[0], "same_need")
            extra = json.loads(json.dumps(rows[0]))
            anchor = _worksheet_rows(packet / wf.EXTRACTION_WORKSHEET_NAME)[1]
            extra["sample_id"] = anchor["sample_id"]
            extra["right_sample_id"] = rows[0]["sample_id"]
            extra["source_id"] = anchor["source_id"]
            extra["family_id"] = anchor["family_id"]
            extra["text"] = anchor["text"]
            extra["content_sha256"] = anchor["content_sha256"]
            mk(extra, "different_need")
            rows.append(extra)
        _set_row_value(packet, wf.PAIR_WORKSHEET_NAME, both_relations)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["conflicting_pairs"], "冲突必须出现在报告里"
        assert report["counts"]["duplicate_conflict_rows"] == 2
        # 反序的已申报对（同一无序身份）不被当作未申报而拒绝：
        assert not any("未在 packet 候选对清单中申报" in r["reason"] for r in report["rejections"])
        # 冲突两行都不进产品文件（绝不任选其一采信），只有未动的 demo-3/demo-5 候选行保留。
        kept = load_annotation_records(tmp_path / "imp" / wf.VALIDATED_ANNOTATIONS_NAME)
        pair_records = [r for r in kept if isinstance(r, PairAnnotationRecord)]
        assert len(pair_records) == 1
        assert sorted((pair_records[0].sample_id, pair_records[0].right_sample_id)) == ["demo-3", "demo-5"]
        # 冲突原件连同原因完整保留在隔离 sidecar：两个相反 relation 都不被丢弃。
        quarantine = _quarantine_rows(tmp_path / "imp")
        conflict_rows = [
            r for r in quarantine
            if sorted((r["sample_id"], r["right_sample_id"])) == ["demo-1", "demo-2"]
        ]
        assert len(conflict_rows) == 2, "冲突记录不得被丢弃"
        assert {r["pair_relation"] for r in conflict_rows} == {"same_need", "different_need"}
        assert all(r["_quarantine"]["stage"] == "duplicate_conflict" for r in conflict_rows)
        assert report["ready"] == "NOT_READY"

    def test_empty_worksheet_import_not_ready(self, tmp_path: Path, sample_manifest: Path):
        out = tmp_path / "pkt"
        wf.prepare_packet(sample_manifest, out)
        _write_jsonl(out / wf.EXTRACTION_WORKSHEET_NAME, [])
        _write_jsonl(out / wf.PAIR_WORKSHEET_NAME, [])
        report = wf.import_worksheet(out, tmp_path / "imp")
        assert report["ready"] == "NOT_READY"
        assert report["counts"]["valid_records"] == 0
        assert "拒绝切分" in report["split"]["reason"]

    def test_insufficient_groups_not_ready(self, tmp_path: Path):
        rows = [_samples()[0], {**_samples()[1], "source_id": "note:demo-1", "family_id": "fam-demo-a"}]
        # 两个样本共享 source 与 family -> 只有 1 个组，group_split 必须拒评
        manifest = _write_jsonl(tmp_path / "s.jsonl", rows)
        out = tmp_path / "pkt"
        wf.prepare_packet(manifest, out)
        def review(rws):
            for row in rws:
                row["reviewer_state"] = "reviewed"
                row["reviewer_id"] = "unit-reviewer"
                row["provenance"] = {
                    "source": "human", "dataset_id": "p2-human-unit-workflow",
                    "producer_id": "annotator-unit", "produced_at": None, "synthetic": False,
                }
                row["label"] = {k: False for k in LABEL_KEYS} | {"explicit_problem": True}
        _set_row_value(out, wf.EXTRACTION_WORKSHEET_NAME, review)
        report = wf.import_worksheet(out, tmp_path / "imp")
        assert report["split"]["validated"] is False
        assert "分组不足" in report["split"]["reason"]
        assert report["ready"] == "NOT_READY"


class TestPacketIntegrityAndOrigins:
    """packet 完整性（不可变身份清单）+ 重复/来源隔离的正向与拒绝用例。"""

    def test_candidate_pairs_manifest_is_immutable_and_frozen(self, packet: Path):
        # 候选对身份冻结在独立不可变清单，而非可编辑工作表；packet 登记其哈希。
        manifest = wf.PacketManifest.model_validate(
            _worksheet_rows(packet / wf.PACKET_MANIFEST_NAME)[0]
        )
        assert wf.CANDIDATE_PAIRS_NAME in manifest.files
        assert sha256_text((packet / wf.CANDIDATE_PAIRS_NAME).read_text("utf-8")) == (
            manifest.files[wf.CANDIDATE_PAIRS_NAME]
        )
        frozen = [json.loads(line) for line in (packet / wf.CANDIDATE_PAIRS_NAME).read_text("utf-8").splitlines() if line.strip()]
        assert sorted(tuple(sorted((p["left_sample_id"], p["right_sample_id"]))) for p in frozen) == [
            ("demo-1", "demo-2"), ("demo-3", "demo-5"),
        ]

    def test_tampered_immutable_candidate_pairs_refused(self, packet: Path, tmp_path: Path):
        rows = [json.loads(line) for line in (packet / wf.CANDIDATE_PAIRS_NAME).read_text("utf-8").splitlines() if line.strip()]
        rows[0]["right_sample_id"] = "demo-3"  # 篡改冻结的候选对身份
        _write_jsonl(packet / wf.CANDIDATE_PAIRS_NAME, rows)
        with pytest.raises(wf.WorkflowError, match="candidate_pairs.jsonl|指纹|篡改"):
            wf.import_worksheet(packet, tmp_path / "imp")

    def test_editable_pair_worksheet_label_edits_do_not_break_packet(self, packet: Path, tmp_path: Path):
        # 只编辑 pair_worksheet 的 label/review 字段（不动身份清单）必须被接受——
        # 早期实现按整文件哈希锁定，会误拒真实人工标注；这里回归该边界。
        def review_first_pair(rows):
            row = rows[0]
            row["reviewer_state"] = "reviewed"
            row["reviewer_id"] = "unit-reviewer"
            row["provenance"] = {
                "source": "human", "dataset_id": "p2-human-unit-workflow",
                "producer_id": "annotator-unit", "produced_at": None, "synthetic": False,
            }
            row["pair_relation"] = "same_need"
        _set_row_value(packet, wf.PAIR_WORKSHEET_NAME, review_first_pair)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["counts"]["rejected_rows"] == 0
        assert report["human_dataset_status"]["human_reviewed_count"] == 1  # 落在 actual_source 对上

    def test_undeclared_candidate_pair_rejected(self, packet: Path, tmp_path: Path):
        def add_undeclared(rows):
            extra = json.loads(json.dumps(rows[0]))
            extra["sample_id"] = "demo-1"
            extra["right_sample_id"] = "demo-3"  # 未在 packet 候选对清单中申报的无序对
            extra["reviewer_state"] = "unlabeled"
            extra["pair_relation"] = "unknown"
            extra["label"] = None
            rows.append(extra)
        _set_row_value(packet, wf.PAIR_WORKSHEET_NAME, add_undeclared)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert any("未在 packet 候选对清单中申报" in r["reason"] for r in report["rejections"])
        assert report["ready"] == "NOT_READY"

    def test_duplicate_identical_rows_folded_not_inflated(self, packet: Path, tmp_path: Path):
        base = _worksheet_rows(packet / wf.EXTRACTION_WORKSHEET_NAME)
        for row in base:
            row["reviewer_state"] = "reviewed"
            row["reviewer_id"] = "unit-reviewer"
            row["provenance"] = {
                "source": "human", "dataset_id": "p2-human-unit-workflow",
                "producer_id": "annotator-unit", "produced_at": None, "synthetic": False,
            }
            row["label"] = {k: False for k in LABEL_KEYS} | {"explicit_problem": True}
        actual_ids = {"demo-1", "demo-2", "demo-3"}
        duplicated = [json.loads(json.dumps(r)) for r in base]
        for row in base:
            if row["sample_id"] in actual_ids:
                duplicated.extend(json.loads(json.dumps(row)) for _ in range(100))
        _write_jsonl(tmp_path / "dup.jsonl", duplicated)
        report = wf.import_worksheet(packet, tmp_path / "imp", extraction_path=tmp_path / "dup.jsonl")
        assert report["ready"] == "NOT_READY"
        # 完全相同的重复折叠到唯一副本：真实来源金标 = 3（demo-1/2/3），绝不膨胀到 300+。
        assert report["human_dataset_status"]["human_reviewed_count"] == 3
        assert report["counts"]["duplicates_folded"] >= 1
        assert report["counts"]["duplicate_conflict_rows"] == 0

    def test_mixed_actual_synthetic_pair_gold_quarantined(self, packet: Path, tmp_path: Path):
        def review_synth_pair(rows):
            row = rows[1]  # (demo-3 actual, demo-5 synthetic)：一端非 actual_source
            row["reviewer_state"] = "reviewed"
            row["reviewer_id"] = "unit-reviewer"
            row["provenance"] = {
                "source": "human", "dataset_id": "p2-human-unit-workflow",
                "producer_id": "annotator-unit", "produced_at": None, "synthetic": False,
            }
            row["pair_relation"] = "same_need"
        _set_row_value(packet, wf.PAIR_WORKSHEET_NAME, review_synth_pair)
        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["human_gold_on_non_actual_samples"]
        assert report["ready"] == "NOT_READY"
        kept = load_annotation_records(tmp_path / "imp" / wf.VALIDATED_ANNOTATIONS_NAME)
        assert not any(
            isinstance(r, PairAnnotationRecord) and sorted((r.sample_id, r.right_sample_id)) == ["demo-3", "demo-5"]
            for r in kept
        ), "混合非 actual_source 的 human gold 对不得进入产品文件（重载绕路被阻断）"
        quarantine = _quarantine_rows(tmp_path / "imp")
        origin_q = [q for q in quarantine if q.get("_quarantine", {}).get("stage") == "origin"]
        assert any(sorted((q["sample_id"], q["right_sample_id"])) == ["demo-3", "demo-5"] for q in origin_q)


class TestPathSafety:
    def test_packet_manifest_rejects_traversal_file_names(self):
        base = {
            "version": wf.WORKFLOW_VERSION, "packet_id": "p",
            "sample_manifest_sha256": "a" * 64, "pairs_manifest_sha256": None,
            "samples_fingerprint": "b" * 64, "pairs_fingerprint": "c" * 64,
            "packet_fingerprint": wf._packet_fingerprint("b" * 64, "c" * 64),
            "n_samples": 0, "n_pairs": 0, "origin_counts": {},
        }
        with pytest.raises(Exception, match="目录穿越"):
            wf.PacketManifest.model_validate({**base, "files": {"../escape.jsonl": "d" * 64}})
        with pytest.raises(Exception, match="裸文件名"):
            wf.PacketManifest.model_validate({**base, "files": {"sub/dir.jsonl": "d" * 64}})

    def test_output_inside_real_templates_dir_refused(self, tmp_path: Path, sample_manifest: Path):
        target = Path(wf.PROJECT_ROOT) / "evaluation" / "templates" / "should_never_be_written"
        with pytest.raises(wf.PathRestrictedError):
            wf.prepare_packet(sample_manifest, target)
        assert not target.exists()
        assert (Path(wf.PROJECT_ROOT) / "evaluation" / "templates").is_dir()

    def test_protected_roots_enforced_via_project_root(self, tmp_path: Path, monkeypatch, sample_manifest: Path):
        fake_root = tmp_path / "proj"
        for name in ("data", "data_live", "exports"):
            (fake_root / name).mkdir(parents=True)
        (fake_root / "evaluation").mkdir()
        (fake_root / "evaluation" / "metrics.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(wf, "PROJECT_ROOT", fake_root)
        for rel in ("data_live/runs", "exports/x", "data/y", "evaluation/templates"):
            with pytest.raises(wf.PathRestrictedError):
                wf.prepare_packet(sample_manifest, fake_root / rel)
        metrics_out = fake_root / "evaluation" / "metrics.json"
        with pytest.raises(wf.PathRestrictedError):
            wf.prepare_packet(sample_manifest, metrics_out)

    def test_import_out_dir_overwrite_refused(self, packet: Path, tmp_path: Path):
        imp = tmp_path / "imp"
        wf.import_worksheet(packet, imp)
        with pytest.raises(wf.PacketExistsError):
            wf.import_worksheet(packet, imp)


class TestPositiveUnitImport:
    def test_real_shaped_manual_review_import_unit_case(self, packet: Path, tmp_path: Path):
        """真实形态的人工复核导入正例（合成 UNIT 模拟，不是真实人工数据集）。

        验证：显式真人身份 + 明确标签可以被接受、契约可加载、切分经
        group_split/validate_split 复核通过；但 3 条 < 200 门槛，必须 NOT_READY。
        """
        def annotate(rows):
            for row in rows[:3]:
                row["reviewer_state"] = "reviewed"
                row["reviewer_id"] = "unit-reviewer-real-shape"
                row["provenance"] = {
                    "source": "human", "dataset_id": "p2-human-unit-workflow",
                    "producer_id": "annotator-unit-real-shape", "produced_at": None, "synthetic": False,
                }
                row["label"] = {k: False for k in LABEL_KEYS} | {"explicit_problem": True, "region": True}
            rows[3]["reviewer_state"] = "draft"
            rows[3]["provenance"] = {
                "source": "model_generated", "dataset_id": "p2-model-unit-workflow",
                "producer_id": "model:unit", "produced_at": None, "synthetic": True,
            }
            rows[3]["label"] = {k: None for k in LABEL_KEYS}
        _set_row_value(packet, wf.EXTRACTION_WORKSHEET_NAME, annotate)

        def pair_reviewed(rows):
            row = rows[0]
            row["reviewer_state"] = "reviewed"
            row["reviewer_id"] = "unit-reviewer-real-shape"
            row["provenance"] = {
                "source": "human", "dataset_id": "p2-human-unit-workflow",
                "producer_id": "annotator-unit-real-shape", "produced_at": None, "synthetic": False,
            }
            row["pair_relation"] = "same_need"
        _set_row_value(packet, wf.PAIR_WORKSHEET_NAME, pair_reviewed)

        report = wf.import_worksheet(packet, tmp_path / "imp")
        assert report["counts"]["rejected_rows"] == 0
        status = report["human_dataset_status"]
        assert status["human_reviewed_count"] == 4  # 3 抽取 + 1 对（真实形态但合成 UNIT）
        assert status["status"] == "NOT_MET"
        assert report["split"]["validated"] is True
        assert len(report["split"]["fingerprint"]) == 64
        assert report["human_gold_on_non_actual_samples"] == []
        assert report["ready"] == "NOT_READY"
        assert any("最低要求" in reason for reason in status["reasons"])
        kept = load_annotation_records(tmp_path / "imp" / wf.VALIDATED_ANNOTATIONS_NAME)
        assert len(kept) == 7  # 5 抽取（3 human + 1 model draft + 1 未动）+ 2 对（1 reviewed + 1 未动）
        by_id = {r.sample_id: r for r in kept if isinstance(r, ExtractionAnnotationRecord)}
        for s in _samples()[:3]:
            assert by_id[s["sample_id"]].text == s["text"]
            assert by_id[s["sample_id"]].content_sha256 == hashlib.sha256(s["text"].encode()).hexdigest()


class TestCli:
    def test_prepare_and_import_cli(self, tmp_path: Path, sample_manifest: Path, pairs_manifest: Path, capsys):
        assert wf.main(["prepare", "--manifest", str(sample_manifest), "--pairs", str(pairs_manifest),
                        "--out", str(tmp_path / "pkt")]) == 0
        out = capsys.readouterr().out
        assert wf.WORKFLOW_VERSION in out
        code = wf.main(["import", "--packet", str(tmp_path / "pkt"), "--out", str(tmp_path / "imp")])
        assert code == 0
        report_text = capsys.readouterr().out
        assert '"ready": "NOT_READY"' in report_text

    def test_cli_error_exit_2(self, tmp_path: Path, capsys):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert wf.main(["prepare", "--manifest", str(empty), "--out", str(tmp_path / "pkt")]) == 2
        assert "工作流拒绝" in capsys.readouterr().err
