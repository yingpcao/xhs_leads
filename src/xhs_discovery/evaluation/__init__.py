"""P2 离线评估基础设施（工程骨架，不含任何人类标注结论）。

本包只实现可离线、可测试的契约与评估器：
- annotations：标注输入契约、空模板与合成 fixture（200–300 条人工标注仍未提供，NOT_MET）；
- splits：按来源/内容家族分组的确定性 dev/heldout 切分；
- metrics：指标契约与评估器（真实数据到位前一切指标保持 unknown）；
- resources：只读硬件/资源检视（不加载、不下载任何模型）。

边界：本包不执行合并、不采集数据、不访问网络；合成数据永远不能通过产品门禁。
"""

from xhs_discovery.evaluation.annotations import (
    ANNOTATION_SCHEMA_VERSION,
    EXTRACTION_LABEL_FIELDS,
    REQUIRED_HUMAN_DATASET_MAX,
    REQUIRED_HUMAN_DATASET_MIN,
    ExtractionAnnotationRecord,
    PairAnnotationRecord,
    AnnotationProvenance,
    HumanDatasetStatus,
    empty_extraction_records,
    empty_pair_records,
    human_dataset_status,
    load_annotation_records,
    synthetic_test_fixtures,
)
from xhs_discovery.evaluation.splits import (
    SPLIT_VERSION,
    InsufficientGroupsError,
    SplitItem,
    SplitResult,
    group_split,
    validate_split,
)
from xhs_discovery.evaluation.metrics import (
    ERROR_MERGE_RATE_MAX,
    GOLD_BINDING_VERSION,
    METRICS_REPORT_VERSION,
    MERGE_PRECISION_MIN,
    MetricCount,
    EvaluationReport,
    MergePrediction,
    ExtractionPrediction,
    compute_gold_fingerprint,
    evaluate_merge,
    evaluate_extraction,
    compare_arms,
    evaluate_gate,
    metrics_skeleton,
)

__all__ = [name for name in dir() if not name.startswith("_")]
