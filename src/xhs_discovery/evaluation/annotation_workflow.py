"""P2-B 离线人工标注准备/校验工作流（p2-annotation-workflow-v1）。

边界（P2-B-ANNOTATION-WORKFLOW-r1）：
- 只复用既有契约：标注记录/来源/复核态用 `annotations`，切分用 `splits`，
  人工集门槛用 `human_dataset_status`；本模块不实现第二套标注 schema 或指标；
- 只处理显式 supplied 的本地 JSONL 样本清单与候选对清单：绝不访问平台、
  不采样、不推断 same_need、不下载/加载模型；
- 工作表初始一律 unlabeled/blank/unknown；导入绝不自动签名、绝不把占位符
  provenance 静默升格为 human/reviewed；合成/模型来源样本永远带标记流转；
- JSONL 是权威载体（原文与哈希逐字节保真），CSV 只是电子表格镜像：
  对以 = + - @ 或制表符开头的单元格加 `'` 护栏，防止公式注入；
- 不写 data/、data_live/、exports/、evaluation/metrics.json、evaluation/templates/；
  输出目录非空即拒绝，不覆盖任何已有工件。

样本出处（sample origin）与标注 provenance 的最小设计权衡（r2 加固）：
既有 `AnnotationProvenance` 只描述"标签由谁产生"，没有字段描述"样本原文由谁
产生"，且 LabelSource 不含 actual_source。为不放宽 synthetic 门禁也不改
既有 gold 规则，本模块把样本出处独立保存在 packet 的 samples.jsonl，导入时
按 sample_id+content_sha256 关联回来；对 human gold 落在非 actual_source 样本
上的记录，本模块 **把它们从产品规范文件 `annotations_validated.jsonl` 隔离到
sidecar `annotations_quarantined.jsonl` 并附原因**（原样保留、绝不静默丢弃、
绝不重新标记 actual 评审者为 synthetic），下游 `load_annotation_records` 只
读产品文件即无法再拿到伪造 gold。产品就绪报告仍来自 `human_dataset_status`
/`is_human_gold`（既有 gold API 规则一字未改），READY 还额外要求隔离数为 0、
无重复冲突、无 rejections。身份去重在 `human_dataset_status` 之前完成：
extraction 按 sample_id、pair 按无序端点对——不同任务永不冲突；重复行完全
相同则折叠到唯一副本，任何字段不同即视为冲突，两条原件都进隔离文件。
packet 加载校验的是**不可变**清单：样本 `samples.jsonl` 逐字节哈希 + samples 指纹、
冻结的候选对身份清单 `candidate_pairs.jsonl` 逐字节哈希 + pairs 指纹、以及清单自洽的
packet_fingerprint。可编辑的 `pair_worksheet.jsonl` 是标注人回传 label/review 字段的
载体，绝不整文件哈希锁定；导入时把它每行的无序端点与冻结身份集合比对——未申报的对被
拒绝，反序的已申报对（同一身份）被允许但与其它同身份行的差异作为冲突浮出并隔离。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Literal

from pydantic import Field, field_validator, model_validator

from xhs_discovery.evaluation.annotations import (
    ANNOTATION_SCHEMA_VERSION,
    EXTRACTION_LABEL_FIELDS,
    AnnotationProvenance,
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
    human_dataset_status,
)
from xhs_discovery.evaluation.splits import (
    InsufficientGroupsError,
    SplitItem,
    group_split,
    validate_split,
)
from xhs_discovery.models.common import StrictModel, sha256_text
from xhs_discovery.relpath import ensure_inside, split_relative

WORKFLOW_VERSION = "p2-annotation-workflow-v1"

PACKET_MANIFEST_NAME = "packet_manifest.json"
SAMPLES_NAME = "samples.jsonl"
EXTRACTION_WORKSHEET_NAME = "extraction_worksheet.jsonl"
EXTRACTION_WORKSHEET_CSV = "extraction_worksheet.csv"
PAIR_WORKSHEET_NAME = "pair_worksheet.jsonl"
PAIR_WORKSHEET_CSV = "pair_worksheet.csv"
# 冻结的候选对身份清单：不可变，_load_packet 逐字节校验其哈希/指纹。
# 与可编辑的 pair_worksheet.jsonl 分离——标注人只编辑后者的 label/review 字段，
# 永不因编辑标签而破坏 packet 完整性；未申报对由身份集合比对拒绝。
CANDIDATE_PAIRS_NAME = "candidate_pairs.jsonl"
PACKET_README_NAME = "PACKET_README.md"
VALIDATED_ANNOTATIONS_NAME = "annotations_validated.jsonl"
QUARANTINED_ANNOTATIONS_NAME = "annotations_quarantined.jsonl"
IMPORT_REPORT_NAME = "import_report.json"

# 与 evaluation/templates/ANNOTATION_INSTRUCTIONS.md 的空模板占位符约定一致；
# 导入时任何仍处于占位符的身份字段都不构成"显式真人复核动作"。
PLACEHOLDER_PRODUCER = "<标注人>"
PLACEHOLDER_DATASET_EXTRACTION = "p2-human-extraction-YYYYMMDD"
PLACEHOLDER_DATASET_PAIR = "p2-human-pair-YYYYMMDD"
PLACEHOLDER_SET = {PLACEHOLDER_PRODUCER, PLACEHOLDER_DATASET_EXTRACTION, PLACEHOLDER_DATASET_PAIR}

SampleOrigin = Literal["actual_source", "synthetic_fixture", "model_generated"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class WorkflowError(ValueError):
    """工作流拒绝任何含糊输入：宁可报错，绝不猜测。"""


class PacketExistsError(WorkflowError):
    """输出目录非空：既有 packet/导入结果不可覆盖（未来标注必须被保护）。"""


class PathRestrictedError(WorkflowError):
    """写路径落入受保护真实数据/指标/模板区，或清单登记了越界文件名。"""


def _protected_paths() -> list[Path]:
    root = PROJECT_ROOT
    return [
        root / "data",
        root / "data_live",
        root / "exports",
        root / "evaluation" / "metrics.json",
        root / "evaluation" / "templates",
    ]


def _assert_not_protected(target: Path, purpose: str) -> Path:
    resolved = Path(target).resolve()
    for protected in _protected_paths():
        prot = Path(protected).resolve()
        if resolved == prot or resolved.is_relative_to(prot):
            raise PathRestrictedError(f"{purpose} 指向受保护路径 {prot}，拒绝写入：{resolved}")
    return resolved


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dump_row(model: StrictModel) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise WorkflowError(f"无法读取 {path}：{error}") from error
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise WorkflowError(f"{path} 第 {line_no} 行不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise WorkflowError(f"{path} 第 {line_no} 行必须是 JSON 对象，收到 {type(payload).__name__}")
        rows.append(payload)
    return rows


class PacketSample(StrictModel):
    """packet 样本清单行：原文、哈希、来源/家族元数据与样本出处显式分离于标注 provenance。"""

    schema_version: str
    sample_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    text: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: SampleOrigin
    origin_producer_id: str = Field(min_length=1)
    origin_dataset_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "PacketSample":
        if self.schema_version != WORKFLOW_VERSION:
            raise ValueError(f"样本清单 schema_version 必须是 {WORKFLOW_VERSION}")
        if sha256_text(self.text) != self.content_sha256:
            raise ValueError(f"样本 {self.sample_id} 的 content_sha256 与 text 不一致")
        producer = self.origin_producer_id
        if self.origin == "actual_source" and producer.startswith(("model:", "synthetic:")):
            raise ValueError(
                f"样本 {self.sample_id}: origin_producer_id={producer!r} 表明模型/合成产物，"
                "不能声明 actual_source（拒绝猜测，也不放宽 synthetic 门禁）"
            )
        if self.origin == "model_generated" and not producer.startswith("model:"):
            raise ValueError(f"样本 {self.sample_id}: origin=model_generated 必须使用 model: 前缀的 producer")
        if self.origin == "synthetic_fixture" and not producer.startswith("synthetic:"):
            raise ValueError(f"样本 {self.sample_id}: origin=synthetic_fixture 必须使用 synthetic: 前缀的 producer")
        return self


class CandidatePair(StrictModel):
    """显式 supplied 的候选语义对；工作流绝不自行推断 same_need。"""

    left_sample_id: str = Field(min_length=1)
    right_sample_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "CandidatePair":
        if self.left_sample_id == self.right_sample_id:
            raise ValueError(f"候选对两端不能相同：{self.left_sample_id}")
        return self


class PacketManifest(StrictModel):
    version: str
    packet_id: str = Field(min_length=1)
    sample_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pairs_manifest_sha256: str | None = None
    samples_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    pairs_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    n_samples: int = Field(ge=0)
    n_pairs: int = Field(ge=0)
    origin_counts: dict[str, int]
    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def _file_names_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            parts = split_relative(name)
            if len(parts) != 1:
                raise ValueError(f"packet 文件登记名必须是目录内裸文件名：{name!r}")
        return value

    @model_validator(mode="after")
    def _check(self) -> "PacketManifest":
        if self.version != WORKFLOW_VERSION:
            raise ValueError(f"packet 版本必须是 {WORKFLOW_VERSION}")
        expected = _packet_fingerprint(self.samples_fingerprint, self.pairs_fingerprint)
        if self.packet_fingerprint != expected:
            raise ValueError("packet_fingerprint 与 samples/pairs 指纹不一致：清单可能被篡改")
        return self


def _fingerprint_of(payload: object) -> str:
    return sha256_text(_canonical_json(payload))


def _packet_fingerprint(samples_fp: str, pairs_fp: str) -> str:
    return _fingerprint_of({"version": WORKFLOW_VERSION, "samples": samples_fp, "pairs": pairs_fp})


def load_sample_manifest(path: Path) -> list[PacketSample]:
    rows = _read_jsonl(path)
    if not rows:
        raise WorkflowError(f"样本清单 {path} 为空：没有任何可打包样本，拒绝生成 packet")
    samples: list[PacketSample] = []
    seen: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(rows, start=1):
        try:
            sample = PacketSample.model_validate(row)
        except Exception as error:  # noqa: BLE001 - 逐行归因，未知字段/坏哈希在此拒绝
            raise WorkflowError(f"样本清单第 {index} 行被拒绝：{error}") from error
        meta = (sample.source_id, sample.family_id)
        if sample.sample_id in seen:
            conflict = "" if seen[sample.sample_id] == meta else "且来源/家族元数据冲突"
            raise WorkflowError(
                f"样本清单第 {index} 行重复 sample_id {sample.sample_id!r}{conflict}：拒绝猜测"
            )
        seen[sample.sample_id] = meta
        samples.append(sample)
    return samples


def load_candidate_pairs(path: Path, sample_ids: Iterable[str]) -> list[CandidatePair]:
    known = set(sample_ids)
    rows = _read_jsonl(path)
    pairs: list[CandidatePair] = []
    seen_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        try:
            pair = CandidatePair.model_validate(row)
        except Exception as error:  # noqa: BLE001
            raise WorkflowError(f"候选对清单第 {index} 行被拒绝：{error}") from error
        unknown = sorted(
            s for s in (pair.left_sample_id, pair.right_sample_id) if s not in known
        )
        if unknown:
            raise WorkflowError(
                f"候选对清单第 {index} 行引用了样本清单之外的 sample_id：{unknown}，拒绝猜测"
            )
        key = tuple(sorted((pair.left_sample_id, pair.right_sample_id)))
        if key in seen_keys:
            raise WorkflowError(f"候选对清单第 {index} 行重复无序对 {list(key)}（含反序）")
        seen_keys.add(key)
        pairs.append(pair)
    return pairs


def compute_samples_fingerprint(samples: list[PacketSample]) -> str:
    return _fingerprint_of([sample.model_dump(mode="json") for sample in sorted(samples, key=lambda s: s.sample_id)])


def compute_pairs_fingerprint(pairs: list[CandidatePair]) -> str:
    normalized = sorted(
        (tuple(sorted((p.left_sample_id, p.right_sample_id))) for p in pairs)
    )
    return _fingerprint_of([list(item) for item in normalized])


FORMULA_SAFE_PREFIX = "'"
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def guard_csv_cell(value: str) -> str:
    """电子表格镜像护栏：以公式触发字符开头的文本加前缀，仅影响 CSV 视图。

    JSONL 权威文件从不做此变换，原文与哈希保持逐字节一致。
    """
    if value.startswith(_FORMULA_LEADERS):
        return FORMULA_SAFE_PREFIX + value
    return value


def _csv_rows_text(header: list[str], data_rows: list[list[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(data_rows)
    return buffer.getvalue()


def _worksheet_extraction_row(sample: PacketSample) -> ExtractionAnnotationRecord:
    return ExtractionAnnotationRecord(
        schema_version=ANNOTATION_SCHEMA_VERSION,
        sample_id=sample.sample_id,
        source_id=sample.source_id,
        family_id=sample.family_id,
        content_sha256=sample.content_sha256,
        text=sample.text,
        provenance=AnnotationProvenance(
            source="human",
            dataset_id=PLACEHOLDER_DATASET_EXTRACTION,
            producer_id=PLACEHOLDER_PRODUCER,
            produced_at=None,
            synthetic=False,
        ),
        reviewer_state="unlabeled",
        reviewer_id=None,
        label=None,
    )


def _worksheet_pair_row(pair: CandidatePair, samples_by_id: dict[str, PacketSample]) -> PairAnnotationRecord:
    anchor = samples_by_id[pair.left_sample_id]
    return PairAnnotationRecord(
        schema_version=ANNOTATION_SCHEMA_VERSION,
        sample_id=pair.left_sample_id,
        right_sample_id=pair.right_sample_id,
        source_id=anchor.source_id,
        family_id=anchor.family_id,
        content_sha256=anchor.content_sha256,
        text=anchor.text,
        provenance=AnnotationProvenance(
            source="human",
            dataset_id=PLACEHOLDER_DATASET_PAIR,
            producer_id=PLACEHOLDER_PRODUCER,
            produced_at=None,
            synthetic=False,
        ),
        reviewer_state="unlabeled",
        reviewer_id=None,
        label=None,
        pair_relation="unknown",
    )


EXTRACTION_CSV_HEADER = (
    ["sample_id", "source_id", "family_id", "content_sha256", "text", "sample_origin"]
    + ["provenance_source", "provenance_dataset_id", "provenance_producer_id",
       "reviewer_state", "reviewer_id"]
    + list(EXTRACTION_LABEL_FIELDS)
)


def _extraction_csv_rows(rows: list[ExtractionAnnotationRecord], origins: dict[str, str]) -> list[list[str]]:
    data = []
    for record in rows:
        label = record.label or {}
        data.append(
            [
                record.sample_id,
                record.source_id,
                record.family_id,
                record.content_sha256,
                record.text,
                origins.get(record.sample_id, "unknown"),
                record.provenance.source,
                record.provenance.dataset_id,
                record.provenance.producer_id,
                record.reviewer_state,
                record.reviewer_id or "",
            ]
            + ["" if label.get(name) is None else str(label[name]).lower() for name in EXTRACTION_LABEL_FIELDS]
        )
    return [[guard_csv_cell(cell) for cell in row] for row in data]


def _pair_csv_rows(rows: list[PairAnnotationRecord], origins: dict[str, str]) -> list[list[str]]:
    header = [
        "sample_id", "right_sample_id", "source_id", "family_id", "content_sha256", "text",
        "left_sample_origin", "right_sample_origin", "provenance_source",
        "provenance_dataset_id", "provenance_producer_id", "reviewer_state", "reviewer_id", "pair_relation",
    ]
    data = [
        [
            record.sample_id,
            record.right_sample_id,
            record.source_id,
            record.family_id,
            record.content_sha256,
            record.text,
            origins.get(record.sample_id, "unknown"),
            origins.get(record.right_sample_id, "unknown"),
            record.provenance.source,
            record.provenance.dataset_id,
            record.provenance.producer_id,
            record.reviewer_state,
            record.reviewer_id or "",
            record.pair_relation,
        ]
        for record in rows
    ]
    return header, [[guard_csv_cell(cell) for cell in row] for row in data]


README_TEMPLATE = """# 标注 packet 使用说明（{packet_id}）

