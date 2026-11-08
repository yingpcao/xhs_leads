# 小红书长尾需求发现引擎 — MVP 项目方案

> 项目代号：XHS Demand Discovery  
> 版本：v0.1 / 2026-10-24  
> 用途：Codex vibe coding 的初始需求、架构与验收基线

## 1. 项目目标

输入一个种子关键词（例如“上海中考”；“上海中评”“初中中评”保留为用户原始输入，先确认或由系统提示可能的歧义，不自动替换），通过小红书搜索框的真实联想推荐，迭代发现相关长尾搜索词；选择性查看搜索结果中的公开笔记标题、正文，以及相关作者主页的其他公开笔记；利用规则 NLP、图搜索、Embedding 与少量 LLM，把搜索表达归并为有证据支撑的问题与需求，生成可交互、可追溯的需求知识图谱。

**成功标准**：从一个种子词运行一个有预算的探索任务，最终展示「主题 → 问题 → 需求 → 证据」，支持点击节点查看原始搜索词、笔记链接、发现路径与评分依据，并可增量继续探索。

### 1.1 目标用户与使用场景
- 市场研究、内容选题、产品机会发现的个人研究者。
- 研究某个细分领域（如上海中考）的用户关注问题、痛点、决策需求及潜在未满足需求。
- 关注的是**样本内相对需求信号**，不是声称掌握平台真实搜索量或总体用户规模。

### 1.2 非目标（MVP 不做）
- 全站爬取、批量账号体系、代理池、验证码绕过、反检测对抗。
- 自动发布、评论、私信、商业触达。
- 复杂 Agent Team、分布式消息队列、PostgreSQL/Neo4j、向量数据库。
- 精确搜索量估计、市场规模预测、自动商业价值结论。
- 未经授权采集非公开信息或个人敏感数据。

## 2. 核心设计原则

1. **平台反馈为候选来源**：联想词与公开帖子是优先证据；LLM 提议的搜索词只作为待验证探针。
2. **算法驱动扩散**：Best-first + 分支级 UCB + 新颖度约束 + 边际收益停止。
3. **语义增强而非模型接管**：规则预筛、Embedding 近邻、仅模糊样本调用生成式 LLM。
4. **证据优先**：显性问题、模型推断需求、产品方案三者严格区分。
5. **可恢复、可复现**：保存采集时间、搜索会话、推荐位置、运行配置、原始证据引用与处理版本。
6. **合规和保守访问**：仅访问可合法获取的公开页面；尊重平台条款、访问限制及隐私；出现验证、封禁、访问拒绝时暂停，不绕过。

## 3. MVP 用户流程

1. 用户在本地 Web UI 输入种子词、最大探针数、帖子采样数，启动任务。
2. 浏览器适配器打开搜索页面，在搜索框输入关键词并等待联想词稳定出现。
3. 记录推荐词、排名、父词、采集时间、页面状态；做标准化与去重。
4. 调度器从候选队列选择下一关键词，进行纵向、回退、横向与问题导向扩散。
5. 对高价值词进入搜索结果，采样公开帖子；必要时查看相关作者其他公开帖子。
6. 规则 NLP 抽取高频词组、疑问句、痛点词与问题候选；Embedding 去重与聚类。
7. LLM 批量抽取难以规则化的问题/需求，对模糊合并做裁决；所有输出附证据引用。
8. 构建关键词发现图与需求知识图；显示分支、评分、证据、运行统计。
9. 当达到预算或边际收益阈值时停止；支持从 SQLite 恢复、追加运行及导出 JSON/CSV/GraphML。

## 4. 系统架构

```text
Local Web UI (Streamlit MVP)
    │  create / pause / resume / inspect / export
    ▼
Run Controller ───────────── SQLite (persistent state)
    │
    ├── Search Scheduler (priority queue + branch UCB + budget)
    │       └── Browser Adapter (Playwright; compliant public UI)
    │               ├── autocomplete suggestions
    │               ├── search-result public posts
    │               └── selected public author posts
    │
    ├── NLP Pipeline (normalization, jieba, n-gram, PMI, TF-IDF)
    ├── Embedding Service (optional; local Qwen3-Embedding-0.6B)
    ├── LLM Extractor (optional; batched structured JSON)
    ├── Need Clusterer (rules + similarity + selective adjudication)
    └── Graph Builder (NetworkX → interactive graph/export)
```

