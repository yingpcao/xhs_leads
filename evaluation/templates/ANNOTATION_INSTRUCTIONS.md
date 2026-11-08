# P2 人工标注说明（P2-01）

状态：200–300 条人工标注**尚未提供（NOT_MET）**。本目录下的两个 `.jsonl` 模板是**空的**，
必须由真实标注人逐条填写；任何脚本或模型都不得批量代签充当人工金标。

## 文件

| 文件 | 用途 |
|---|---|
| `evaluation/templates/annotation_extraction_template.jsonl` | 单样本抽取标签（6 个维度） |
| `evaluation/templates/annotation_pair_template.jsonl` | 语义对同义关系标签 |
| `evaluation/metrics.json` | 指标骨架：全部 unknown/null，状态 NOT_EVALUATED |

抽取标签与语义关系**分开标注**：同一样本可以同时有抽取标签，而 pair 文件只回答
"两条是否同一需求"。

## 记录格式（schema_version=p2-annotations-v1）

每行一个 JSON 对象，字段与 `src/xhs_discovery/evaluation/annotations.py` 的
`ExtractionAnnotationRecord` / `PairAnnotationRecord` 一致（未知字段会被拒绝）：

```json
{"schema_version": "p2-annotations-v1", "sample_id": "s001", "source_id": "note:xxx",
 "family_id": "fam-01", "content_sha256": "<utf8(text) 的 sha256>", "text": "<原文>",
 "provenance": {"source": "human", "dataset_id": "p2-human-extraction-YYYYMMDD",
   "producer_id": "<标注人>", "produced_at": null, "synthetic": false},
 "reviewer_state": "unlabeled", "reviewer_id": null, "label": null}
```

抽取 `label` 恰好包含六个键，值域 `true/false/null`（null=unknown，看不准就写 null）：
`explicit_problem, implicit_problem, advertisement, negation, region, year`。
pair 记录另有 `right_sample_id` 与 `pair_relation ∈ same_need|different_need|unknown`。

## 标注流程规则

1. `reviewer_state`：`unlabeled`（未开始）→ `draft`（初标）→ `reviewed`（复核完成，
   必须有 `reviewer_id`）→ `adjudicated`（分歧裁决完成）。
2. 只有 `source=human`、`reviewed/adjudicated` **且携带明确任务标签** 的记录才计入人工金标：
   抽取记录至少有一个维度为 true/false（全部 unknown 的 reviewed 记录是合法的在途标注，
   会被排除并给出原因，但不能支撑门禁 PASS）；pair 记录必须给出 same_need/different_need。
   合成 fixture / 模型生成标签会被 `human_dataset_status` 拒绝计入并留下原因记录。
3. 合并（merge）质量只能由 `PairAnnotationRecord` 金标祝福：评估的每个语义对都必须
   命中两端样本齐备的人工对金标；抽取金标与预测自报的 `human_relation` 都不能替代。
4. 覆盖要求（DEVELOPMENT_PLAN.md:207）：明确/隐性问题、广告、近义、跨主题、否定、
   地域、年份长尾都要有样本；总量 200–300 条。
5. `content_sha256` 必须等于 `text` 的 UTF-8 sha256，防止标注漂移后错配。
6. 标注完成后：用 `group_split` 按来源/家族切 dev/heldout，再运行评估器；门禁会用记录的
   实际样本/来源/家族元数据整体重建并复核 SplitResult（同家族跨分区、篡改指纹一律拒绝）。
   在此之前 `evaluation/metrics.json` 保持 NOT_EVALUATED，不得预填数值。

## 复现示例

```powershell
python -m pytest tests/test_p2_annotations.py tests/test_p2_splits.py tests/test_p2_metrics.py tests/test_p2_gold_integrity.py -q
```

门禁与来源校验规则由上面这组测试覆盖。重建模板/合成 fixture 的一次性脚本与门禁探针属开发过程记录，
只在维护者本机的 `docs/verification/repros/` 下保留，未随仓库发布。
