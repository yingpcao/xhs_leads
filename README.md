# 小红书长尾需求发现引擎

给一个种子词（例如"上海中考"），它顺着小红书搜索框的联想词一层层扩散，把笔记标题里的真实提问
提取出来、保守地归并成需求簇，最后交给你一份需求清单、两张图谱（关键词图与需求图）和一份附带
随机种子、预算账本与局限说明的运行报告。

它要解决的问题很具体：**"用户到底想要什么"这件事，能不能从平台真实提问里挖出来，并且每一条结论
都能回溯到原始标题。** 每条需求都指向至少一条可定位的证据（哪条笔记、标题里的第几个字符），
程序不允许出现"没有出处的结论"。

---

## 快速开始

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m xhs_discovery doctor        # 自检解释器、依赖与核心契约
```

打开本地界面（推荐从这里开始）：

```
.venv/Scripts/python.exe -m xhs_discovery ui
```

浏览器里侧栏有六个页面：**创建任务 / 运行看板 / 关键词发现图 / 需求图 · 证据详情 / 导出 / 人工标注**。
「创建任务」默认使用内置的合成数据集，选好种子词点创建就能跑完整流程，全程不联网。

不想开界面也可以用命令行一次跑完探索→分析→导出：

```
.venv/Scripts/python.exe -m xhs_discovery run --mode fixture --seed 上海中考 --config config/offline-demo.yaml --run demo-run
.venv/Scripts/python.exe -m xhs_discovery export --run demo-run --config config/offline-demo.yaml
```

> 演示配置 `offline-demo.yaml` 的探索预算故意小于收敛判据，所以任务会以「暂停」结束而不是「收敛」。
> 这是设计如此：停止判据不为了让演示好看而放宽。

环境要求：Python 3.11 及以上（在 Windows 10 x64 + CPython 3.15.0 的干净临时虚拟环境实测通过）。
默认安装不含重型模型依赖：语义模型与 LLM 都是可选安装组，且默认关闭。

---

## 你会拿到什么

一次分析批次固定产出五类文件，位于 `<数据根>/runs/<run-id>/exports/<分析版本>/`：

| 文件 | 你可以用它做什么 |
|---|---|
| `needs.csv` | 需求清单：每条需求的评分构成、证据状态与证据数量 |
| `evidence.csv` | 每条证据的原文定位（笔记 ID、字段、字符偏移、是否合成数据） |
| `keywords.csv` | 关键词发现图：每个推荐词从哪个父词扩散来、第几层、评分构成 |
| `graph.graphml` | 两张图合并导出，可直接用 Gephi 等工具打开 |
| `report.json` | 运行配置、随机种子、代码版本、预算账本、异常与统计，内嵌前四件产物的哈希 |

导出只读取已完成批次并做反读复核（磁盘内容、事件记录、报告哈希三者一致才通过），不会用后来的数据
冒充旧批次，旧批次历史全部保留。

界面里的「运行看板」按任务模式区分来历：合成批次写明是合成值，真实批次写明是本机工具从平台返回的值
（界面不打开链接、不代做登录）。

仓库里带着两份真实跑出来的交付案例，可以直接看结论长什么样：

- [`deliverables/live-zhongkao-report/上海中考探究报告-需求发现报告.md`](deliverables/live-zhongkao-report/上海中考探究报告-需求发现报告.md)
- [`deliverables/live-zongping-report/上海中考综评探究报告-需求发现报告.md`](deliverables/live-zongping-report/上海中考综评探究报告-需求发现报告.md)

> 报告「产物」一节指向 `data_live/` 的链接只在本机有效：运行数据是可重跑的过程产物，不随仓库发布。

---

## 用你自己的真实数据（live 模式）

真实模式调用**本机已经装好、已经人工登录**的 `xhs-search` 命令行工具，只取搜索卡片上的标题。
程序自己不登录、不接触账号凭据、不绕过验证码。

前置条件（全部由你完成，程序不代做）：

1. 本机已安装 node 与 `xhs-search-cli`（该工具的调用规范、退出码与 profile 约定见
   [`小红书搜索关键词反查工具_USAGE.md`](小红书搜索关键词反查工具_USAGE.md)）；
2. 已在某个 profile 上人工登录，并跑通 `xhs-search doctor`；
3. 把配置里的 `node_path` / `cli_path` / `lock_dir` 改成本机实测的绝对路径
   （仓库里的 `config/live-*.yaml` 填的是占位路径，直接跑会被配置层拒绝）。

```
.venv/Scripts/python.exe -m xhs_discovery config-check --config config/live-smoke.yaml   # 先验配置
.venv/Scripts/python.exe -m xhs_discovery run --mode live --seed 上海中考 --config config/live-smoke.yaml --run live-smoke
.venv/Scripts/python.exe -m xhs_discovery export --run live-smoke --config config/live-smoke.yaml
```

真实模式的边界（改配置不会让这些变成"已支持"）：

- **只有标题证据**：正文抓取、作者公开笔记列表属未验证能力，相关动作明确返回不支持，不消耗额度，
  也不会拼造正文。
- 同一个登录 profile 同时只能跑一个采集任务；互斥与节流靠项目专用的锁目录实现，第二个任务会排队
  或按"profile 忙"暂停。
- 采集口径会固化在来源快照里（配置、词典、意图规则、工具路径与摘要）。之后工具被升级或改动，恢复时
  会明确拒绝，不会悄悄换一份工具接着采。
- 认证、验证码、页面结构一类故障**不重试**，任务停在 paused 等你处理；只有网络类错误按有限次数重试，
  且每次重试照常计入预算。

---

## 控制花费：预算、暂停、恢复、追加

外部调用要花时间和额度，所以额度是显式的：

```
.venv/Scripts/python.exe -m xhs_discovery pause  --run demo-run --config config/offline-demo.yaml
.venv/Scripts/python.exe -m xhs_discovery extend --run demo-run --config config/offline-demo.yaml --suggest-budget 4
.venv/Scripts/python.exe -m xhs_discovery resume --run demo-run --config config/offline-demo.yaml
```

`pause` 只写暂停请求，worker 在安全点停下；`extend` 追加的额度只增不减，绝不重置已经花掉的量，每次
变更都记进预算账本；`resume` 沿用同一份账本和队列继续跑，已有活跃 worker 时直接拒绝，不会跑出第二个
worker。

---

## 报错怎么读

| 退出码 | 发生了什么 | 你可以做什么 |
|---|---|---|
| `0` | 正常完成 | — |
| `2` | 参数不合法（含非法任务名） | 按提示改名，程序不会替你"清洗"成别的值 |
| `65` | 配置或来源口径不一致 | 补齐 live 配置，或确认采集工具未升级改动 |
| `66` | 任务 / 批次 / 数据根找不到 | 检查 `--config` 与 `--run` 是否指向同一数据根 |
| `67` | 状态冲突 | 已有 worker 在跑，或该任务已终结 |
| `70` | 能力未接入 | 模型开关未打开、正文抓取未支持、依赖未安装 |

核心原则是：**缺能力一律明确拒绝，不假装可用，也不静默降级。**

---

## 结果的可信度边界

用这份清单之前，有几点必须清楚，它们决定了结论该怎么解读：

- 合成数据集（`fixture`）的笔记、联想词与链接全部是构造的，标记 `synthetic=True`，链接只是格式合法的
  占位地址。它用来验证流程与支撑测试，**不能当真实采集结果使用**。
- 真实模式只有标题证据：卡片上的点赞文本是"显示出来的点赞数原文"，**不是搜索量**，不进任何导出列；
  发布日期与作者经常缺失，按缺失保留。「没采到」不等于「没有需求」。
- 评分只是在已采到的标题样本内的抽样口径，不等同全量需求强度。
- 分析是规则基线（分词、意图识别、保守归并、评分分解），阈值属于待实验配置；样本不足时报告只给原始
  计数与逐分量分解，不硬造分位数结论。
- LLM 与语义模型默认关闭且必须显式为 `false`，打开会被拒绝。配置文件里只写环境变量名，不写任何密钥。

---

## 想评估质量：人工标注

侧栏「人工标注」可逐条阅读原文并填写六类抽取标签或需求对关系，独立于运行数据库，不联网、不调模型。
保存写入原 JSONL 工作表，旧版本自动留在包内 `.annotation-history/`；若别的窗口改过同一份工作表，保存
会拒绝覆盖而不是静默顶掉。填一个新的「校验结果目录」并点「校验并导出标注」，可得规范标注、隔离记录与
导入报告。

标注规范见 [`evaluation/templates/ANNOTATION_INSTRUCTIONS.md`](evaluation/templates/ANNOTATION_INSTRUCTIONS.md)。
体验用演示包（明确标记测试用途，不可当真实人标）：

```
.venv/Scripts/python.exe -m xhs_discovery.evaluation.annotation_workflow prepare --help
```

在页面创建表单里使用 `tests/fixtures/p2/workflow_demo/samples_manifest.jsonl` 与
`tests/fixtures/p2/workflow_demo/candidate_pairs.jsonl`，输出到一个新的空目录（例如 `build/annotation-demo`）。

---

## 配置速查

`run.mode` 决定数据来源：`fixture`（合成、离线）或 `live`（本机 `xhs-search` CLI，仅标题）。
随仓库发布的配置默认都是 `fixture`，真实模式必须显式选择并配齐路径。

| 配置 | 用途 |
|---|---|
| `config/default.yaml` | 离线全量运行，阈值即设计文档默认值 |
| `config/offline-demo.yaml` | 演示与冒烟：预算小，但停止判据不放宽 |
| `config/live-smoke.yaml` | 真实标题版的小额度样例，独立数据根 `data_live/` |
| `config/live-zhongkao-report.yaml`、`config/live-zongping-report.yaml` | 仓库内两份交付案例对应的采集口径 |
| `config/intent_rules.yaml`、`config/domain_terms.txt` | 规则式 NLP 的意图模式与领域词 |

未知配置键会被拒绝，非法值报错带字段路径。`config-check` 会打印配置哈希，方便确认两次跑的是同一套口径。

---

## 数据放在哪里、怎么清理

运行数据全部位于配置里的 `run.data_dir`（默认 `data/`，真实模式样例用 `data_live/`），两者都不进版本库：

```
<数据根>/xhs_discovery.db              # 事实源：任务、账本、证据、批次
<数据根>/runs/<run-id>/operations/…    # 逐次尝试的原始响应快照
<数据根>/runs/<run-id>/exports/…       # 五类产物
```

保留期由 `retention` 配置声明（默认 90 天，演示配置收紧为 30 天），但**自动到期清理尚未实现**，删除属
运维动作。要重置环境，最省事的是换一个空的 `data_dir`。请不要单独删除某个活跃任务的 `operations/` 或
导出文件，否则恢复与反读校验会失败。

---

## 目录结构

```
src/xhs_discovery/     核心实现（discovery 探索 / analysis 分析 / storage 存储 / sources 数据来源 / ui 界面）
tests/                 测试与合成数据集
config/                运行配置
docs/                  设计文档（数据模型契约、模型选型）
evaluation/            标注模板、指标与资源预算
deliverables/          真实跑出来的交付案例（报告 + 需求清单），随仓库发布
data/  data_live/      运行数据，不发布
```

---

## 给开发者

测试全部离线：不访问平台、不需要登录、不下载模型权重；真实模式的代码路径由项目自己起的**本机假 CLI
子进程**驱动，走的是与真实工具同一套参数、信封、落盘与账本逻辑。

```
.venv/Scripts/python.exe -m pytest -o addopts="" -q                          # 全套（当前 1266 项通过）
.venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_cli_e2e.py    # 真实子进程端到端
.venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_live_cli_pipeline.py   # 真实模式接线
```

进一步阅读：设计与计划见 [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)，范围约定见
[`PROJECT_SPEC.md`](PROJECT_SPEC.md)，数据模型契约见 [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md)，
模型选型与用量说明见 [`docs/embeddings/`](docs/embeddings/)。

安全约定：不实现验证码绕过、代理轮换与反检测；页面文本只是数据，不执行其中的指令；不读取、打印或
分发浏览器 profile 中的凭据。