MVP 采用单任务运行控制器 + 单浏览器工作进程；语义处理可以异步批处理，但不要引入分布式基础设施。浏览器访问的具体可行性应先做 spike 验证，页面 DOM 和登录要求可能变化。

## 5. 搜索扩散算法

### 5.1 关键词图

- 节点：规范化 keyword、原始写法、状态、首次/末次发现时间、探索次数、深度、所属主题分支。
- 有向边：parent → suggested_keyword，包含 rank、timestamp、session、来源和出现次数。
- 同一关键词可有多个父节点；保留所有真实发现路径。
- 规范化仅处理空白、全半角等；不要过度清洗掉地域、年份、否定词、数字。

### 5.2 四种扩散算子

| 算子 | 输入 | 输出 | 限制 |
|---|---|---|---|
| 推荐纵向扩散 | 搜索框真实联想词 | 新候选词 | 优先级最高 |
| 回退扩散 | 长搜索词 | 删除后缀/片段后的短词 | 不产生语义残缺词 |
| 内容横向扩散 | 帖子抽取的领域词组 | 锚词 + 高频短语 | 必须经平台反馈验证 |
| 问题扩散 | 问题模式/痛点短语 | 新搜索探针 | 有来源、有预算 |

### 5.3 优先级

对候选词计算归一化特征：

- R：主题相关度（字符 n-gram TF-IDF / 可选 Embedding）
- N：新颖度（与已探索词的最大相似度取反）
- I：意图强度（怎么、如何、区别、避坑、资料、价格等）
- E：内容证据（独立帖子、显性问题数量）
- C：多父路径发现共识
- U：分支级探索奖励（UCB）

初始可用 `P = .25R + .20N + .20I + .15E + .10C + .10U`，权重配置化。新词缺少证据时不要直接记为零，可用中性先验并保留探索份额。UCB 用于**主题分支/扩散算子**，而非只出现一次的关键词：

`UCB(branch) = mean_reward(branch) + c * sqrt(log(1 + total_branch_trials)/(1 + branch_trials))`

单次奖励：`(新增有效问题簇 + 0.2 × 新增相关关键词) / 标准化操作成本`。

预算初始分配：60% 高优先级利用、25% 未充分探索分支、15% 随机/新颖探索。另在分析统计上将主动探索样本与覆盖校准样本区分，以避免自适应采样偏差。

### 5.4 退出与恢复

- 硬上限：默认 300 次探针，用户可改为 500；每次搜索和帖子访问均有独立预算。
- 收敛：最近 50 次搜索新增有效问题簇 < 3、推荐重复率 > 85%、单分支连续 15 次无新增；均为**可调试验阈值**，避免单条件误停。
- 页面异常、验证码或拒绝访问：暂停并记录原因，不自动绕过。
- 所有队列状态、运行配置与访问结果持久化，恢复不重复消费已完成任务。

## 6. 内容采样与 NLP

### 6.1 内容采样

- 高优先级关键词：最多 10–20 篇可访问公开帖子。
- 中优先级：最多 5 篇；低优先级：暂不进正文。
- 作者主页：只在与研究主题高度相关且确有必要时查看少量公开帖子（例如最多 10 篇），并记录来源；不推断或收集不必要的个人属性。
- 标题、正文、时间、公开链接、来源查询词、可见标签与采集时间；正文不可访问时仅保留标题，不臆造。
- 用稳定内容标识 + URL 去重；文本指纹用于近重复识别。

### 6.2 传统 NLP

- jieba / pkuseg 分词、停用词、领域词典。
- 字符 2–4 gram、TF-IDF、PMI 发现短语；TextRank 可作为可选对照。
- 规则意图：信息查询、方法指导、比较决策、风险规避、资源获取、成本约束、效果提升、经验验证。
- 从标题与正文识别问句、对象、痛点、目标；为每条候选保留原文片段和 post_id。
- 机构发布、广告/营销内容与个人求助应做来源类型标记，不能用帖子数替代用户数。

## 7. Embedding 和 LLM 的边界

### 7.1 Embedding（第二阶段引入）

