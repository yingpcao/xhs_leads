"""Local worksheet editor; immutable packet identities and existing gold rules remain authoritative."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from xhs_discovery.evaluation import annotation_workflow as wf
from xhs_discovery.evaluation.annotations import ExtractionAnnotationRecord, PairAnnotationRecord
from xhs_discovery.relpath import ensure_inside
from xhs_discovery.storage.worker import OsGuard


def load_sheet(packet: Path, kind: str):
    if kind not in ("extraction", "pair"):
        raise wf.WorkflowError("未知标注类型")
    packet = Path(packet).resolve()
    manifest, samples, pairs = wf._load_packet(packet)
    name = wf.EXTRACTION_WORKSHEET_NAME if kind == "extraction" else wf.PAIR_WORKSHEET_NAME
    path = Path(ensure_inside(packet / name, packet))
    raw = path.read_bytes()
    model = ExtractionAnnotationRecord if kind == "extraction" else PairAnnotationRecord
    records = [model.model_validate(json.loads(line)) for line in raw.decode("utf-8").splitlines() if line.strip()]
    by_id = {s.sample_id: s for s in samples}
    declared = {tuple(sorted((p.left_sample_id, p.right_sample_id))) for p in pairs}
    seen = set()
    for record in records:
        reason = wf._workflow_check(record, by_id, kind)
        if reason:
            raise wf.WorkflowError(reason)
        identity = wf._identity_key(record)
        if identity in seen:
            raise wf.WorkflowError("工作表存在重复身份，请先解决重复记录")
        seen.add(identity)
        if kind == "pair" and identity[1] not in declared:
            raise wf.WorkflowError("候选对未在冻结清单中申报")
    return manifest, by_id, records, hashlib.sha256(raw).hexdigest()


def save_annotation(packet: Path, kind: str, index: int, expected_digest: str,
                    *, producer: str, dataset: str, reviewer: str,
                    state: str, label: dict | None = None, relation: str = "unknown") -> str:
    packet = wf._assert_not_protected(Path(packet), "标注目录")
    guard = OsGuard(packet / ".annotation-editor.guard")
    if not guard.try_acquire():
        raise wf.WorkflowError("另一窗口正在保存，请稍后重试")
    try:
        _, samples, records, digest = load_sheet(packet, kind)
        if digest != expected_digest:
            raise wf.WorkflowError("工作表已被其他窗口修改，请重新加载后再保存")
        if not 0 <= index < len(records):
            raise wf.WorkflowError("标注行不存在")
        original = records[index]
        if original.provenance.source != "human":
            raise wf.WorkflowError("模型或合成标签不可在此改成真人标注")
        if state not in ("draft", "reviewed", "adjudicated"):
            raise wf.WorkflowError("请选择草稿、已复核或已裁决")
        producer, dataset, reviewer = producer.strip(), dataset.strip(), reviewer.strip()
        if not producer or not dataset or (state != "draft" and not reviewer):
            raise wf.WorkflowError("请填写标注人、数据集；复核或裁决时还需填写复核人")
        payload = original.model_dump(mode="json")
        payload["provenance"].update(producer_id=producer, dataset_id=dataset, produced_at=None)
        payload.update(reviewer_state=state, reviewer_id=reviewer or None)
        if kind == "extraction":
            payload["label"] = label
        else:
            payload["pair_relation"] = relation
        updated = type(original).model_validate(payload)
        reason = wf._workflow_check(updated, samples, kind)
        if reason:
            raise wf.WorkflowError(reason)
        records[index] = updated
        name = wf.EXTRACTION_WORKSHEET_NAME if kind == "extraction" else wf.PAIR_WORKSHEET_NAME
        target = Path(ensure_inside(packet / name, packet))
        # Keep the previous exact bytes; save atomically so a crash cannot truncate a worksheet.
        history = Path(ensure_inside(packet / ".annotation-history", packet))
        history.mkdir(exist_ok=True)
        backup = history / f"{name}.{digest}.bak"
        if not backup.exists():
            with backup.open("xb") as out:
                out.write(target.read_bytes())
        text = "".join(wf._dump_row(r) + "\n" for r in records)
        fd, temporary = tempfile.mkstemp(prefix=".annotation-", suffix=".tmp", dir=packet)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    finally:
        guard.release()


def validate_packet(packet: Path, output: Path) -> dict:
    packet = wf._assert_not_protected(Path(packet), "标注目录")
    guard = OsGuard(packet / ".annotation-editor.guard")
    if not guard.try_acquire():
        raise wf.WorkflowError("另一窗口正在保存，请稍后重试")
    try:
        return wf.import_worksheet(packet, output)
    finally:
        guard.release()