- 权威标注载体是 `extraction_worksheet.jsonl` / `pair_worksheet.jsonl`（UTF-8，逐行 JSON）。
  同目录 `*.csv` 只是电子表格镜像：以 = + - @ 开头的单元格会被加 `'` 护栏，**不要**用 CSV 回传。
- 原始样本文本与 `content_sha256` 由 packet 冻结，标注人不得改动 text/hash/source_id/family_id；
  导入时会逐条比对，任何漂移都会被拒绝。
- 候选对身份冻结在 `candidate_pairs.jsonl`（**不可变**，packet 完整性校验的对象）；请只在
  `pair_worksheet.jsonl` 里填写每条对的 `pair_relation`/复核字段。**不要**增删行或改动端点：
  未申报的无序对会被拒绝，反序填写同一已申报对会被允许，但与其它同身份行的差异会作为冲突浮出并隔离。
- 每行初始 `reviewer_state="unlabeled"`、label/`pair_relation` 为 null/unknown。请按真实标注动作填写：
  1. 把 `provenance.dataset_id` 与 `provenance.producer_id` 的占位符改成真实数据集编号与真实标注人；
  2. 完成标注后把 `reviewer_state` 升为 `draft` 或 `reviewed`（`reviewed` 必须填 `reviewer_id`）；
  3. `reviewed`/`adjudicated` 的行必须携带至少一个明确标签（抽取六维至少一个 true/false；pair 必须
     same_need/different_need），否则导入将被拒绝——工作流不会替你签名或代填标签。