- 模型：本地 `Qwen3-Embedding-0.6B` 或同类轻量中文向量模型；提供统一接口以便替换。
- 对关键词、问题候选和需求规范表达计算向量并缓存；不强制使用向量数据库，SQLite 存 metadata、向量可存文件或 BLOB。
- 相似度先筛候选，再比较 `topic / object / intent / pain_point` 属性。
- 阈值（例如 0.72/0.88）只是初始实验值，必须用人工标注集校准；高相似度不代表一定可合并。

### 7.2 生成式 LLM（第三阶段引入）

只处理三类任务：

1. 对规则难处理的帖子**批量抽取**显性问题、推断需求、目标、痛点与证据引用。
2. 对聚类边界模糊样本**裁决是否合并**，避免错误吞并长尾需求。
3. 每 50 次搜索进行一次**低频策略诊断**，输出未覆盖主题和预算建议，不直接控制浏览器。

强制 JSON Schema、有限枚举、多条 evidence_id、失败重试和结果缓存。模型不得编造帖子事实；显性、推断和解决方案分别存储，置信度为启发式分数，不声称经过概率校准。

建议输出：

```json
{
  "post_id": "p123",
  "explicit_problems": [
    {"text": "不知道如何排序志愿", "evidence_quote": "志愿到底怎么排", "intent": "决策支持"}
  ],
  "inferred_needs": [
    {"need": "基于成绩和地区的志愿排序辅助", "evidence_ids": ["p123"], "status": "inferred"}
  ],
  "solution_ideas": []
}
```

## 8. 数据模型（SQLite）

建议最小表：

| 表 | 关键字段 | 用途 |
|---|---|---|
| runs | id, seed, config_json, status, created_at, finished_at | 一次探索任务 |
| keywords | id, normalized_text UNIQUE, first_seen_at, status | 搜索词节点 |
| suggestions | id, run_id, parent_keyword_id, child_keyword_id, rank, observed_at, session_id | 推荐边及证据 |
| probes | id, run_id, keyword_id, status, started_at, ended_at, error, raw_snapshot_path | 搜索探针记录 |
| posts | id, canonical_url, title, body, published_at, fetched_at, author_ref, content_hash, access_status | 公开笔记 |
| post_queries | post_id, keyword_id, run_id, result_rank | 搜索结果来源 |
| authors | id, public_ref, relevant_post_count | 最小化公开作者引用 |
| phrases | id, text, score, source_post_id, extractor_version | NLP 短语 |
| problems | id, canonical_text, topic, object, intent, pain_point, extraction_method, confidence | 问题 |
| needs | id, canonical_text, category, evidence_status, score, updated_at | 需求簇 |
| evidence | id, source_type, source_id, problem_id, need_id, excerpt, relation, extractor_version | 可追溯证据 |
| graph_edges | id, src_type, src_id, edge_type, dst_type, dst_id, weight, evidence_id | 图谱关系 |
| model_calls | id, task, model, input_hash, status, token_usage, output_path | 成本/可复现 |

关键索引：`suggestions(run_id,parent_keyword_id)`、`probes(run_id,keyword_id)`、`post_queries(keyword_id)`、`evidence(need_id)`；通过 schema migrations 管理版本。个人信息尽量最小化，支持删除和数据保留期限配置。

## 9. 双图谱与需求评分

**发现图**：Keyword → SUGGESTS → Keyword；用于复盘搜索路径、分支收益和覆盖。

**需求图**：Topic → Problem → Need → Evidence(Post)，并通过 EXTRACTED_FROM 关联 Keyword；用于分析问题、痛点与证据。

需求分数（仅样本内相对强度）：

`DemandScore = .35*S + .30*Q + .20*A + .15*F`

- S：推荐信号（位置、多路径出现、跨会话稳定性）
- Q：表达明确问题的去重公开帖子数
- A：不同公开来源的支持度，防止单一作者刷屏
- F：近期持续性

所有特征进行对数缩放与分位数归一化；评分输出特征分解。`Opportunity = DemandScore × (1 - SupplySatisfaction)` 仅作为后续实验功能，MVP 不宣称准确评估供需缺口。

## 10. 项目目录

