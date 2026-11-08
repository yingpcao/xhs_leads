"""标注输入契约：抽取标签与语义对标签分开，来源与复核状态强制显式。

设计约束（DEVELOPMENT_PLAN.md P2、PROJECT_CHECKLIST P2-01）：
- 200–300 条人工标注是门槛；当前尚未提供，human_dataset_status 恒为 NOT_MET，
  本模块绝不自动批量生成示例来凑数；
- 合成 fixture / 模型生成标签只用于单元测试，provenance.source 如实标注，
  不存在任何把它们转成 human gold 的路径；
- unknown/未标注合法：label 可为 None 且 labeled=False，但二者必须一致；
  reviewed 却所有相关标签仍为 unknown 的记录不构成金标（在途标注，排除有原因）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from xhs_discovery.models.common import StrictModel, sha256_text

ANNOTATION_SCHEMA_VERSION = "p2-annotations-v1"

# 人工数据集门槛（DEVELOPMENT_PLAN.md:207）。未达到前一切评估口径为 NOT_MET。
REQUIRED_HUMAN_DATASET_MIN = 200
REQUIRED_HUMAN_DATASET_MAX = 300

LabelSource = Literal["human", "synthetic_fixture", "model_generated"]
ReviewerState = Literal["unlabeled", "draft", "reviewed", "adjudicated"]

# 抽取标签的固定字段集：值域为 True/False/None(unknown)。
EXTRACTION_LABEL_FIELDS = (
    "explicit_problem",
    "implicit_problem",
    "advertisement",
    "negation",
    "region",
    "year",
)


def _require_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间戳必须带时区，未知时间不能伪造")
    return value


class AnnotationProvenance(StrictModel):
    """标注来源与复核身份。合成或模型生成来源不得冒充 human。"""

    source: LabelSource
    dataset_id: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    produced_at: datetime | None = None
    synthetic: bool = False

    @field_validator("produced_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_tz(value)

    @model_validator(mode="after")
    def _check(self) -> "AnnotationProvenance":
        if self.source == "human":
            if self.synthetic:
                raise ValueError("human 标注不能标记 synthetic=True")
            if self.producer_id.startswith(("model:", "synthetic:")):
                raise ValueError("producer_id 表明这是模型/合成产物，不能声明 source=human")
        else:
            # 合成与模型生成来源必须显式 synthetic=True，方便下游一键隔离。
            if not self.synthetic:
                raise ValueError(f"source={self.source} 必须显式 synthetic=True")
        return self


class _RecordBase(StrictModel):
    schema_version: str
    sample_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str
    provenance: AnnotationProvenance
    reviewer_state: ReviewerState
    reviewer_id: str | None = None
    label: dict[str, bool | None] | None = None

    @model_validator(mode="after")
    def _check(self) -> "_RecordBase":
        if self.schema_version != ANNOTATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version 必须是 {ANNOTATION_SCHEMA_VERSION}")
        if sha256_text(self.text) != self.content_sha256:
            raise ValueError("content_sha256 与 text 不一致，标注归属不可信")
        if self.reviewer_state in ("reviewed", "adjudicated") and not self.reviewer_id:
            raise ValueError(f"reviewer_state={self.reviewer_state} 必须有 reviewer_id")
        return self


class ExtractionAnnotationRecord(_RecordBase):
    """单样本抽取标签：六个维度各为 true/false/unknown。"""

    def has_any_known_label(self) -> bool:
        """至少有一个维度给出明确 true/false 才算携带金标信号。

        全 unknown 的 label 是合法的在途标注（work in progress），不构成金标。
        """
        return self.label is not None and any(value is not None for value in self.label.values())

    def is_human_gold(self) -> bool:
        return (
            self.provenance.source == "human"
            and self.reviewer_state in ("reviewed", "adjudicated")
            and self.has_any_known_label()
        )

    @model_validator(mode="after")
    def _check_label(self) -> "ExtractionAnnotationRecord":
        if self.reviewer_state == "unlabeled":
            if self.label is not None:
                raise ValueError("unlabeled 记录不得携带 label")
        elif self.label is None:
            raise ValueError(f"reviewer_state={self.reviewer_state} 必须携带 label")
        if self.label is not None and set(self.label) != set(EXTRACTION_LABEL_FIELDS):
            raise ValueError("抽取 label 必须恰好覆盖固定字段集，缺项须显式写 null(unknown)")
        return self


class PairAnnotationRecord(_RecordBase):
    """语义对标签：same_need/different_need/unknown，独立于抽取标签。"""

    right_sample_id: str = Field(min_length=1)
    pair_relation: Literal["same_need", "different_need", "unknown"]

    @model_validator(mode="after")
    def _check_pair(self) -> "PairAnnotationRecord":
        if self.right_sample_id == self.sample_id:
            raise ValueError("语义对的两个 sample_id 不能相同")
        if self.reviewer_state == "unlabeled" and self.pair_relation != "unknown":
            raise ValueError("unlabeled 对的 pair_relation 只能是 unknown")
        if self.reviewer_state in ("reviewed", "adjudicated") and self.pair_relation == "unknown":
            raise ValueError("已复核的对必须给出明确 relation，unknown 只属于未标注/待裁决")
        return self

    def is_human_gold(self) -> bool:
        return self.provenance.source == "human" and self.reviewer_state in (
            "reviewed",
            "adjudicated",
        )


class HumanDatasetStatus(StrictModel):
    """人工数据集检查结论。不可为了通过而伪造记录。"""

    human_reviewed_count: int
    required_min: int
    required_max: int
    status: Literal["NOT_MET", "MET"]
    reasons: list[str] = Field(default_factory=list)


def human_dataset_status(records: list[_RecordBase]) -> HumanDatasetStatus:
    """只统计 human+已复核+携带明确任务标签 的记录；合成/模型来源一律不计入。

    reviewed 但所有相关标签仍为 unknown 的记录是合法的在途标注：显式排除并给出
    原因，但永远不能作为人工金标支撑门禁 PASS。
    """
    reasons: list[str] = []
    bad_sources = [
        r for r in records
        if r.provenance.source in ("synthetic_fixture", "model_generated")
        and r.reviewer_state in ("reviewed", "adjudicated")
    ]
    if bad_sources:
        reasons.append(
            f"{len(bad_sources)} 条合成/模型来源记录被拒绝计入人工集（不得自动转成 human gold）"
        )
    human_reviewed = [
        r for r in records
        if r.provenance.source == "human" and r.reviewer_state in ("reviewed", "adjudicated")
    ]
    unknown_labels = [
        r for r in human_reviewed
        if isinstance(r, ExtractionAnnotationRecord) and not r.has_any_known_label()
    ]
    if unknown_labels:
        reasons.append(
            f"{len(unknown_labels)} 条 human 已复核记录所有抽取标签仍为 unknown：属合法在途标注，"
            "已带原因排除，不得充当人工金标"
        )
    human_gold = [r for r in records if r.is_human_gold()]
    count = len(human_gold)
    if count < REQUIRED_HUMAN_DATASET_MIN:
        reasons.append(
            f"人工复核记录 {count} 条 < 最低要求 {REQUIRED_HUMAN_DATASET_MIN} 条：NOT_MET，"
            "等待真实人工标注，不允许自动生成凑数"
        )
        status = "NOT_MET"
    elif count > REQUIRED_HUMAN_DATASET_MAX:
        reasons.append(
            f"人工复核记录 {count} 条 > 计划上限 {REQUIRED_HUMAN_DATASET_MAX} 条：需人工确认口径"
        )
        status = "NOT_MET"
    else:
        status = "MET"
    return HumanDatasetStatus(
        human_reviewed_count=count,
        required_min=REQUIRED_HUMAN_DATASET_MIN,
        required_max=REQUIRED_HUMAN_DATASET_MAX,
        status=status,
        reasons=reasons,
    )


def empty_extraction_records(n_hint: int = 0) -> list[dict[str, object]]:
    """生成空的人工标注模板行（不含任何标签、来源为 human 待填）。

    n_hint 只是模板行数提示，仅供文档演示格式；不会批量生成 200+ 示例。
    """
    template = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "sample_id": "",
        "source_id": "",
        "family_id": "",
        "content_sha256": "0" * 64,
        "text": "",
        "provenance": {
            "source": "human",
            "dataset_id": "p2-human-extraction-YYYYMMDD",
            "producer_id": "<标注人>",
            "produced_at": None,
            "synthetic": False,
        },
        "reviewer_state": "unlabeled",
        "reviewer_id": None,
        "label": None,
    }
    return [dict(template) for _ in range(n_hint)]


def empty_pair_records(n_hint: int = 0) -> list[dict[str, object]]:
    template = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "sample_id": "",
        "right_sample_id": "",
        "source_id": "",
        "family_id": "",
        "content_sha256": "0" * 64,
        "text": "",
        "provenance": {
            "source": "human",
            "dataset_id": "p2-human-pair-YYYYMMDD",
            "producer_id": "<标注人>",
            "produced_at": None,
            "synthetic": False,
        },
        "reviewer_state": "unlabeled",
        "reviewer_id": None,
        "label": None,
        "pair_relation": "unknown",
    }
    return [dict(template) for _ in range(n_hint)]


def synthetic_test_fixtures() -> tuple[list[ExtractionAnnotationRecord], list[PairAnnotationRecord]]:
    """单元测试专用合成 fixture：来源显式 synthetic_fixture，绝不当作人工金标。

    这些是模拟人工契约取值的合成 UNIT 记录（显式标签、成对端点元数据一致），
    仅用于验证接口契约；它们与任何真实收集的人工标注无关，导出示例也带
    synthetic_fixture 来源标记。时间戳固定为 Unix 纪元并用 producer_id 声明
    合成身份，不伪造真人或真实日期。
    """
    provenance = {
        "source": "synthetic_fixture",
        "dataset_id": "p2-synthetic-unit-fixtures",
        "producer_id": "synthetic:p2-unit-fixture-generator",
        "produced_at": "1970-01-01T00:00:00+00:00",
        "synthetic": True,
    }

    def extraction(sample_id: str, text: str, labels: dict[str, bool | None]) -> ExtractionAnnotationRecord:
        return ExtractionAnnotationRecord(
            schema_version=ANNOTATION_SCHEMA_VERSION,
            sample_id=sample_id,
            source_id=f"src-{sample_id}",
            family_id=f"fam-{sample_id[-1]}",
            content_sha256=sha256_text(text),
            text=text,
            provenance=AnnotationProvenance.model_validate(provenance),
            reviewer_state="reviewed",
            reviewer_id="synthetic-reviewer",
            label=labels,
        )

    full_labels = {name: None for name in EXTRACTION_LABEL_FIELDS}
    extraction_records = [
        extraction("s1", "上海中考数学怎么提高", {**full_labels, "explicit_problem": True, "region": True}),
        extraction("s2", "上海中考数学怎么提高！", {**full_labels, "explicit_problem": True, "region": True}),
        extraction("s3", "北京考研英语资料推荐", {**full_labels, "implicit_problem": True, "advertisement": False}),
        extraction("s4", "不是广告｜广州雅思备考规划", {**full_labels, "negation": True, "region": True}),
        extraction("s5", "2026年杭州幼升小政策", {**full_labels, "year": True, "region": True}),
    ]
    sample_meta = {r.sample_id: r for r in extraction_records}

    def pair(left: str, right: str, relation: str) -> PairAnnotationRecord:
        anchor = sample_meta[left]
        return PairAnnotationRecord(
            schema_version=ANNOTATION_SCHEMA_VERSION,
            sample_id=left,
            right_sample_id=right,
            source_id=anchor.source_id,
            family_id=anchor.family_id,
            content_sha256=anchor.content_sha256,
            text=anchor.text,
            provenance=AnnotationProvenance.model_validate(provenance),
            reviewer_state="reviewed",
            reviewer_id="synthetic-reviewer",
            label=None,
            pair_relation=relation,
        )

    pair_records = [
        pair("s1", "s2", "same_need"),
        pair("s3", "s4", "different_need"),
    ]
    return extraction_records, pair_records


def load_annotation_records(path: Path) -> list[_RecordBase]:
    """加载 JSONL 标注文件并逐行校验；未知 schema 版本立即失败。"""
    records: list[_RecordBase] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            version = payload.get("schema_version")
            if version != ANNOTATION_SCHEMA_VERSION:
                raise ValueError(f"第 {line_no} 行 schema_version={version!r} 不受支持")
            if "pair_relation" in payload:
                records.append(PairAnnotationRecord.model_validate(payload))
            else:
                records.append(ExtractionAnnotationRecord.model_validate(payload))
    return records


__all__ = [
    "ANNOTATION_SCHEMA_VERSION",
    "REQUIRED_HUMAN_DATASET_MIN",
    "REQUIRED_HUMAN_DATASET_MAX",
    "EXTRACTION_LABEL_FIELDS",
    "LabelSource",
    "ReviewerState",
    "AnnotationProvenance",
    "ExtractionAnnotationRecord",
    "PairAnnotationRecord",
    "HumanDatasetStatus",
    "human_dataset_status",
    "empty_extraction_records",
    "empty_pair_records",
    "synthetic_test_fixtures",
    "load_annotation_records",
]