- 看不准就写 null(unknown)。全 null 的 draft 是合法在途标注，但不会被计入人工金标。
- 合成/模型来源样本（见 CSV 的 sample_origin 列或 samples.jsonl 的 origin 字段）照常标注，
  导入报告会单独清点，它们不会被算作真实人工数据集。
- packet 指纹：`{packet_fingerprint}`（samples: `{samples_fingerprint}` / pairs: `{pairs_fingerprint}`）。
"""


def _ensure_fresh_dir(target: Path, purpose: str) -> Path:
    resolved = _assert_not_protected(target, purpose)
    if resolved.exists() and any(resolved.iterdir()):
        raise PacketExistsError(f"{purpose} 目录非空，拒绝覆盖既有工件：{resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def prepare_packet(
    sample_manifest_path: Path,
    out_dir: Path,
    pairs_manifest_path: Path | None = None,
    packet_id: str | None = None,
) -> PacketManifest:
    """从显式 supplied 的样本/候选对清单生成独立 packet 目录（绝不覆盖非空目录）。"""
    samples = load_sample_manifest(sample_manifest_path)
    by_id = {sample.sample_id: sample for sample in samples}
    pairs = load_candidate_pairs(pairs_manifest_path, by_id.keys()) if pairs_manifest_path else []

    samples_fp = compute_samples_fingerprint(samples)
    pairs_fp = compute_pairs_fingerprint(pairs)
    fingerprint = _packet_fingerprint(samples_fp, pairs_fp)
    resolved_out = _ensure_fresh_dir(Path(out_dir), "packet 输出目录")

    extraction_rows = [_worksheet_extraction_row(sample) for sample in samples]
    pair_rows = [_worksheet_pair_row(pair, by_id) for pair in pairs]
    origins = {sample.sample_id: sample.origin for sample in samples}

    files: dict[str, str] = {}

    def write(name: str, text: str) -> None:
        (resolved_out / name).write_text(text, encoding="utf-8", newline="\n")
        files[name] = sha256_text(text)

    write(SAMPLES_NAME, "\n".join(_dump_row(sample) for sample in samples) + "\n")
    write(
        EXTRACTION_WORKSHEET_NAME,
        ("\n".join(_dump_row(row) for row in extraction_rows) + "\n") if extraction_rows else "",
    )
    write(
        EXTRACTION_WORKSHEET_CSV,
        "\ufeff" + _csv_rows_text(list(EXTRACTION_CSV_HEADER), _extraction_csv_rows(extraction_rows, origins)),
    )
    write(
        PAIR_WORKSHEET_NAME,
        ("\n".join(_dump_row(row) for row in pair_rows) + "\n") if pair_rows else "",
    )
    pair_header, pair_data = _pair_csv_rows(pair_rows, origins)
    write(PAIR_WORKSHEET_CSV, "\ufeff" + _csv_rows_text(pair_header, pair_data))
    # 冻结候选对身份到不可变清单：这是 packet 完整性校验的对象，标注人不编辑它。
    write(
        CANDIDATE_PAIRS_NAME,
        ("\n".join(_dump_row(pair) for pair in pairs) + "\n") if pairs else "",
    )
    write(
        PACKET_README_NAME,
        README_TEMPLATE.format(
            packet_id=packet_id or f"packet-{fingerprint[:16]}",
            packet_fingerprint=fingerprint,
            samples_fingerprint=samples_fp,
            pairs_fingerprint=pairs_fp,
        ),
    )

    manifest = PacketManifest(
        version=WORKFLOW_VERSION,
        packet_id=packet_id or f"packet-{fingerprint[:16]}",
        sample_manifest_sha256=sha256_text(Path(sample_manifest_path).read_text(encoding="utf-8")),
        pairs_manifest_sha256=(
            sha256_text(Path(pairs_manifest_path).read_text(encoding="utf-8"))
            if pairs_manifest_path
            else None
        ),
        samples_fingerprint=samples_fp,
        pairs_fingerprint=pairs_fp,
        packet_fingerprint=fingerprint,
        n_samples=len(samples),
        n_pairs=len(pairs),
        origin_counts=dict(Counter(sample.origin for sample in samples)),
        files=files,
    )
    write(PACKET_MANIFEST_NAME, _dump_row(manifest) + "\n")
    return manifest


def _load_packet(packet_dir: Path) -> tuple[PacketManifest, list[PacketSample], list[CandidatePair]]:
    packet_dir = Path(packet_dir).resolve()
    manifest_path = ensure_inside(packet_dir / PACKET_MANIFEST_NAME, packet_dir)
    if not Path(manifest_path).is_file():
        raise WorkflowError(f"packet 目录缺少 {PACKET_MANIFEST_NAME}：{packet_dir}")
    rows = _read_jsonl(Path(manifest_path))
    if len(rows) != 1:
        raise WorkflowError(f"{PACKET_MANIFEST_NAME} 必须恰好一行 JSON 对象，读到 {len(rows)} 行")
    manifest = PacketManifest.model_validate(rows[0])
    samples_path = packet_dir / SAMPLES_NAME
    if manifest.files.get(SAMPLES_NAME) != sha256_text(samples_path.read_text(encoding="utf-8")):
        raise WorkflowError(f"{SAMPLES_NAME} 与 packet 登记哈希不一致：样本可能被篡改，拒绝导入")
    samples = [PacketSample.model_validate(row) for row in _read_jsonl(samples_path)]
    recomputed = compute_samples_fingerprint(samples)
    if recomputed != manifest.samples_fingerprint:
        raise WorkflowError("samples 指纹与 packet 清单不一致：拒绝在不确定的 packet 上导入")
    declared_sample_ids = {sample.sample_id for sample in samples}
    pairs: list[CandidatePair] = []
    candidate_pairs_path = packet_dir / CANDIDATE_PAIRS_NAME
    if CANDIDATE_PAIRS_NAME in manifest.files:
        declared_hash = manifest.files[CANDIDATE_PAIRS_NAME]
        if not candidate_pairs_path.is_file():
            raise WorkflowError(f"packet 清单登记了 {CANDIDATE_PAIRS_NAME} 但文件缺失：拒绝导入")
        actual_hash = sha256_text(candidate_pairs_path.read_text(encoding="utf-8"))
        if actual_hash != declared_hash:
            raise WorkflowError(
                f"{CANDIDATE_PAIRS_NAME} 与 packet 登记哈希不一致：候选对身份清单可能被篡改，拒绝导入"
            )
        for index, row in enumerate(_read_jsonl(candidate_pairs_path), start=1):
            try:
                pair = CandidatePair.model_validate(row)
            except Exception as error:  # noqa: BLE001
                raise WorkflowError(f"packet {CANDIDATE_PAIRS_NAME} 第 {index} 行不受支持：{error}") from error
            unknown = sorted(
                s for s in (pair.left_sample_id, pair.right_sample_id) if s not in declared_sample_ids
            )
            if unknown:
                raise WorkflowError(
                    f"packet {CANDIDATE_PAIRS_NAME} 第 {index} 行引用了样本清单之外的 sample_id："
                    f"{unknown}，拒绝猜测"
                )
            pairs.append(pair)
        recomputed_pairs_fp = compute_pairs_fingerprint(pairs)
        if recomputed_pairs_fp != manifest.pairs_fingerprint:
            raise WorkflowError("pairs 指纹与 packet 清单不一致：拒绝在不确定的 packet 上导入")
    elif manifest.n_pairs:
        raise WorkflowError(f"packet 声明 {manifest.n_pairs} 条候选对但 {CANDIDATE_PAIRS_NAME} 未登记：拒绝导入")
    if len(pairs) != manifest.n_pairs:
        raise WorkflowError(
            f"packet 声明候选对数量 {manifest.n_pairs} 与实际 {len(pairs)} 不一致：拒绝导入"
        )
    return manifest, samples, pairs


def _identity_key(record: ExtractionAnnotationRecord | PairAnnotationRecord) -> tuple[str, tuple[str, str]]:
    """extraction 用 sample_id；pair 用无序端点对，不同任务永不冲突。"""
    if isinstance(record, PairAnnotationRecord):
        return ("pair", tuple(sorted((record.sample_id, record.right_sample_id))))
    return ("extraction", (record.sample_id,))


def _dedup_records(
    records: list[ExtractionAnnotationRecord | PairAnnotationRecord],
) -> tuple[
    list[ExtractionAnnotationRecord | PairAnnotationRecord],
    list[dict[str, object]],
    list[tuple[ExtractionAnnotationRecord | PairAnnotationRecord, dict[str, object]]],
]:
    """按身份键归并：完全相同 → 折叠到唯一副本；有差异 → 视为冲突，两方都隔离。

    返回 (unique_records, folded_summaries, conflicting_quarantine)。
    绝不"任选一条"作为代表，冲突记录连同原因进入隔离区。
    """
    groups: dict[tuple[str, tuple[str, str]], list[tuple[object, str]]] = {}
    order: list[tuple[str, tuple[str, str]]] = []
    for record in records:
        key = _identity_key(record)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((record, _dump_row(record)))
    unique: list[ExtractionAnnotationRecord | PairAnnotationRecord] = []
    folded: list[dict[str, object]] = []
    conflicts: list[tuple[ExtractionAnnotationRecord | PairAnnotationRecord, dict[str, object]]] = []
    for key in order:
        entries = groups[key]
        if len(entries) == 1:
            unique.append(entries[0][0])  # type: ignore[arg-type]
            continue
        variants = {dump for _, dump in entries}
        if len(variants) == 1:
            unique.append(entries[0][0])  # type: ignore[arg-type]
            folded.append({"key": [key[0], list(key[1])], "duplicate_rows": len(entries)})
        else:
            reason = {
                "key": [key[0], list(key[1])],
                "duplicate_rows": len(entries),
                "distinct_variants": len(variants),
                "note": "同一身份键的重复行内容不一致（label/reviewer_state/provenance/pair_relation 等）：拒绝猜测、不采信任何一边",
            }
            for record, _ in entries:
                conflicts.append((record, reason))  # type: ignore[arg-type]
    return unique, folded, conflicts


def import_worksheet(
    packet_dir: Path,
    out_dir: Path,
    extraction_path: Path | None = None,
    pairs_path: Path | None = None,
) -> dict[str, object]:
    """把人工填写后的 worksheet JSONL 校验/导入为既有标注契约记录，并输出 READY/NOT_READY 报告。

    本函数不写 evaluation/metrics.json、不做任何产品门禁判定；汇总完全来自既有
    `human_dataset_status`，切分只走既有 `group_split`/`validate_split`。
    """
    manifest, samples, declared_pairs = _load_packet(Path(packet_dir))
    by_id = {sample.sample_id: sample for sample in samples}
    declared_pair_keys = {tuple(sorted((p.left_sample_id, p.right_sample_id))) for p in declared_pairs}
    extraction_source = Path(extraction_path) if extraction_path else Path(packet_dir) / EXTRACTION_WORKSHEET_NAME
    pairs_source = Path(pairs_path) if pairs_path else Path(packet_dir) / PAIR_WORKSHEET_NAME

    resolved_out = _ensure_fresh_dir(Path(out_dir), "导入输出目录")
    rejections: list[dict[str, str]] = []
    valid: list[ExtractionAnnotationRecord | PairAnnotationRecord] = []

    def _load_rows(path: Path, kind: Literal["extraction", "pair"]) -> None:
        for line_no, row in enumerate(_read_jsonl(path), start=1):
            tag = f"{kind}#L{line_no}"
            try:
                record = (
                    ExtractionAnnotationRecord.model_validate(row)
                    if kind == "extraction"
                    else PairAnnotationRecord.model_validate(row)
                )
            except Exception as error:  # noqa: BLE001 - 契约拒绝逐行归因，不悄悄丢弃
                rejections.append({"id": tag, "stage": "contract", "reason": str(error)})
                continue
            reason = _workflow_check(record, by_id, kind)
            if reason:
                rejections.append({"id": tag, "stage": "workflow", "reason": reason})
                continue
            if kind == "pair":
                pair_key = tuple(sorted((record.sample_id, record.right_sample_id)))  # type: ignore[attr-defined]
                if pair_key not in declared_pair_keys:
                    rejections.append({
                        "id": tag,
                        "stage": "workflow",
                        "reason": (
                            f"pair 端点 {list(pair_key)} 未在 packet 候选对清单中申报：拒绝猜测、"
                            "工作流绝不自行推断 same_need"
                        ),
                    })
                    continue
            valid.append(record)

    _load_rows(extraction_source, "extraction")
    if pairs_path is not None and not pairs_source.exists():
        raise WorkflowError(f"显式 supplied 的 pair 工作表不存在：{pairs_source}（拒绝猜测）")
    if pairs_source.exists():
        _load_rows(pairs_source, "pair")

    unique_valid, duplicates_folded, duplicate_conflicts = _dedup_records(valid)

    conflicting_pairs: list[dict[str, object]] = [
        {
            "pair": item["key"][1],
            "relations": [],
            "note": item["note"],
        }
        for _record, item in duplicate_conflicts
        if item["key"][0] == "pair"
    ]

    quarantined: list[tuple[ExtractionAnnotationRecord | PairAnnotationRecord, dict[str, object]]] = [
        (record, dict(reason, stage="duplicate_conflict"))
        for record, reason in duplicate_conflicts
    ]
    product_valid: list[ExtractionAnnotationRecord | PairAnnotationRecord] = []
    non_actual_gold: list[str] = []
    synthetic_marked: list[str] = []
    for record in unique_valid:
        if record.provenance.source != "human":
            synthetic_marked.append(record.sample_id)
        if record.is_human_gold():
            involved = [record.sample_id]
            if isinstance(record, PairAnnotationRecord):
                involved.append(record.right_sample_id)
            bad = sorted(sid for sid in involved if by_id[sid].origin != "actual_source")
            if bad:
                non_actual_gold.append(f"{record.sample_id}:{'|'.join(bad)}")
                quarantined.append((
                    record,
                    {
                        "stage": "origin",
                        "reason": (
                            f"human gold 记录涉及非 actual_source 样本 {bad}："
                            "从产品规范文件隔离，原样保留在 sidecar 中，不重新标记真实评审者"
                        ),
                        "involved_samples": bad,
                    },
                ))
                continue
        product_valid.append(record)

    status = human_dataset_status(product_valid)
    split_summary: dict[str, object]
    if not product_valid:
        split_summary = {"validated": False, "reason": "没有任何有效记录，拒绝切分（不猜测）"}
    else:
        items: dict[str, SplitItem] = {}
        for record in product_valid:
            sample = by_id[record.sample_id]
            items[sample.sample_id] = SplitItem(
                sample_id=sample.sample_id, source_id=sample.source_id, family_id=sample.family_id
            )
        try:
            result = group_split(list(items.values()))
            validate_split(result, list(items.values()))
            split_summary = {
                "validated": True,
                "fingerprint": result.fingerprint,
                "dev": len(result.dev_sample_ids),
                "heldout": len(result.heldout_sample_ids),
            }
        except InsufficientGroupsError as error:
            split_summary = {"validated": False, "reason": f"分组不足，拒绝评估：{error}"}
        except ValueError as error:
            split_summary = {"validated": False, "reason": f"切分复核失败：{error}"}

    ready = (
        status.status == "MET"
        and split_summary.get("validated") is True
        and not conflicting_pairs
        and not rejections
        and not non_actual_gold
        and not duplicate_conflicts
        and not quarantined
    )
    report: dict[str, object] = {
        "version": WORKFLOW_VERSION,
        "packet_fingerprint": manifest.packet_fingerprint,
        "ready": "READY" if ready else "NOT_READY",
        "counts": {
            "valid_records": len(product_valid),
            "rejected_rows": len(rejections),
            "extraction_records": sum(isinstance(r, ExtractionAnnotationRecord) for r in product_valid),
            "pair_records": sum(isinstance(r, PairAnnotationRecord) for r in product_valid),
            "non_human_provenance_records": len(synthetic_marked),
            "duplicates_folded": len(duplicates_folded),
            "duplicate_conflict_rows": len(duplicate_conflicts),
            "quarantined_records": len(quarantined),
        },
        "human_dataset_status": status.model_dump(mode="json"),
        "split": split_summary,
        "rejections": rejections,
        "conflicting_pairs": conflicting_pairs,
        "duplicates_folded": duplicates_folded,
        "duplicate_conflicts": [reason for _record, reason in duplicate_conflicts],
        "quarantine": [
            {
                "sample_id": record.sample_id,
                **({"right_sample_id": record.right_sample_id} if isinstance(record, PairAnnotationRecord) else {}),
                **reason,
            }
            for record, reason in quarantined
        ],
        "synthetic_marked_sample_ids": sorted(set(synthetic_marked)),
        "human_gold_on_non_actual_samples": sorted(set(non_actual_gold)),
        "notes": [
            "本报告不构成产品/质量门禁 PASS；metrics.json 与既有 gold 规则未被改动",
            "human_reviewed 计数只来自既有 human_dataset_status 且仅在产品集合上计算",
            "本工作流不签名、不代填标签；非 actual_source 的 human gold 与冲突重复都隔离到 sidecar，不进入产品规范文件",
        ],
    }

    annotations_text = ("\n".join(_dump_row(record) for record in product_valid) + "\n") if product_valid else ""
    (resolved_out / VALIDATED_ANNOTATIONS_NAME).write_text(annotations_text, encoding="utf-8", newline="\n")
    if quarantined:
        quarantine_lines: list[str] = []
        for record, reason in quarantined:
            payload = record.model_dump(mode="json")
            payload["_quarantine"] = reason
            quarantine_lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        (resolved_out / QUARANTINED_ANNOTATIONS_NAME).write_text(
            "\n".join(quarantine_lines) + "\n", encoding="utf-8", newline="\n"
        )
    else:
        (resolved_out / QUARANTINED_ANNOTATIONS_NAME).write_text("", encoding="utf-8", newline="\n")
    report_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (resolved_out / IMPORT_REPORT_NAME).write_text(report_text, encoding="utf-8", newline="\n")
    return report


def _workflow_check(
    record: ExtractionAnnotationRecord | PairAnnotationRecord,
    by_id: dict[str, PacketSample],
    kind: str,
) -> str | None:
    """工作流层拒绝条件：归属漂移、占位符升格、无意义标签。返回 None 才放行。"""
    sample = by_id.get(record.sample_id)
    if sample is None:
        return f"sample_id {record.sample_id!r} 不在 packet 冻结样本内"
    if record.content_sha256 != sample.content_sha256 or record.text != sample.text:
        return f"样本 {record.sample_id} 的 text/hash 与 packet 冻结原文不一致：标注归属不可信"
    if record.source_id != sample.source_id or record.family_id != sample.family_id:
        return f"样本 {record.sample_id} 的 source/family 元数据与 packet 不一致"
    if isinstance(record, PairAnnotationRecord) and record.right_sample_id not in by_id:
        return f"对端 {record.right_sample_id!r} 不在 packet 冻结样本内"
    if record.reviewer_state != "unlabeled":
        placeholders = [
            value
            for value in (
                record.provenance.producer_id,
                record.provenance.dataset_id,
                record.reviewer_id,
            )
            if value in PLACEHOLDER_SET
        ]
        if record.reviewer_state in ("draft", "reviewed", "adjudicated") and placeholders:
            return (
                f"样本 {record.sample_id}: reviewer_state={record.reviewer_state} 仍携带占位符身份字段"
                f"（标注人/数据集未显式声明为真人真数据集）：拒绝静默升格"
            )
        if (
            record.reviewer_state in ("reviewed", "adjudicated")
            and isinstance(record, ExtractionAnnotationRecord)
            and not record.has_any_known_label()
        ):
            return (
                f"样本 {record.sample_id}: {record.reviewer_state} 但六个抽取标签全为 unknown："
                "已复核行必须携带至少一个明确任务标签，未知只能保持 draft/unlabeled"
            )
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="annotation_workflow",
        description="P2 离线人工标注 packet 生成/导入工作流（不联网、不生成人工金标）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="从显式样本清单（+候选对清单）生成 packet 目录")
    p_prepare.add_argument("--manifest", required=True, help="样本清单 JSONL 路径")
    p_prepare.add_argument("--pairs", help="候选对清单 JSONL 路径（可选；不推断 same_need）")
    p_prepare.add_argument("--out", required=True, help="packet 输出目录（必须为空或不存在）")
    p_prepare.add_argument("--packet-id", help="packet 标识（默认由指纹派生）")

    p_import = sub.add_parser("import", help="校验/导入人工填写的工作表并输出 READY 报告")
    p_import.add_argument("--packet", required=True, help="prepare 产出的 packet 目录")
    p_import.add_argument("--extraction", help="填写后的 extraction_worksheet.jsonl（默认取 packet 内文件）")
    p_import.add_argument("--pairs", help="填写后的 pair_worksheet.jsonl（默认取 packet 内文件）")
    p_import.add_argument("--out", required=True, help="导入输出目录（必须为空或不存在）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_packet(
                Path(args.manifest), Path(args.out),
                Path(args.pairs) if args.pairs else None,
                packet_id=args.packet_id,
            )
            print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "import":
            report = import_worksheet(
                Path(args.packet), Path(args.out),
                Path(args.extraction) if args.extraction else None,
                Path(args.pairs) if args.pairs else None,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:  # argparse 已保证不可达
            raise WorkflowError(f"未知子命令 {args.command}")
    except WorkflowError as error:
        print(f"工作流拒绝：{error}", file=sys.stderr)
        return 2
    return 0


__all__ = [
    "WORKFLOW_VERSION",
    "PACKET_MANIFEST_NAME",
    "SAMPLES_NAME",
    "EXTRACTION_WORKSHEET_NAME",
    "EXTRACTION_WORKSHEET_CSV",
    "PAIR_WORKSHEET_NAME",
    "PAIR_WORKSHEET_CSV",
    "CANDIDATE_PAIRS_NAME",
    "VALIDATED_ANNOTATIONS_NAME",
    "QUARANTINED_ANNOTATIONS_NAME",
    "IMPORT_REPORT_NAME",
    "PLACEHOLDER_PRODUCER",
    "PLACEHOLDER_DATASET_EXTRACTION",
    "PLACEHOLDER_DATASET_PAIR",
    "SampleOrigin",
    "PacketSample",
    "CandidatePair",
    "PacketManifest",
    "WorkflowError",
    "PacketExistsError",
    "PathRestrictedError",
    "load_sample_manifest",
    "load_candidate_pairs",
    "compute_samples_fingerprint",
    "compute_pairs_fingerprint",
    "guard_csv_cell",
    "prepare_packet",
    "import_worksheet",
    "build_parser",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