```text
xhs-demand-discovery/
├── README.md
├── PROJECT_SPEC.md
├── pyproject.toml
├── .env.example
├── config/
│   ├── default.yaml
│   ├── intent_rules.yaml
│   └── domain_terms.txt
├── src/xhs_discovery/
│   ├── app.py                 # Streamlit UI
│   ├── cli.py                 # 命令行
│   ├── config.py
│   ├── db.py
│   ├── models.py             # Pydantic schemas
│   ├── browser/
│   │   ├── base.py           # 抽象接口
│   │   ├── xhs_playwright.py # 真实适配器（可用性验证后实现）
│   │   └── fixture.py        # 离线样本适配器
│   ├── exploration/
│   │   ├── scheduler.py
│   │   ├── expanders.py
│   │   └── scoring.py
│   ├── nlp/
│   │   ├── normalize.py
│   │   ├── phrases.py
│   │   ├── intents.py
│   │   └── clustering.py
│   ├── semantic/
│   │   ├── embeddings.py
│   │   └── llm_extract.py
│   ├── graph/
│   │   ├── build.py
│   │   └── export.py
│   └── pipeline.py
├── tests/
│   ├── fixtures/
│   ├── test_scheduler.py
│   ├── test_normalize.py
│   ├── test_clustering.py
│   ├── test_graph.py
│   └── test_pipeline_fixture.py
└── data/                     # gitignored: db, cache, exports
```

## 11. 关键接口契约

```python
from typing import Protocol

class SearchSource(Protocol):
    async def get_suggestions(self, keyword: str) -> list[dict]: ...
    async def search_posts(self, keyword: str, limit: int) -> list[dict]: ...
    async def get_public_posts(self, author_ref: str, limit: int) -> list[dict]: ...

class SemanticExtractor(Protocol):
    def extract(self, posts: list[dict]) -> list[dict]: ...

class NeedClusterer(Protocol):
    def assign(self, problems: list[dict]) -> list[dict]: ...
```

`SearchSource` 至少实现 `FixtureSearchSource` 和 `PlaywrightSearchSource`；前者用于无网络 CI 与算法开发，后者必须在实际页面上验证 DOM/可访问性，不使用虚构选择器。返回值后续用 Pydantic 类型细化，记录来源、时间、状态和错误。

### 11.1 核心循环

```python
while budget.remaining() and queue:
    item = scheduler.pop()
    if cache.recently_probed(item.keyword):
        continue
    result = await source.get_suggestions(item.keyword)
    repository.save_probe_and_edges(item, result)
    candidates = expand_from_suggestions(result)
    if scheduler.should_validate(item):
        posts = await source.search_posts(item.keyword, limit=10)
        repository.save_posts(posts)
        phrases, problems = nlp.extract(posts)
        candidates += expand_from_evidence(item.keyword, phrases, problems)
        semantic_queue.enqueue_uncertain(problems)
    scheduler.update_branch_reward(item, repository.new_evidence())
    scheduler.enqueue_filtered(candidates)
    if stopping_policy.converged():
        break

semantic_pipeline.process_batches()
need_graph = graph_builder.build(repository)
```

## 12. 界面与交付物

MVP 页面：

1. **任务配置**：种子词、预算、模式（fixture/live）、采样量、LLM 开关。
2. **运行看板**：探针进度、发现关键词数、帖子数、有效问题簇、失败原因、最近发现。
3. **关键词图**：父子推荐关系、搜索深度、来源和优先级。
4. **需求图谱**：按主题/意图过滤，点击需求查看问题、关键词、证据链接、显性/推断标签。
5. **导出**：keywords.csv、needs.csv、evidence.csv、graph.graphml、report.json。

先用 Streamlit + NetworkX/PyVis 快速验证；复杂前端留到后续。

## 13. 开发阶段与验收标准

### P0 — 离线闭环（优先完成）
- 建立目录、配置、SQLite migration、fixture 数据源。
- 实现搜索图、优先级队列、去重、预算、停止、断点恢复。
- 规则 NLP 识别问题和初步需求，生成图谱与导出。
- **验收**：`pytest` 全通过；使用固定 fixture 从种子跑通，输出可追溯的需求节点；重复运行不会重复写入同一证据。

### P1 — 浏览器接入可行性验证
- 仅在用户允许且平台允许的情况下，用真实浏览器人工辅助验证搜索框联想词是否可稳定读取、公开帖子标题/正文是否可访问。
- 记录页面状态、访问限制与失败案例；实现保守频率、指数退避、暂停/恢复。
- **验收**：至少能完成少量真实关键词的联想采集并保存来源；若页面不可访问，fixture 模式仍完整可用；不绕过访问限制。

### P2 — Embedding 与聚类
- 接入本地 Embedding、缓存、近邻候选召回、属性约束合并。
- **验收**：固定标注集上比较 TF-IDF 基线与 Embedding 的去重/聚类准确率，保留实验结果。

### P3 — 选择性 LLM
- JSON Schema 抽取、证据校验、批处理、模糊边界裁决、成本日志。
- **验收**：所有生成需求有 evidence_id；无证据推断单独标记；可关闭 LLM 完整运行；记录模型调用次数和错误率。

### P4 — 实验与调参
- 300–500 次探针预算实验（须符合平台限制），比较 BFS、Best-first、混合 UCB。
- **验收**：输出每 100 次探针新增有效需求、重复率、覆盖率、成本、错误合并率与停止曲线。

## 14. 评估设计

构建 200–300 条人工标注样本，覆盖：明确提问、隐性困惑、广告营销、近义重复、跨主题、反问/否定、不同年份与地域。建议至少双人复核部分样本。

对照：A 规则+TF-IDF；B 规则+Embedding；C 规则+Embedding+选择性 LLM。

指标：问题抽取 Precision/Recall/F1、同义需求合并准确率、错误合并率、每 100 次探针有效新增问题数、覆盖校准样本中的主题覆盖率、单需求计算成本、模型调用数。所有实验固定采集快照，避免平台推荐漂移干扰对比。

## 15. 运行与运维要求

- `.env` 保存模型 endpoint/key；不提交 cookie、会话、token、私人内容。
- SQLite WAL、事务写入、任务状态机（queued/running/paused/completed/failed）。
- 结构化日志；每次浏览器调用记录结果状态和耗时；失败重试次数有限。
- 页面访问限制、验证码、封禁迹象：立即暂停并提示人工检查，不尝试绕过。
- 支持清除历史数据、配置数据保留周期；输出默认仅包含最小必要公开引用。
- 搜索推荐可能受账号、时间和排序机制影响；展示采样条件和局限。

## 16. 需要 Codex 先确认的技术风险

1. 小红书当前搜索联想是否可在可接受的访问条件下稳定观察；是否必须登录；登录态如何由用户手动提供而不泄露凭据。
2. 公开搜索结果、帖子详情与作者主页的页面结构是否可持续访问；部分内容不可访问时如何降级。
3. 小规模试验中推荐词重复率、平均分支度、真实内容验证成本。
4. 中文问题聚类的领域词典和阈值如何通过标注样本校准。
5. 访问条款与使用范围；不应以规避风控作为实现目标。

## 17. 给 Codex 的首轮执行指令

> 你是本项目的实现工程师。阅读 `PROJECT_SPEC.md`，严格按 P0 → P1 → P2 → P3 的顺序推进。先实现**可完全离线运行**的 MVP：Python 项目结构、Pydantic 模型、SQLite schema、FixtureSearchSource、关键词图搜索（Best-first + 分支级 UCB）、规则 NLP、需求证据图、Streamlit 最小页面、CSV/GraphML 导出及 pytest。不要一开始接入真实小红书、LLM、分布式框架或数据库服务。为所有外部依赖定义接口和 fixture。每完成一个阶段运行测试、汇报新增功能、剩余问题与下一步计划。P0 完成后先报告验收结果，再进入真实浏览器适配器可行性验证。任何页面访问限制都应暂停，不得实现验证码绕过、代理轮换或反检测对抗。所有需求节点必须可追溯到证据，不能将 LLM 推断当作事实。

## 18. 第一版 Done 定义

- [ ] 一条命令启动本地应用与 SQLite 数据库。
- [ ] 可输入种子词并创建探索任务。
- [ ] Fixture 模式完成至少 3 层关键词扩散。
- [ ] 能暂停、恢复、避免重复探针。
- [ ] 可查看推荐词发现图及父子路径。
- [ ] 可从模拟帖子提取问题、归并需求并展示证据。
- [ ] 可查看需求图谱并导出 CSV/GraphML/JSON。
- [ ] 可关闭所有模型依赖并正常运行。
- [ ] 测试覆盖调度、去重、停止、证据追溯、重复运行幂等。
- [ ] README 含安装、运行、测试、配置和限制说明。

---

**总体技术取舍**：先用纯算法实现可靠的数据发现与可追溯闭环，再以 Embedding 和选择性 LLM 提升语义精度；不要把搜索操作和每一步调度交给大模型。
