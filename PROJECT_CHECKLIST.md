# 小红书长尾需求引擎：项目开发 Checklist

更新日期：2026-10-24（Asia/Shanghai）。本文件统一跟踪任务、依赖、交接与验收；产品要求见 `PROJECT_SPEC.md`，实施细节见 `DEVELOPMENT_PLAN.md`，Qoder 首轮任务见 `QODER_TASK.md`。

## 1. 当前状态与协作规则

**负责人变更（2026-10-30）**：按用户要求，停止本项目 Qoder 开发与派发，Codex 直接接手后续实现、测试与交付。派发入口已设置阻断，见 `docs/coordination/QODER_DISPATCH_DISABLED.md`。以下旧交接与角色状态为历史，不构成重新派发授权。

**当前状态（2026-10-29）**：P0/G0/P1标题版G1与P2离线基础/标注workflow已PASS；DashScope隔离API接入工程独立PASS（最终1226全套201.21s/exit0，报告2026-10-29-P2-DASHSCOPE-CODEX-REVIEW.md）。无活动Qoder/测试，等待真实200-300人标及后续实验输入/预算范围；唯一API smoke已消耗，不重发/不启用生产。真实P2/G2未过，正文作者OPEN，监测ACTIVE。

最新独立验收（2026-10-26 14:24）：P0-C / P0-11～21 DONE，C-08/C-09/C-10关闭。最终独立574 passed in70.12s，范围与逐项证据见 `docs/verification/2026-10-26-P0-C-CODEX-REVIEW.md`。P0-D/P0-22～29 已由 Qoder 于 14:40 接收并标 DOING；整个P0/G0未完成。

最新复核（2026-10-24 21:42，Asia/Shanghai）：P0-A、P0-B 已由 Codex 独立复核通过。P0-B 最终全套 332 passed in 33.04s，C-02～C-07 在存储与控制机制边界关闭，证据见 `docs/verification/2026-10-24-P0-B-CODEX-REVIEW.md`。P0-C（P0-11～P0-21）已由 Qoder 于 22:05 接收并标 DOING，全程离线、不接模型与真实平台；整个 P0/G0 未完成。

最新交接（2026-10-26 20:47，Asia/Shanghai）：**P0-D / P0-22～29 实现与自测完成，转 REVIEW**（等待 Codex 独立复核，八项复选框未自行勾选，未进入 P0-E）。交付：`analysis/{lexicon,phrase_mining,corpus,extraction,clustering,scoring,graph,export,store,pipeline}.py` 十个模块 + `storage/migrations.py` 新增 **v4/v5**（卡片可见字段 `published_at_text/likes_text`、`content_family_members` 成员行、`needs.stable_key`、`phrases.analysis_version` + 部分唯一索引）+ `docs/DATA_MODEL.md` 第 9.3～9.5 节 #33～#47。测试：`.venv/Scripts/python.exe -m pytest -o addopts="" -q` → **719 passed in 55.69s，退出码 0**（574 既有一条未删、未 skip + P0-D 新增 145，其中 142 例在 8 个 `tests/test_analysis_*.py` 专项文件、3 例为 v4/v5 schema 形状断言随 `tests/test_storage_migrations.py` 补齐）；8 专项文件合并 **142 passed in 17.73s**，相邻回归 10 文件 **203 passed in 17.67s**，退出码 0。Codex 14:24 点名的反例（属性冲突/否定/广告/未知日期/重复内容/失效引用、GraphML 类型、CSV 安全、JSON 配置·版本·预算·局限、导出反读）逐条对照见报告第 3 节。口径：分析一律写**新 `analysis_version`**，编排层在任何一阶段动手前拒绝 `p0-preliminary.1` 与在线抽取器版本，`branch_stats` 与初步行逐字段不变；分析不写预算账本（跨层闭环用例核对 `budget_ledger` 行数前后一致）；联想只进 `search_intent_candidate` 分区、永不产生帖子证据行；展示需求必须有可定位证据，五类失效引用码把坏引用挡出排行榜。全程离线、未访问真实平台、未读凭据、未下载模型权重、未接外部 LLM/API、未提交或推送；`export`/`run` 命令行仍退 **70**（接线属 P0-E，未做假 stub）。已知限制见报告第 7 节（数据集专属词表不在库里、`nlp.tfidf_batch_size` 暂无消费方、同批次共用一个时间戳、评分常量不进配置、模型预算恒为 0）。

上一轮交接（C-10 返工，2026-10-26 13:40，Asia/Shanghai）：**C-10 返工完成，P0-C 第三次转 REVIEW**（等待 Codex 复核，未自行勾选 DONE，未进入 P0-D）。缺陷在 `discovery/engine.py::_execute`：access、write_artifact、attach 包在同一个 try，任何异常都 `release()` 预留并 `finish_attempt(failed)`，从不结算——复核复现 `source_returned=1、charged=0.0、reservations_open=0`。修复按**访问阶段**收口：`not_started`（请求未交出）与 `access` 阶段的数据源证据 `AccessNotStarted` 才允许退回预留；`access` 阶段源抛异常（无法证明未访问）与 `local` 阶段（结果已返回、只是落盘/入库失败）一律照计一笔。每 attempt 至多一笔访问账（结算幂等 + `budget_ledger` 部分唯一索引）；结算自身失败不掩盖原始异常、预留保持未消费交给恢复补记；attempt 的 `error_json` 新增 `access_phase/access_charged/billing_error`，事件新增 `attempt_charged_after_failure`/`attempt_refunded_not_accessed`；`run_guarded()` 的 `finally` 放锁路径未动。契约补齐：`sources/base.py` 新增 `AccessNotStarted(ValueError)`（继承 ValueError 以不放宽既有数据源契约断言），fixture 的未绑定会话与 `limit<1` 两处真实"证明未发起"改用它。测试：`tests/test_engine_worker.py` **39 passed**（新增 9 例覆盖 Codex 六项验收：写盘失败、入库失败、源中途抛异常、有证据未发起（两阶段参数化）、结算也失败的恢复补记、已计费 attempt 重放不重复、额度只剩 1 次的越界防护），相邻存储/源契约 5 文件 **185 passed**，C-09 专项 **71 passed**，全套 **574 passed in 60.79s / 退出码 0**（565 既有 + 9 新增，一条未删、未 skip）。按复核要求改写 `test_run_guarded_releases_both_locks_when_the_source_explodes` 的"异常必定 used=0"假设（0.0 → 1.0），锁与租约释放、`ghosts=0`、历史 attempt 不被改写等断言原样保留，未放宽任何预算要求。证据见 `docs/verification/2026-10-24-P0-C.md` 第 9 节；`docs/DATA_MODEL.md` 已同步（第 2 节 `operation_attempts`/`run_events`、第 6 节第 17 条、第 9.1 节补 #27 并新增 #29）。

更早交接（C-09 返工，2026-10-26 12:10，Asia/Shanghai）：**C-09 返工完成，P0-C 重新转 REVIEW**（等待 Codex 复核，未自行勾选 DONE，未进入 P0-D）。修复分两层：① `storage/migrations.py` 新增 **v3**，给 `body_observations` 补 `run_id/operation_id/attempt_id/session_id`（可空 + `UNIQUE(operation_id, attempt_id, session_id)`，不新增表）；② `Repository.require_observation_source()` 在写库前核对操作归属/类型/attempt 归属/非空 session（与 C-07 同口径，拒绝而非返回 None），`ingest_body` 改用与联想、卡片同口径的幂等键，`engine._existing_observation` 覆盖 `body_observations`，`content_validation_count` 同时核对观察行与 `operations` 的 `run_id`，来源 NULL 或不自洽一律按 0 计。测试：专项 `tests/test_body_provenance.py` **10 passed**（含 Codex 指定的 B 0→0→1 链、同帖同时间戳跨 run 隔离、重复 attempt 不写第二行、迁移兼容、收敛反例），相关 6 个文件 **133 passed / 退出码 0**，全套 **565 passed in 81.17s / 退出码 0**（554 例既有 + 11 例新增，一条未删）。证据见 `docs/verification/2026-10-24-P0-C.md` 第 8 节；`docs/DATA_MODEL.md` 已同步（第 3 节 `body_observations`、第 6 节第 16 条、第 7 节索引、第 9.1 节 #28）。

首轮 REVIEW 交接（2026-10-26 11:25，Asia/Shanghai）：P0-C 实现与自测完成（代码与首轮全套于 2026-10-25 01:53 收尾，本机为云电脑、两轮之间被远程关机故跨日），收工前复跑全套 **554 passed / 退出码 0**（37.30s、63.75s 两轮，P0-A/P0-B 既有 332 例一条未删 + P0-C 新增 222 例），转 **REVIEW**。自测驱动真实主循环时发现并修复 7 处跨模块衔接缺陷，其中 2 处落在 P0-B 已复核通过的文件（`storage/artifacts.py`、`discovery/priority.py`），登记为 **C-08**（FIXED，待复核）。

本轮接收（2026-10-26 14:40，Asia/Shanghai）：Qoder 接收 Codex 14:24 的 P0-C 验收通过交接，**P0-D / P0-22～29 标 DOING**（已于 20:47 实现并自测完成，转 REVIEW）。范围按 `DEVELOPMENT_PLAN.md` 第 5.1～5.4 节与 P0 表原定细节，不扩大：领域分词/字符 n-gram/TF-IDF/PMI 与低频降级、八类意图与来源类型及问题抽取（保留地域/年份/对象/否定）、可定位原文证据与版本·操作引用、稳定需求 ID 与保守可撤销归并、显性问题/推断需求/方案构想/联想意图候选四类陈述、S/Q/A/F 评分分解与内容去重及未知属性处理、关键词发现图 + 主题→问题→需求→证据图、五类产物导出与反读验证。约束：全程离线，不接真实平台与外部模型、不读取凭据、不提交或推送；分析写入使用**新的 analysis_version**，不改写 P0-C 的历史奖励与 `p0-preliminary.1` 行；所有展示需求必须有有效证据，generated 联想不得冒充帖子事实；保持既有 574 项回归一条不删；补 Codex 点名的反例（属性冲突/否定/广告/未知日期/重复内容/失效引用、GraphML 类型与 CSV 安全、JSON 配置版本预算局限、导出反读）。CLI/UI 接线留 P0-E，`export` 命令仍退 70。完成后转 REVIEW，不自行勾 DONE。

上一轮接收（2026-10-26 12:34，Asia/Shanghai）：Codex 复核给出 **C-10**（`docs/coordination/QODER_GUIDANCE.md` 顶部 12:34 条目）——C-09 专项通过，但 **P0-C 仍不通过、不进 P0-D**，回 DOING 返工。问题：`discovery/engine.py::_execute` 把 access / write_artifact / attach 包在同一 try，任何异常都释放预留并记 `failed`，从不结算；已发生的外部访问被退款，重启也可能无法补账。要求区分"未发起访问 / 访问结果不确定 / 已返回但本地落盘·入库失败"三阶段并保守计费、维护每 attempt 至多一次结算与恢复一致性、保持 `finally` 放锁，并更新 `test_run_guarded_releases_both_locks_when_the_source_explodes` 的"异常必定 used=0"错误假设。

前一轮接收（2026-10-26 11:40，Asia/Shanghai）：Codex 复核给出 **C-09**（`docs/coordination/QODER_GUIDANCE.md` 顶部 11:30 条目）——**P0-C 暂不通过、不进入 P0-D**，P0-C 回 **DOING** 返工。问题：正文观察缺少 run/operation/attempt 来源，`discovery/stopping.py::content_validation_count` 只按全局 `post_id` 连本 run 的 `post_queries`，run A 的 completed 正文会被 run B（只有同帖标题卡片）算成自己的内容验证量；`ingest_body` 用 `(post_id, captured_at)` 去重也会跨 run 借用既有行。核实：`body_observations` 无 `operation_id` 列，需以迁移补齐可审计来源关系后再 JOIN，不得引用不存在的列。

| 角色 | 职责 | 当前指派状态 |
|---|---|---|
| 用户 | 需求取舍、人工登录、使用体验与最终验收 | 已确定 |
| Qoder | 已停止本项目开发 | 不再派发；历史成果保留 |
| Codex | 直接实现、测试、审查证据、修复与交付 | 已接手，沿用已验收基线与现有实验边界 |

状态约定：TODO 未开始；DOING 已确认执行；REVIEW 有实现及自测证据待复核；DONE 复核通过；BLOCKED 有具体阻塞；REOPEN 复核失败或回归。复选框只在 DONE 时勾选。下文未注明状态的未勾选项均为 TODO。

执行约定：

1. ID 固定，新增项分配新 ID，不因排序而重编号。以 ID 交接，不用“基本完成”代替状态。
2. Codex 直接承接开发与验证，不再派发 Qoder；人员变化、DOING、REVIEW、BLOCKED、REOPEN 在第 9 节登记。计划中的职责不等于任务已送达。
3. 同时最多一个实现工作包 DOING；关联文件由一个执行者修改。复核者发现问题先登记和反馈，避免同时覆盖同一文件。
4. 勾选必须附文件/产物路径、实际命令与结果、复核结论；截图、口头描述、模型“已完成”不足以证明代码或测试通过。
5. BLOCKED 写明原因、恢复条件、负责人和下一步；平台限制不绕过，离线任务可继续。
6. 阶段门禁通过后才推进下一阶段；文档准备可提前，真实采集和模型接入不能提前混入 P0。
7. 本清单不改变用户最新指令的范围。若用户要求“先理解”，只记录理解结果，不据清单擅自启动开发。
8. 调整目标或实现方案时先记录变更与验收影响；能力降级必须显式展示，不能把原需求静默删掉。

## 2. 阶段总览

| 阶段 | 入口依赖 | 工作量参考 | 当前状态 | 退出依据 |
|---|---|---|---|---|
| 准备 | 无 | 已有文档 | DONE（文档齐备） | B-01～B-03 |
| P0 离线 MVP | 准备完成 | 9–13 人日 | DONE | G0独立通过：2026-10-28-G0-CODEX-REVIEW.md |
| P1 真实标题证据版 | G0 | 3–5 人日 | TODO | G1 全部通过 |
| P1 正文/作者能力 | G0，先做可行性验证 | 验证后估算 | TODO | 能力结论及实现/阻塞说明 |
| P2 Embedding | G1 | 3–5 人日及人工标注 | TODO | G2 全部通过 |
| P3 选择性 LLM | G2 决策完成 | 3–5 人日 | TODO | G3 全部通过 |
| P4 对照实验与交付 | G3 决策完成 | 2–4 人日及采样等待 | TODO | G4 全部通过 |

模型增强未达发布标准时允许完成实验并明确保留规则基线；这不等于增强功能验收通过。正文/作者不可访问时标题版可交付，但完整原需求仍有能力缺口。

## 3. 准备与 P0：离线闭环

### 准备

- [x] B-01 产品需求与原始 Done 定义存在。证据：`PROJECT_SPEC.md`。
- [x] B-02 可执行开发计划存在。证据：`DEVELOPMENT_PLAN.md`。
- [x] B-03 首轮任务及现有工具说明存在。证据：`QODER_TASK.md`、`小红书搜索关键词反查工具_USAGE.md`。完成只表示文档存在，不表示任务已下达。

### P0-A 工程与契约（依赖 B-01～B-03）

- [x] P0-01 验证可用 Python、隔离环境及核心依赖安装/导入，记录实测版本；不修改系统默认环境。
- [x] P0-02 建立 pyproject、src/tests/config 目录、忽略规则、环境变量样例和启动入口。
- [x] P0-03 配置含种子原文、分项预算、采样、随机种子、阈值、保留期限与模型开关；非法值有明确报错。
- [x] P0-04 定义 Pydantic 数据源/操作/证据模型、能力声明、结果状态和错误分类；区分空结果、失败和 unsupported。
- [x] P0-05 编写 `docs/DATA_MODEL.md`，明确全局身份、run 状态、观察版本、证据、逻辑键和索引。

### P0-B 存储与恢复（依赖 P0-04～P0-05）

- [x] P0-06 实现 migrations、SQLite 外键/WAL/事务及 Repository；验证建库、升级和回滚。
- [x] P0-07 实现 run 状态机、事件、run_keywords、持久 frontier 和 branches；验证跨 run 隔离。
- [x] P0-08 实现操作/attempt、预算预留/结算与重试计数；重复导入不重复扣费。
- [x] P0-09 原始产物原子落盘、入库与重启补导入；已完成不重处理，无可靠结果标 uncertain。
- [x] P0-10 实现单 worker 锁/租约、暂停/恢复/追加预算；验证遗留锁与进程崩溃，不承诺外部访问 exactly-once。

### P0-C 数据源与探索（依赖 P0-06～P0-08）

- [x] P0-11 FixtureSearchSource 可完全离线运行；数据明确 synthetic，至少三分支、三层、多父与环路。
- [x] P0-12 异常 fixture 覆盖空、partial、failed、认证暂停、未知作者/日期、广告、地域/年份/否定。
- [x] P0-13 标准化保留地域、年份、数字、否定与原始输入；歧义种子不自动改写。
- [x] P0-14 发现图保留推荐排名、会话、时间、所有父路径；队列去重不丢失观察。
- [x] P0-15 实现联想纵向、完整词组回退、内容横向、问题探针；generated 与 observed 分开。
- [x] P0-16 实现 R/N/I/E/C/U 特征、未知证据先验和分解记录；分支稳定、单次奖励不重复归属。
- [x] P0-17 实现 Best-first、分支 UCB、60/25/15 探索分配、冷启动和确定性随机种子。
- [x] P0-18 内容采样后在线初步归并并更新奖励，成本归操作；后续语义精修不篡改历史奖励。
- [x] P0-19 分项预算、采样优先级、有限重试和队列耗尽可执行；失败重试占预算。
- [x] P0-20 停止策略区分预算/收敛/异常；不足窗口、无内容验证与失败不能伪装收敛，单分支停滞不误停全局。
- [x] P0-21 校准候选池冻结、独立抽样并单列预算/标签；不将自适应样本当平台覆盖率。

### P0-D 规则分析、证据与图谱 — **DONE（2026-10-27，独立762项及缺陷反例通过）**

- [x] P0-22 分词/领域词典、字符 n-gram、TF-IDF、PMI 和最低频次降级可运行。
- [x] P0-23 八类意图及问题候选抽取保留对象、痛点、地域、年份、否定；来源区分求助/分享/机构/营销/未知。
- [x] P0-24 每条证据保留内容版本、title/body/suggestion、quote/位置及来源操作，能在原文定位。
- [x] P0-25 保守归并与稳定需求 ID；属性冲突不自动合并，归并可撤销，原始问题保留。
- [x] P0-26 区分显性问题、推断需求、方案构想；联想意图候选不冒充帖子事实。
- [x] P0-27 实现 S/Q/A/F 分解、内容家族去重、未知来源/日期处理和证据完整度；不把点赞当搜索量。
- [x] P0-28 构建关键词发现图与主题→问题→需求→证据图；所有展示需求有有效引用。
- [x] P0-29 导出五类产物并反读校验；GraphML 类型兼容，CSV 文本安全转义，JSON 含配置/版本/预算/局限。

### P0-E 界面、命令与验收（依赖 P0-B～P0-D）

- [x] P0-30 CLI 支持 ui/run/pause/resume/extend/export；extend 明确追加额度。
- [x] P0-31 Streamlit 支持种子、模式、预算、采样和模型开关，一条命令启动并建库。
- [x] P0-32 看板显示进度、关键词、帖子、有效问题、失败原因和最近发现；刷新不启动重复 worker。
- [x] P0-33 双图支持交互、主题/意图过滤、点选原词/原文/链接/路径/评分；模拟证据明确标识。
- [x] P0-34 UI 支持暂停、恢复、追加探索及全部导出，后台任务不被页面刷新打断。
- [x] P0-35 pytest 覆盖调度、去重、预算、停止、跨 run、幂等、恢复、归并、证据及导出行为。 DONE（2026-10-28独立G0复核；本次具名测试映射见 P0_ACCEPTANCE.md，完整 829 passed 退出0）
- [x] P0-36 普通测试无平台/登录/模型下载依赖；关闭所有模型时完整运行。 DONE（2026-10-28独立G0复核；tests 无网络/权重调用，干净 venv heavy=[] 离线通过，见 EACCEPT-CLEANENV.json）
- [x] P0-37 README 含安装、启动、演示、测试、配置、数据清理、保留期限和能力限制。 DONE（2026-10-28独立G0复核；README 重写覆盖全部条目；清理仅作说明未执行删除）
- [x] P0-38 完成干净环境端到端和 UI 冒烟；生成 `P0_ACCEPTANCE.md`，记录实际命令、结果及原需求第 18 节逐项证据。 DONE（2026-10-28独立G0复核；隔离 e2e/干净安装/UI 冒烟退出均0；P0_ACCEPTANCE.md 已由Codex接受；独立浏览器深度交互及新安装环境66项通过）

### G0 阶段门禁（依赖 P0-01～P0-38）

- [x] G0-01 原需求第一版 10 项 Done 全部有证据：启动、建任务、三层扩散、恢复、发现图、抽取、需求图/导出、无模型、测试、README。
- [x] G0-02 Codex 复核关键实现并复跑相关测试；无未关闭的阻断缺陷，模拟数据不伪装真实。
- [x] G0-03 汇报 P0 结果和 P1 入口条件；记录是否进入真实数据阶段，不能自行将未验收状态改为通过。

## 4. P1：真实 CLI 与内容能力（依赖 G0）

- [x] P1-01 CliSearchSource 使用参数数组/shell=False，路径/profile/browser 可配置；保存 CLI/schema 版本。
- [x] P1-02 录制协议测试覆盖全部退出码、逐词状态、中文、空/损坏 JSON、输出故障、超时与取消。
- [x] P1-03 stdout/stderr 分离、总进程超时、独立 attempt 输出目录、partial 保存和恢复导入可用。
- [x] P1-04 同 profile 串行、跨调用限频、有限退避；认证/验证立即暂停，不循环重试或绕过。
- [x] P1-05 实测 doctor 与人工登录协作，不读取/打印凭据；记录访问条件与诊断结果。
- [x] P1-06 用 3–5 词、少量卡片完成真实联想/标题闭环，保留原始响应、排名、会话与完整访问 URL。
- [x] P1-07 界面明确 title_only、正文缺失、日期/作者未知及评分局限；真实恢复/导出可核查。
- [ ] P1-08 分别验证公开正文和相关作者公开笔记的可行性，形成访问/成本/必要性结论及后续估算。
- [ ] P1-09 可行时补能力接口、保守采样、来源追踪与测试；不可行时记录明确缺口和恢复条件，此项保持未完成，不以“不支持”勾选为实现完成。
- [x] P1-10 形成 `P1_VALIDATION.md`，区分本项目实测与工具历史记录，列出失败案例和缺口。
- [x] G1-01 复核真实来源闭环、故障暂停、fixture 回归；正文/作者能力状态已披露。平台不可访问时 G1 保持未通过。

## 5. P2：Embedding 与标注评估（依赖 G1）

- [x] P2-UI-01 人工标注页面（2026-10-31，Codex 直接实现与验证，DONE）：复用冻结 packet 与既有导入校验，支持抽取/需求对编辑、显式复核、旧版本备份、多窗口冲突拒绝、筛选搜索和保存后继续标注。专项 56 passed；最终全套 1247 passed in 294.39s，退出码 0，证据见 `docs/verification/2026-10-31-ANNOTATION-QUEUE-CODEX.md` 及对应 PYTEST.log。此工程项不代替 P2-01 真人数据或 G2 效果验收。

2026-10-31 续开发：增加状态筛选、双端原文搜索、保存并跳转下一条待完成记录，修复回到旧记录后的版本误判。新页面与既有工作流专项 56 passed in 9.25s。前日全套会话失效，未获得最终结果，不登记为通过；本日重新运行全套已通过（1247 项），日志 `docs/verification/2026-10-31-ANNOTATION-UI-PYTEST.log`。

- [ ] P2-01 建立 200–300 条人工标注集及说明；部分双人复核，覆盖长尾、营销、否定与地域年份。
- [ ] P2-02 按来源/内容家族划分开发和保留测试集，固定评估口径，避免近重复泄漏。
- [ ] P2-03 验证本机硬件/内存/耗时，记录模型权重、指令、预处理和设备版本。
- [ ] P2-04 接入可替换 Embedding 接口、版本化向量缓存和余弦召回；关闭后不加载模型。
- [ ] P2-05 属性约束合并、阈值校准、模糊样本待裁决、可撤销归并可用。
- [ ] P2-06 比较 TF-IDF 与 Embedding 的抽取/合并指标、成本和错误案例，输出 `evaluation/metrics.json` 及报告。
- [ ] G2-01 审核自动合并 precision≥0.90、错误合并率≤0.05（附样本分母），且至少一项预声明指标改善；不达标明确关闭/限用增强，不伪报通过。

## 6. P3：选择性 LLM（依赖 P2 实验与启用决策完成）

- [ ] P3-01 可配置服务接口、Schema、prompt 版本、缓存和离线录制响应可用，凭据不入库/日志。
- [ ] P3-02 只处理难例批量抽取、边界合并裁决和每 50 次搜索的策略诊断。
- [ ] P3-03 evidence_id 属于输入且有效，quote 可定位；显性/推断/方案隔离，非法输出拒收。
- [ ] P3-04 调用/token/费用预算预留结算、有限重试、成本日志及服务故障规则回退可用。
- [ ] P3-05 模型建议探针仍经调度和平台验证；页面文本不能改变工具或任务指令。
- [ ] P3-06 固定快照比较 A规则 / B+Embedding / C+LLM，记录质量、错误率与单需求成本。
- [ ] G3-01 合法证据入库与故障用例通过，模型关闭回归通过；记录启用/保持关闭的实验结论。

## 7. P4：实验、运维与交付（依赖 P3 实验与决策完成）

- [ ] P4-01 BFS/Best-first/混合 UCB 在相同快照、预算、成本和归并口径下对照，多随机种子复现。
- [ ] P4-02 冻结快照缺数据标 coverage gap；主动探索与覆盖校准指标分开，不宣称总体覆盖率。
- [ ] P4-03 访问条件允许时执行 300–500 探针实验；不能执行则留存阻塞与范围限制，不能用小实验替代。
- [ ] P4-04 输出每百次探针新增、重复率、覆盖、错误合并、成本及停止曲线，区分尝试与成功。
- [ ] P4-05 锁定发布环境；验证安装、日志、留存清理、共享引用保护、迁移与离线降级。
- [ ] P4-06 交付演示数据、复现命令、已知限制、能力矩阵和最终验收报告。
- [ ] G4-01 用户场景验收通过，所有缺口有状态，未实现的正文/作者功能不得宣称完成；发布范围与实际证据一致。

## 8. 跨阶段质量底线

- [ ] Q-01 所有正式需求有可定位证据，数据源、处理版本与采样条件可追溯。
- [ ] Q-02 任务/操作/观察/派生结果清楚分层，多 run 与重复导入行为正确。
- [ ] Q-03 外部访问、模型、存储失败都保留有效数据，预算和状态不虚报。
- [ ] Q-04 登录/验证/访问拒绝暂停；不实现绕过、反检测或未经请求的触达。
- [ ] Q-05 评分不冒充搜索量、人数或商业价值；缺失数据与模型推断明确标识。
- [ ] Q-06 阶段回归通过，文档、命令、能力声明与当前实现一致。

这些是发布检查项，每次阶段验收重新检查；通过后若实现或接口改变，应撤销受影响勾选并记录回归。

## 9. 执行看板、证据与问题记录

### 当前执行看板

| 工作包/ID | 状态 | 执行者 | 复核者 | 前置/阻塞 | 下一动作 |
|---|---|---|---|---|---|
| P0-A / P0-01～05 | DONE（2026-10-24 Codex 独立复核通过） | Qoder | Codex | 范围仅工程/配置/契约/设计 | 复核证据：`docs/verification/2026-10-24-P0-A-CODEX-REVIEW.md` |
| P0-B / P0-06～10 | DONE（Codex 独立复核，2026-10-24 21:42） | Qoder | Codex | P0-A DONE | `docs/verification/2026-10-24-P0-B-CODEX-REVIEW.md`；全套 332 passed in 33.04s，C-02～C-07 已复核关闭；本机磁盘/单 worker 边界 |
| P0-C / P0-11～21 | **DONE（2026-10-26 14:24）** | Qoder | Codex | C-08/C-09/C-10关闭；P0-D 已于 14:40 接收 | 独立574 passed in70.12s；docs/verification/2026-10-26-P0-C-CODEX-REVIEW.md |
| P0-D / P0-22～29 | **REVIEW（2026-10-26 20:47 自测完成，待复核）** | Qoder | Codex | P0-C DONE；离线、不接模型；CLI/UI 接线留 P0-E | 复跑 `pytest -o addopts="" -q`（本轮 719 passed in 55.69s）与 8 个 `tests/test_analysis_*.py`；重点复核 `docs/DATA_MODEL.md` #33～#47 与报告第 7 节已知限制；八项复选框待复核者勾选 |

开始工作后以最新可验证执行状态更新，不把本表的拟交接当作自动授权或已发消息。

### 完成证据登记

| ID | 文件/产物 | 实际命令与结果 | 复核人/结论 | 日期 |
|---|---|---|---|---|
| B-01 | PROJECT_SPEC.md | 本地读取，需求及 Done 项存在 | Codex：文档齐备 | 2026-10-24 |
| B-02 | DEVELOPMENT_PLAN.md | 本地读取，P0–P4 工作包/依赖/验收存在 | Codex：文档齐备 | 2026-10-24 |
| B-03 | QODER_TASK.md；工具_USAGE.md | 本地读取，任务与协议说明存在 | Codex：仅确认文档 | 2026-10-24 |
| P0-01 | `.venv`；`tests/test_environment.py` | `python -m venv .venv` + `.venv/Scripts/python.exe -m pip install -e ".[dev]"` 成功；`.venv/Scripts/python.exe -m pytest tests/test_environment.py` → 3 passed；`doctor` 实测 Python 3.15.0 + pydantic 2.14.1/PyYAML 6.0.3/networkx 3.7/streamlit 1.66.1/jieba 0.42.1/scikit-learn 1.9.1/pytest 9.1.1，子进程断言核心契约未导入 jieba/sklearn/streamlit/torch 等 | 待复核 | 2026-10-24 |
| P0-02 | `pyproject.toml`、`src/xhs_discovery/{cli,paths,relpath,__main__}.py`、`.gitignore`、`.env.example`、`README.md` | `.venv/Scripts/python.exe -m pytest tests/test_cli_and_paths.py` → 28 passed；`--version`/`doctor`/`contracts`=0、无参数=2、非法配置=65、`run/pause/resume/extend/export/ui`=70 并指名归属工作包（输出见验证文档第 3 节） | 待复核 | 2026-10-24 |
| P0-03 | `config/{default,offline-demo,tests-convergence-tiny}.yaml`、`config/{intent_rules.yaml,domain_terms.txt}`、`src/xhs_discovery/config.py`、`tests/test_config.py` | `.venv/Scripts/python.exe -m pytest tests/test_config.py` → 40 passed；`config-check --config config/tests-convergence-tiny.yaml` → VALID hash=c747a82a5a40…；非法配置报 `scheduler.weights 之和必须为 1.0，当前 1.150000` | 待复核 | 2026-10-24 |
| P0-04 | `src/xhs_discovery/models/*.py`、`src/xhs_discovery/sources/base.py`、`tests/test_{source,evidence,scoring,run}_contracts.py` | `.venv/Scripts/python.exe -m pytest` → 177 passed in 2.24s（契约测试 106 例：source 69/evidence 14/scoring 10/run 13）；`contracts` 自检 rejected=3/3；QODER_GUIDANCE 四条复核意见的实现与对应测试见验证文档第 5 节 | 待复核 | 2026-10-24 |
| P0-05 | `docs/DATA_MODEL.md` | 通读校对：分层/列/逻辑键/索引/幂等与恢复规则完整，含新增第 6 节第 7～8 条（关键词身份与产物路径归属）与退出码×error.code 分类；本轮无 DB 可跑迁移，落库与迁移验证属 P0-B | 待复核 | 2026-10-24 |
| P0-06 | `src/xhs_discovery/storage/{db,migrations}.py`、`tests/test_storage_migrations.py`、`tests/test_transaction_atomicity.py` | `pytest tests/test_storage_migrations.py` → **24 passed**；`pytest tests/test_transaction_atomicity.py` → **7 passed**。空库建表=v1+v2、v1 库升级保留原行、`rollback(1)/(0)` 可重复、旧代码开高版本库报 `SchemaError`、迁移中途失败断言 `keywords`/`keyword_aliases` 均不存在且 `applied_versions=()`、**一行两条语句也用 `complete_statement` 正确切分并整体回滚**、**down 脚本半途失败不破坏本层**、**连接上有未提交事务时 `apply_migrations`/`rollback` 明确拒绝且 schema 版本仍 (1,2)**、PRAGMA 四项符合文档、FK RESTRICT、枚举 CHECK、operations 逻辑键。返工新增：Codex 原始复现 `rollback-probe` 外层批次回滚后 `keywords` 行数为 0，嵌套 `transaction()` 只回滚 `SAVEPOINT`、外层已写行不丢 | 待复核 | 2026-10-24 |
| P0-07 | `src/xhs_discovery/storage/repository.py`、`tests/test_storage_repository.py` | `pytest tests/test_storage_repository.py` → 25 passed；状态机只允许白名单迁移（`queued→completed` 越级被拒）、终态必带原因事件、`converged` 无据必拒、run_keywords 每 run 一行、同词跨 run 隔离、frontier 理由必填且空白理由被拒、奖励只归被选中分支、分支停滞不改 run 状态、generated 边不冒充 observed | 待复核 | 2026-10-24 |
| P0-08 | `src/xhs_discovery/storage/ledger.py`、`src/xhs_discovery/models/run.py`、`tests/test_storage_ledger.py` | `pytest tests/test_storage_ledger.py -o addopts=""` → **34 passed**；未配置键不隐含无限、预留先挡后访问、失败不退款、缓存命中记 0 单位、配额单独计量、`model_cost_usd` 小数原样存回；DB 级 `UNIQUE(run_id, operation_id, attempt_no) WHERE external_access=1` 使同一次 attempt 重复结算只落一行。17:28 返工补齐边界：**释放/结算必须声明预留归属**（跨 run/换键/跨操作四类拒绝实测报错）、**访问键拒小数与 0 单位**、**整数配额键拒 1.5**、**结算受 headroom 守卫**、**上限=base+Σ`budget_revisions.added_json` 从库派生**（重建对象与事务内/回滚后分别得 5.0/105.0/5.0）、**未认领预留照样占额度**、**结算忘带 id 自动认领本操作预留**（`used=2.0 outstanding=0.0`，无幽灵预留）、**恢复对已 started 的 attempt 按次补记不退款**。20:57 C-07 返工：**每个写账入口（`reserve`/`settle_external`/恢复补记/`record_cache_hit`/`record_quota`）在写账前核对 `operations.run_id`、库里实际 `operation_type`、预算键↔类型映射、`operation_attempts` 中该 attempt 是否真存在，有/无 `reservation_id` 同口径**；报错原文（`操作 1 属于 run run-b…`、`操作 2 的类型是 suggest，结算却说是 search`、`预算键 suggest_attempts 不属于操作类型 search…`、`操作 2 没有 attempt 9…`）与"拒绝前后 ledger/reservations/revisions 三份快照逐字相同"见验证文档第 2.6 节；**冲突重放拒绝**（`重放参数与既成事实冲突：消耗单位 1 ≠ 2.0`、`…预算键 suggest_attempts ≠ calibration_suggest_attempts`），完全一致的重放仍返回"已记过"且不重复计费 | 待复核 | 2026-10-24 |
| P0-09 | `src/xhs_discovery/storage/artifacts.py`、`tests/test_storage_artifacts.py`、`tests/test_path_resolution.py` | `pytest tests/test_storage_artifacts.py` → 25 passed、`tests/test_path_resolution.py` → 7 passed；原子改名+内容哈希幂等重导、同路径不同内容必拒、run 目录外与符号链接逃逸均在解析后拒绝（本机 `os.symlink` 实测可用，链接测试真实执行非 skip）、补导入把无归属文件登记为孤儿并写事件、终态不重放、`in_flight` 无结果标 `uncertain`、重开连接可恢复；崩溃冒烟实录见验证文档第 3 节 | 待复核 | 2026-10-24 |
| P0-10 | `src/xhs_discovery/storage/worker.py`、`src/xhs_discovery/paths.py`、`tests/test_storage_worker.py` | `pytest tests/test_storage_worker.py -o addopts="" -q` → **33 passed in 7.83s**（连续复跑 6.74/7.55/8.36/7.83s 无抖动）；返工后所有权只认 `owner_token`（实例随机 16 字节令牌），pid 仅做存活探测且探测为三态 `live/dead/unknown`——`unknown` 与锁内容 `invalid` 都**不**当作可接管；**19:57 返工：所有权变更由"持有到进程退出"的 OS 字节区间互斥保护（每条锁配 `*.guard`，`msvcrt.locking` / `fcntl.flock`，该文件只创建永不删除），`force=True` 只能绕过 pid/租约推断、绕不过互斥**；心跳除 token 外还要求本实例仍持互斥，acquire 中途失败会撤回已写记录并交还全部互斥；锁记录用"临时文件 + `os.link`（回退 `O_CREAT\|O_EXCL`）"原子创建；跨 run 另有 `data/worker-global.lock`（同样配 guard）；心跳/释放带 `WHERE owner_token=?` fencing。**真实多进程证据**（`subprocess.Popen` 起独立解释器，stdin 作释放信号）：`test_a_real_second_process_cannot_take_a_live_lock`、`test_two_concurrent_processes_contend_and_exactly_one_wins`（3 进程同一 `GO` 屏障同抢，恰好 1 个赢家且锁/租约 token 一致）、`test_two_processes_both_seeing_a_dead_lock_still_admit_exactly_one`（**19:57 指定场景**：盘上先留一把 pid 已死的遗留锁，两个子进程各自回报 `saw="dead"` 后同时 `acquire`，恰好 1 个赢家且败者未删未覆盖胜者记录，两进程都退出后新 `OsGuard` 立刻抢得到＝OS 已自动释放）、`test_a_second_process_cannot_force_past_a_live_guard`（真实子进程持锁时 `acquire(force=True)` 仍被拒，`forced_takeover` 0 条、记录 token 未变）、`test_a_killed_process_leaves_a_dead_lock_that_may_be_taken_over`（`proc.kill()` 后遗留锁判 `dead` 才接管并写 `stale_lock_removed`）、`test_a_live_guard_blocks_even_when_the_record_still_looks_dead`（确定性：记录写着死 pid 但互斥被别方占 → 拒绝，记录一字未动、`worker_leases` 0 行、全局记录已撤回）；同进程形态另有 `test_the_same_pid_is_not_the_same_worker_instance`（令牌不同即拒）、`test_force_may_only_take_a_record_that_no_guard_protects`（无互斥保护的遗留记录才允许 force，并写 `forced_takeover`+`lease_taken_over`）、`test_takeover_leaves_the_old_instance_unable_to_touch_lock_or_lease`（被接管后旧实例写不回锁与租约）；`extend` 经账本只追加并递增 `budget_revision`、跨 run 拒绝，控制请求按序只应用一次；不承诺外部访问 exactly-once | 待复核 | 2026-10-24 |
| P0-06～10 汇总 | `docs/verification/2026-10-24-P0-B.md` | 返工后复跑：`.venv/Scripts/python.exe -m pytest -o addopts=""` → **332 passed in 24.77s**（同轮重复运行另得 26.13 / 27.12 / 28.84s；P0-A 177 + P0-B 155）；`.venv/Scripts/python.exe -m pip check` → No broken requirements found；11 个存储/契约模块导入 OK（Python 3.15.0）；`--version` → 0，`run` → 70 并指名 P0-C。17:23/17:28/18:23/19:57 四项的复测实录（迁移半途失败、外层批次回滚、迁移拒绝未提交事务、预留归属报错原文、配额精度、extend 重放/回滚、恢复补记）与真实多进程、OS 互斥证据表见验证文档第 2.5 节；20:57 C-07（写账入口归属核对与冲突重放拒绝）的逐条报错原文与"拒绝前后快照逐字相同"见第 2.6 节 | 待复核 | 2026-10-24 |
| P0-11 | `src/xhs_discovery/sources/fixture.py`、`tests/fixtures/shanghai_zhongkao.json`（dataset v2）、`tests/test_source_fixture.py` | `pytest tests/test_source_fixture.py -o addopts=""` → **24 passed in 0.32s**；数据集显式 `synthetic`、名称与文件名不符/文件缺失都报错并给路径、能力不支持时 fail fast；`test_three_branches_three_levels_multi_parent_and_cycle_exist` 直接在数据集层面证明三分支/三层/多父/环路存在；同一逻辑键（run+keyword+operation_type+revisit_seq+attempt）会话身份稳定、`bind` 前取会话被拒；unknown 词返回 completed+空而非异常；search limit 裁卡片不改 rank 且 limit<1 被拒；note 引用必须有效 | 待复核 | 2026-10-25 |
| P0-12 | 同上（异常场景集） | 同一文件 24 例中的场景断言：空结果=completed 不是 failed；可重试 `PAGE_TIMEOUT` 后第 2 次成功（按 attempt 分派）；`AUTH_REQUIRED` 不可重试且需人工；`skipped` 显式且 items 为空（本轮补齐：数据集声明的 exit_code/error_code 还原成 `ErrorInfo`，`WORD_SKIPPED`/budget/不可重试，见 DATA_MODEL 9.1 #27）；partial 必须点名失败词；正文"确实为空"与"不可访问/不支持"是两种事实；作者目录未登记与显式声明空列表区分；地区/年份/否定各占独立场景词；营销帖在场以便来源类型判定 | 待复核 | 2026-10-25 |
| P0-13 | `src/xhs_discovery/text/normalize.py`、`text/intent.py`、`text/similarity.py`、`tests/test_text_normalize.py` | `pytest tests/test_text_normalize.py -o addopts=""` → **16 passed in 0.04s**；只做 NFKC+空白归一、幂等稳定、`fold` 单独可用于原文比较；地域/年份/年级/数字/否定词**保留在原文里**只做抽取不改写；结构属性缺失报告为缺失而非通配；歧义种子（如"上海"）只打标记不改写、声明本身也走归一；地名词表最长优先子串匹配；`as_keyword_text` 同时保留 raw/normalized | 待复核 | 2026-10-25 |
| P0-14 | `discovery/graph.py`、`discovery/ingest.py`、`tests/test_discovery_graph.py` | `pytest tests/test_discovery_graph.py -o addopts=""` → **15 passed in 1.80s**；observed 子词建节点+边并保留推荐 rank、会话与观察时间；多父保留每条边但只有首次调度归属（`run_keywords.parent_keyword_id`/`branch_id`/`depth` 稳定）；同父重复观察加边不加节点；自观察记观察不记自环；同 attempt 重放不产生第二身份；generated 边永不充当平台共识；环路/超深路径写 `graph_path_truncated` 事件而不是伪装成无环；菱形路径全枚举；`describe` 汇总别名/父路径/观察且拒绝跨 run 关键词；ingest 拒绝未登记关键词与错操作；失败观察仍落一行 0 items | 待复核 | 2026-10-25 |
| P0-15 | `discovery/operators.py`、`tests/test_discovery_operators.py` | `pytest tests/test_discovery_operators.py -o addopts=""` → **16 passed in 1.67s**；四类算子各有门槛：`suggest_vertical` 只取高 rank 且跳过自身/已穷尽子词、单算子有上限；`phrase_fallback` 仅在无联想可用时触发且不留半句不重出现见文本；`content_lateral` 必须有观察到的标题与领域词且不重复锚词内已有词；`problem_probe` 必须有来源证据；`expand` 固定算子顺序、批内去重、绝不提议父词自己、未知算子名报错、无模型无随机可复现；`register_candidates` 把 generated 边与 observed 边分开且不抢已有归属。**本轮驱动主循环发现两处衔接缺陷并修复**（seen 含本批新词导致零入队、调用点传错形状），见 DATA_MODEL 9.1 #24/#25 | 待复核 | 2026-10-25 |
| P0-16 | `discovery/priority.py`、`tests/test_discovery_priority.py` | `pytest tests/test_discovery_priority.py -o addopts=""` → **20 passed in 0.12s**；R/N/I/E/C/U 六分量齐全且各自带原值/归一值/权重/可用性说明；未采样用 E 中性先验并**标注是先验不是证据**、采样后用可定位 quote 计数并饱和；generated 无共识记 missing 而不是 0，observed 未返回给不同理由；父共识取对数缩放封顶；分数可由落库的 breakdown 重算、缺失分量重归一而非稀释；无已探索词时 N 记"未定义"不是满分新颖；无锚词时 R 记缺失；单候选批次标 `provisional`；`model_version` 绑定批次语料；同输入同分数；权重逐字取自配置。**本轮修复**：`known_bounds()` 取代 `len(anchors)+len(explored)`（去重语料越界 `IndexError`），见 9.1 #22 | 待复核 | 2026-10-25 |
| P0-17 | `discovery/scheduler.py`、`tests/test_scheduler.py` | `pytest tests/test_scheduler.py -o addopts=""` → **20 passed in 1.90s**；槽位比例来自配置、窗口按桶交错而非成批、计划确定性且随配置缩短、窗口取完回绕；零尝试分支 UCB 为无穷并映射到单位区间；分支探索槽优先覆盖新分支并记录剩余验证份额；高分槽跨分支 Best-first、同分按 frontier id 决定；随机槽用带种子的 RNG 并记录 `selection_probability`、优先未评分候选；空桶回流说明借了哪个桶；校准行留在队列可见但不被主动探索取走；分支统计按 run 与分支隔离；奖励只更新被探索分支；安静奖励累积 `stale_streak` | 待复核 | 2026-10-25 |
| P0-18 | `discovery/merge.py`、`discovery/extract.py`、`discovery/cost.py`、`tests/test_problem_merge.py` | `pytest tests/test_problem_merge.py -o addopts=""` → **26 passed in 5.90s**；求助式标题生成可验证簇且 quote 在原文可定位（**本轮修复 #26**：切分改在原文上做，NFKC 把全角逗号折成半角导致 `Evidence.locate()` 整槽崩掉）；非求助来源只算观察不升级；有意图无求助线索不计数；否定极性与年份作为属性保留；同记录重放零写入；重复观察并入既有簇、同标题两帖归一需求；违反约束的整批回滚；属性冲突拆新簇且拆分键确定、第二次冲突复用同簇；缺失属性不算冲突；联想意图候选只登记不验证不计数、重放不重复、不与显式问题相撞；`merge` 事件写出账；奖励只数新簇、簇必须有可定位证据、suggest 操作不得认领内容簇、失败操作零奖励；簇按 run 隔离。`cost.OperationCost.normalized=max(0.25, attempts+0.05*posts)` 使重试真的摊薄奖励 | 待复核 | 2026-10-25 |
| P0-19 | `storage/ledger.py`（P0-08 已复核）、`discovery/engine.py`、`tests/test_engine_worker.py`、`tests/test_stopping.py` | 分项预算/采样优先级/有限重试在引擎侧的实测：`test_slot_books_every_action_in_the_documented_order`（预留→访问→落盘→入库→结算逐笔可追，帖子配额行 `external_access=0`）、`test_settlement_carries_the_reservation_it_reserved`（结算显式带回自己那笔预留，跑完 0 幽灵）、`test_retry_is_a_new_attempt_a_new_reservation_and_a_new_settlement`（attempt [1,2]、两次 `external_access=1`、`retry_backoff` 带 planned_seconds、奖励 `0.2*2/2.0` 减半）、`test_retry_budget_of_zero_leaves_one_charged_attempt`、`test_attempt_cap_reuses_the_same_operation_row`（超上限记 skipped 且不再访问不扣额）、`test_low_value_tier_skips_content_without_spending_search_budget`、`test_post_quota_cap_is_visible_rather_than_silently_exceeded`；`test_stopping.py` 侧另有单项预算只挡该动作、校准预算独立、运行时长报告不花动作、超支在写账之前被拒、body=0 无法预留 | 待复核 | 2026-10-25 |
| P0-20 | `discovery/stopping.py`、`tests/test_stopping.py` | `pytest tests/test_stopping.py -o addopts=""` → **36 passed in 5.01s**；无观察时重复率无定义、失败观察不算成功、窗口只看最近 N 次、**校准观察不进收敛窗口**、新簇只计当前分析版本、无正文不算内容验证、零内容验证阻断收敛；单分支停滞不误停全局、停滞按连挂暂缓一次、空队列不算停滞；全部动作被挡且队空才是 `budget_exhausted`、有额度队空才是 `queue_exhausted`、有活没额度是 `paused/fault`；收敛必须全部条件齐备且缺一个即破；错误类别映射暂停理由、cancelled 走终态、budget 类不得伪装成收敛；信号可序列化进报告；重试与帖子抬高归一成本；采样档位按配置阈值。**C-09 后口径**：内容验证量按 run 计（同时核对 `body_observations.run_id` 与 `operations.run_id`，来源 NULL 不自洽按 0），`tests/test_stopping.py::content_validation` 相应改为先 `start_attempt` 再带 `operation_id/attempt_id` 入库 → **36 passed** | 待复核 | 2026-10-26 |
| P0-21 | `discovery/calibration.py`、`tests/test_calibration.py` | `pytest tests/test_calibration.py -o addopts=""` → **18 passed in 1.56s**；按种子从候选池抽出稳定子集、候选不足时夹紧、种子决定抽到谁且可复放；**冻结池不吸收后续新发现**、重启后从事件重读整池、payload 即整池；缺预算或缺候选直接拒绝冻结；只有 completed 才算 sampled，主动探索的观察不给成员记 sampled；待办按池顺序推进且做完即停；校准预算独立、不得借用探索预算；池成员不混进 frontier；覆盖率只描述池、从未触碰的池报 0 份额；两类观察不混算；irrelevant 候选不进新池 | 待复核 | 2026-10-25 |
| P0-11～21 汇总 | `discovery/engine.py`、`tests/test_engine_worker.py`（30 例）、`docs/verification/2026-10-24-P0-C.md` | `pytest tests/test_engine_worker.py -o addopts=""` → **30 passed**（两次运行 7.63s / 9.54s）；`pytest -o addopts=""`（全套）→ **554 passed in 37.30s**，退出码 0（既有 332 例一条未删）；收工前（2026-10-26 11:25）复跑 → **554 passed in 63.75s**，退出码 0；八槽实录：三桶各轮到（high_score 4 / branch_exploration 3 / random_novelty 1，含第 0 槽冷启动"空桶回流"）、`suggest 8/24 search 4/4 posts 4/20`、0 幽灵预留、12 attempt/16 账行/12 产物/31 发现边/depth 0..3、终止原因 `max_slots` 而非收敛；暂停在安全点生效且账不回滚、resume 继续计数、`extend(+2)` 后 `limit=3.0 used=2.0` 且只 1 条 `budget_revisions`、cancel→`completed/cancelled`、认证→`paused/auth_required` 且 `outcome=None`、崩溃 worker 释放两把锁并交还下一任（重做走新 `revisit_seq`，旧 attempt 不被改写）、无租约实例零访问零预留、被接管后旧实例抛 `WorkerAlreadyRunning`、重启读回额度并为崩溃窗口按次补记不退款、跨 run Ledger 与 run_id 复用均被拒、两套库轨迹逐字相同；子进程导入 21 个模块 `heavy=[]`（不拉 jieba/sklearn/streamlit/torch）；`config-check` → VALID hash `c747a82a…`；`run` 仍退出 **70**（CLI 接线属 P0-E，未做假 stub） | 待复核 | 2026-10-25 |
| C-09 返工 / P0-09·P0-13·P0-20 | `storage/migrations.py`（v3）、`storage/repository.py`、`discovery/ingest.py`、`discovery/engine.py`、`discovery/stopping.py`、`tests/test_body_provenance.py`（10 例）、`tests/test_storage_migrations.py`（25 例）、`docs/DATA_MODEL.md` 第 3/6/7/9.1 节 | `.venv/Scripts/python.exe -m pytest tests/test_body_provenance.py -o addopts="" -q` → **10 passed in 2.57s**；`.venv/Scripts/python.exe -m pytest tests/test_body_provenance.py tests/test_stopping.py tests/test_engine_worker.py tests/test_storage_migrations.py tests/test_transaction_atomicity.py tests/test_storage_artifacts.py -o addopts="" -q` → **133 passed in 25.26s**，退出码 0；`.venv/Scripts/python.exe -m pytest -o addopts="" -q`（全套）→ **565 passed in 81.17s**，退出码 0；文档同步后复跑 → **565 passed in 40.82s**，退出码 0（554 既有 + 11 新增，未删未跳过）；`pip check` → No broken requirements found；`xhs-discovery config-check` → VALID hash c747a82a…（0）、`doctor` → 0、`contracts` → valid=completed rejected=3/3（0）、`run --mode fixture` → 仍 **70**（P0-E）。行为证据：Codex 指定链路实测 B **0**（只有 A 的 completed 正文）→ **0**（B 正文失败）→ **1**（B 正文成功），A 恒为 1；同帖·同 `captured_at`·同 `session_id` 的两个 run 各写一行（旧 `(post_id, captured_at)` 键必失败）；内容版本 `id` 跨 run 相同而观察 `id` 不同；同 attempt 重放 `created=False` 且行数不增，绕过入库层直写同一 `(operation_id, attempt_id, session_id)` 触发 `IntegrityError`；四类归属错位（跨 run / 错类型 / attempt 不属于该操作 / 空 session）全部拒绝且观察行快照逐字段不变、`budget_ledger` 仍 0 行；v2 老库的旧形状行 rollback→直插→`apply_migrations` 后原样保留、来源 NULL、计数 0，本 run 自己重取才变 1；行 `run_id` 与 `operations.run_id` 不自洽的行两边都按 0 计；收敛反例——8 次成功观察+停滞窗口+队列清空但缺自己正文的 B，`convergence_failures` 命中"内容验证"、`evaluate()` 不返回 `converged`，B 自取正文后同一配方才 `completed/converged`；v3 迁移用例钉住"不新增表 + 四列可空默认 NULL + `idx_body_observation_attempt` + FK 指向 runs/operations/operation_attempts"。schema 版本断言随版本真实推进更新（`(1,2)`→`(1,2,3)`、rollback 元组、`HEAD_VERSION`） | FIXED，待复核 | 2026-10-26 |
|  C-10 返工 / P0-19·P0-22 衔接（`discovery/engine.py::_execute`）  |  `discovery/engine.py`（三阶段收口 `_settle_access`/`_close_failed_attempt`/`FAILURE_PHASE_CATEGORIES`）、`sources/base.py` + `sources/__init__.py`（`AccessNotStarted`）、`sources/fixture.py`（两处证明未发起改抛该异常）、`tests/test_engine_worker.py`（新增 9 例 + 改写崩溃用例）、`docs/DATA_MODEL.md` 第 2/6/9.1 节  |  `.venv/Scripts/python.exe -m pytest tests/test_engine_worker.py -o addopts="" -q` → **39 passed in 16.08s**；`… tests/test_storage_ledger.py tests/test_storage_artifacts.py tests/test_storage_worker.py tests/test_source_contracts.py tests/test_source_fixture.py -o addopts="" -q` → **185 passed in 11.47s**；`… tests/test_body_provenance.py tests/test_stopping.py tests/test_storage_migrations.py -o addopts="" -q` → **71 passed in 5.16s**；`… -q -k "charged or refund or not_accessed or overspend or explode"` → **10 passed, 29 deselected in 3.05s**；全套 `pytest -o addopts="" -q` → **574 passed in 60.79s**，退出码 **0**；`.venv/Scripts/xhs-discovery.exe config-check --config config/offline-demo.yaml` → `VALID … hash=013579ea…`，退出码 0；`… run --mode fixture --seed 上海中考 --config config/offline-demo.yaml` → 退出码 **70**（P0-E）；`pip check` → `No broken requirements found.`   六项验收逐条见报告第 9.3 节表：写盘失败/入库失败/源中途抛异常 → `used=1.0` 且该 attempt 恰好一笔 `external_access=1`、预留被消费、`access_phase` 与 `category` 对得上；有证据未发起（`not_started` 与 `AccessNotStarted` 两形态）→ `used=0.0`、零账行、预留 `consumed_at` 已写、满额还可用；结算也失败 → 原始 `OSError` 不被掩盖、`billing_error` 留档、重启补记 1 笔（`released_reservations` 命中）；额度只剩 1 → 失败的访问花掉最后 1 单位，下一次在预留阶段 `BUDGET_EXHAUSTED` 显式 skipped，全库仅 1 行外部访问账。真实落盘失败用例额外核对 `.tmp-*` 已撤回、目标 `.json` 不存在（P0-09 原子性未被计费口径破坏）  | FIXED，待复核（REVIEW） | 2026-10-26 |
| P0-22 | `src/xhs_discovery/analysis/lexicon.py`、`analysis/phrase_mining.py`、`analysis/store.py`、`tests/test_analysis_phrases.py`（23 例） | `.venv/Scripts/python.exe -m pytest tests/test_analysis_phrases.py -o addopts="" -q` → **23 passed in 2.93s**；词典贪心最长匹配且**从不改写原文**、词典外逐字符保留（不猜词）、数字/拉丁/全角原样、只删空白、空词典直接拒；`load_lexicon` 真的读配置声明的 `domain_terms_path`；文档频次按**文档**而非重复次数计、词典项内部的片段不再被发现、跨项边界仍算候选；`word_score` 是文档占比不是权重；PMI 随自身尺度上报、**抬高阈值时逐个降级并留原因**而不是静默变少、单文档语料拒绝算 PMI、空语料报 `corpus_below_min_docs` 而非"零个短语"；阈值全部来自 `settings.nlp`、不自洽或魔数阈值被拒、文档顺序不影响批次结果；`record_phrase_run` 写行 + 解释性事件、同批次重放零第二行、新 `analysis_version` 只加自己的行不动历史 | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-23 | `analysis/extraction.py`、`analysis/corpus.py`、`text/intent.py`（复用）、`tests/test_analysis_extraction.py`（17 例） | `pytest tests/test_analysis_extraction.py -o addopts="" -q` → **17 passed in 3.23s**；语料层只从库里读：标题走"卡片可见版本"规则、正文只认领**本 run 有归属的**观察、联想用观察到的版本原文与 rank、`CorpusUnit` 携带完整来源而不带 ingest 对象、按 run 隔离且确定性；抽取保留八类意图属性且**缺就不编**（`test_extraction_keeps_the_eight_intent_attributes_and_never_invents_them`）；营销/否定/分享标题保留 quote 但不算有效求助；来源类型五分类（求助/分享/机构/营销/未知）；quote 在容器内可定位且保住全角标点；一篇正文可出多问但不重复计数；无问题的文档显式上报而非静默丢弃；联想单元永不变成帖子问题；`cap<=0` 直接拒绝而不是产出空跑 | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-24 | `analysis/store.py::insert_evidence`、`analysis/extraction.py`、`tests/test_analysis_extraction.py`、`tests/test_analysis_export.py::test_evidence_csv_keeps_a_locatable_quote_and_full_provenance` | 同文件 17 例中的证据入库断言：`test_evidence_and_problem_rows_keep_versions_and_operation_refs`（内容版本/联想版本 id、quote 与字符区间、`operations`/`operation_attempts`/`session_id`、关键词与帖子身份齐全）；`test_a_quote_that_no_longer_matches_its_version_row_is_refused`（写库前用**库里的版本行原文**复核定位，定位不上拒绝而非截断）；`test_replay_writes_no_second_row_and_reuses_ids`（同自然键重放复用原 id）；`test_events_record_the_batch_and_every_no_problem_document`；跨层另有 `test_analysis_pipeline.py::test_a_hand_edited_quote_is_quarantined_not_silently_repaired`（手改 quote 只落 `orphan_evidence` 审计、坏行不再被需求引用、重跑按原文重新落正确证据） | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-25 | `analysis/clustering.py`、`analysis/store.py`、`tests/test_analysis_clustering.py`（14 例） | `pytest tests/test_analysis_clustering.py -o addopts="" -q` → **14 passed in 2.73s**；无可定位证据的问题永不成为需求；重复内容归并为一条需求但两条 quote 都留；**属性冲突拆簇**（`#conflict[...]` 另一行）而非自动合并；否定自成一簇、缺失属性记进 `attribute_gaps` 不当通配；每条有效需求至少一条证据；`stable_key` 表达"归并结论"而非帖子身份，只在 `(run_id, analysis_version)` 内唯一；**撤销＝`superseded_at` + `auto_merged=0`**，原句/证据/需求行全留、重新归并复用同一行；新分析版本写自己的行不动历史；批次事件与每条跳过理由都落库 | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-26 | `analysis/clustering.py`（`statement_kind` 四分区）、`analysis/graph.py`、`analysis/export.py`、`tests/test_analysis_clustering.py` + `tests/test_analysis_graph.py` + `tests/test_analysis_export.py` | 四类陈述分区且口径互不越界：`test_inferred_need_is_a_conservative_template_reusing_only_known_attributes`（推断需求只复用已知属性）、`test_inference_is_skipped_when_the_object_is_missing`、`test_solution_idea_needs_both_an_artifact_and_automation_and_is_never_verified`、`test_a_tool_mentioned_by_an_ad_is_not_a_user_solution_idea`（广告里的"自动填报系统"连方案构想都不登记）、`test_suggestion_text_becomes_an_intent_candidate_not_a_post_fact`；图上 `test_a_suggestion_candidate_reaches_a_suggestion_version_and_never_a_post`；导出 `test_a_suggestion_never_masquerades_as_a_post_fact` + `test_analysis_pipeline.py` 断言 needs.csv 无 `search_intent_candidate` 行、联想证据字段只能是 title/body | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-27 | `analysis/scoring.py`、`analysis/store.py`、schema v4（`post_queries.published_at_text/likes_text`、`content_families`+`content_family_members`）、`tests/test_analysis_scoring.py`（20 例）、`tests/test_analysis_facts.py`（9 例） | `pytest tests/test_analysis_scoring.py -o addopts="" -q` → **20 passed in 1.60s**；`pytest tests/test_analysis_facts.py -o addopts="" -q` → **9 passed in 1.17s**；S/Q/A/F 四分量带权重与原值/归一值；对数缩放到锚点饱和；无年份或含糊词的可见文本不猜日期；**解析不出的日期 → F 缺失并记 `demand_score_unknown_component`，不当 0**，而"解析得出但已过时"才给真实 0；作者引用缺失不把每帖算成独立来源；**点赞文本只是页面文本、永不作为需求信号**；无页面位置则位置分量保持 unknown；重复内容 Q 只算一次但两条 quote 都留、正文与标题算两份内容；小样本整批标 `provisional` 并保留原始计数；方案构想与意图候选永不参与评分；失去证据的需求不给 0 分而是无分；重评幂等、重放不写第二个家族；只碰自己的 `analysis_version`、只读本批次证据；v4 卡片可见字段落在**查询行**而非全局帖子行、页面文本逐字存回、重放读回同样事实 | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-28 | `analysis/graph.py`、`tests/test_analysis_graph.py`（15 例） | `pytest tests/test_analysis_graph.py -o addopts="" -q` → **15 passed in 2.14s**；主题→问题→需求→证据→帖子全链可追、节点 id 带类型前缀使整型 id 无法相撞；推断需求在图上暴露推导来源；营销证据是"未采纳"而非"孤儿"；generated 变体永不成为 `suggests` 边；发现图节点带各自问题数；**展示中的需求若无任何引用直接拒绝**；别批次的证据行不是逃生通道；位置移动的 quote 报 `evidence_quote_broken`；无人认领的证据标 `orphan_evidence`；撤销的成员退出图但留在库里；每个分析版本有自己的图；从行重建确定性；`build_run_graph` 同时返回两视图与完整性报告 | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-29 | `analysis/export.py`、`tests/test_analysis_export.py`（29 例） | `pytest tests/test_analysis_export.py -o addopts="" -q` → **29 passed**；五类产物（`keywords.csv`/`needs.csv`/`evidence.csv`/`graph.graphml`/`report.json`）内容寻址落 `runs/<run>/exports/<analysis_version>/` 并登记 `artifacts`；report 最后渲染且内嵌另四份摘要；表头与文档列一致、行确定；needs 排行榜只显示"有分数且有证据"的行，**坏引用整行剔除**、0 分可展示而 NULL 分不可；`= + - @ 制表符 回车` 前置撇号且反读逐格相等、公式样标题原样往返；GraphML 节点 id 是合法 XML name、`ref` 保持精确身份、复合与类型漂移值统一 JSON 化成声明的 string、非法 XML 字符被清除、每属性单一 `attr.type`；report 分列版本五件套/config/budget/limitations 与"快照不全"caveat，真实配置快照下预算 limit 取自配置；反读校验捕获篡改、库内事后变动、目录多余文件四类；拒绝覆盖已登记文件；同版本重导出字节幂等、新分析版本写自己的目录；导出不碰初步行与奖励；跨 run 不泄漏；`report_separates_export_bookkeeping_from_run_facts`（`file_written` 之类自身事件不算 run 事实） | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |
| P0-22～29 汇总 | `analysis/pipeline.py`、`tests/test_analysis_pipeline.py`（15 例）、`storage/migrations.py`（v4/v5）、`docs/DATA_MODEL.md` 第 9.3～9.5 节 #33～#47、`docs/verification/2026-10-26-P0-D.md` | `.venv/Scripts/python.exe -m pytest tests/test_analysis_pipeline.py -o addopts="" -q` → **15 passed in 5.69s，退出码 0**；8 个专项文件合并 → **142 passed in 17.73s**；相邻回归（migrations/repository/ledger/atomicity/evidence 契约/text 规范化/discovery 图/在线归并/正文归属/CLI）→ **203 passed in 17.67s，退出码 0**；全套 `pytest -o addopts="" -q` → **719 passed in 55.69s，退出码 0**（= 基线 574 + 新增 145，一条未删、未 skip；转 REVIEW 后收工前复跑 → **719 passed in 70.04s，退出码 0**）；`pip check` → No broken requirements found；`.venv/Scripts/xhs-discovery.exe config-check --config config/offline-demo.yaml` → VALID hash `da8ad12b6aad2357…`（0），`export --run run-a` → **70**、`run --mode fixture --seed 上海中考 --config config/offline-demo.yaml` → **70**（命令行接线属 P0-E，owner 文案已注明"P0-E（五类产物库已由 P0-D 交付）"）；跨层闭环 `test_a_real_fixture_run_analyzes_into_displayable_needs`：真实 harness 采 8 槽 → `run_analysis()` → needs.csv 有行、反读通过、`budget_ledger` 行数不变、全部证据 `synthetic=1`；不改写历史由三例钉住（拒绝 `p0-preliminary.1`、拒绝在线 `EXTRACTOR_VERSION`、`branch_stats` 与初步行逐字段对照且库内零在线版本行）；同批次 5 类事件共用一个时间戳、重放须显式 `snapshot` 否则 `ExportConflict` | DONE，独立报告见2026-10-27-P0-D-CODEX-REVIEW | 2026-10-26 |

后续每个 DONE 项都要追加证据；日志推荐放 `docs/verification/`，按日期/任务 ID 命名，记录代码版本或内容哈希。不得把计划中的示例命令填为实际执行结果。

Codex 复核结论（2026-10-24 13:23）：上述 P0-01～05 的“待复核”为实现者交接时状态，现已全部通过。独立报告 `docs/verification/2026-10-24-P0-A-CODEX-REVIEW.md` 逐项映射证据；复跑 177 passed in 2.37s，pip check 无冲突，六个核心依赖显式导入通过。原始产物真实路径 resolve 校验由 P0-09 接入前补齐，不宣称现有词法校验覆盖符号链接。

### 缺陷/阻塞记录

| 编号 | 影响任务 | 现象 | 负责人 | 恢复条件/下一步 | 状态 |
|---|---|---|---|---|---|
| C-01 | Qoder 自动下达/交接 | 原UI点击缺少geometry，无法可靠发送 | Codex | 2026-10-27已改为Qoder CLI：真实任务回执摘要一致、进程实际退出、交付与独立返工均有证据；不声称UI故障已修复 | CLI交接已解决；旧UI监测PAUSED |
| C-11 | P0-25/26/28 | 同canonical缺失属性被填充，派生文字碰撞丢证据 | Qoder | r1/r2修复；Codex独立727 passed in217.36s，三属性×6排列及原派生反例通过；见2026-10-27-C11-R2-CODEX-REVIEW.md | CLOSED（2026-10-27） |
| C-12 | P0-27/29 | 相对日期按分析时间漂移 | Qoder | V6可空观察锚点、解析/窗口分离；独立49项专项及739项全套通过，原反例日期稳定；见同日C12-CODEX-REVIEW | CLOSED（2026-10-27） |
| C-13 | P0-27 | 同事实两会话观察，证据去重后S.sessions也只计1 | Qoder | C-12后复测仍成立，P0-D-C13-r1已启动并确认回执 | CLOSED（2026-10-27；见P0-D独立报告） |
| C-14 | P0-27 | 同帖标题/正文两个家族令Q.raw=2，而distinct_posts=1 | Qoder | 与C-13同包修评分聚合，不删除原文/家族审计 | CLOSED（2026-10-27；见P0-D独立报告） |
| C-15 | P0-11/24/27 | 同帖标题A→B→A，第三次采集A但按版本首次时间重放成B | Qoder | 真库复现见同日C15-CODEX-REPRO；当前C13退出后复测，仍失败则串行修显式观察→标题版本引用 | CLOSED（2026-10-27；见P0-D独立报告） |
| C-16 | P0-33 | 当前Streamlit选择对象按图层分组，UI误按列表解析；真实点图后仍无详情；布局过小及中文字缺失 | Qoder | 独立浏览器/原探针见2026-10-28-P0-E-UI-CODEX-REOPEN；UI-r2返工 | CLOSED（2026-10-28；见UI-CODEX-REVIEW） |
| C-17 | P0-33 | 同总权重但可用S/Q/A/F分量不同，仍混为同一精确名次组 | Qoder | UI-r2原探针及独立35项通过，按分量/权重分组 | CLOSED（2026-10-28） |
| C-18 | P0-34 | 瞬时按钮嵌套确认按钮导致追加/恢复无操作；run_id永久防抖又挡合法恢复 | Qoder | 真实浏览器与Codex AppTest+子进程探针确认0→0失败，UI-r3返工 | CLOSED（2026-10-28；见UI-CODEX-REVIEW） |
| C-02 | P0-06/P0-09 | 复核（17:23）：写事务所有权不统一，外层批次内的失败会被内层提交；迁移中途失败留下"半张 schema + 已登记版本"，且在已有未提交事务时运行迁移会顺手提交调用方批次 | Qoder | 统一到 `db.transaction()`（嵌套走 SAVEPOINT）、迁移入口拒绝未提交事务、`complete_statement` 切分语句；复现与回归见 `tests/test_transaction_atomicity.py`（7 例）与迁移测试新增 2 例 | FIXED，待复核（REVIEW） |
| C-03 | P0-08 | 复核（17:28）：预算账本边界不足——预留可被跨 run/换键/跨操作认领或释放、上限来自内存字段导致重启与回滚口径不一、小数/0 单位预留无约束、恢复后未认领预留可被当作未发生 | Qoder | `ledger.py` 重写：归属校验 + 自动认领、`limits` 从 `budget_revisions` 派生、按访问/配额键分别校验单位、headroom 守卫、已 `started_at` 的 attempt 补记不退款；回归见 `tests/test_storage_ledger.py`（27 例）与复测实录第 2.5 节 [4]～[7] | FIXED，待复核（REVIEW） |
| C-04 | P0-10 | 复核（18:23）：接管安全性以 pid 为所有权凭证（pid 复用即失守），存活判定只有二态，且缺少真实多进程证据 | Qoder | `owner_token` 作为唯一所有权凭证 + 心跳/释放 token fencing；三态 `live/dead/unknown` 与锁内容 `invalid`；原子建锁与跨 run 全局锁；`subprocess` 起真实进程抢锁的用例 | FIXED，待复核（REVIEW） |
| C-05 | P0-10 | 复核（19:57）：`_claim_lock` 是 probe→unlink→os.link，两个进程都按旧快照看到同一把 dead 锁时，后到者会 unlink 掉前者刚建好的活锁再建自己的，两边都可能返回成功；此前的空锁竞争测试不等价于这个遗留锁场景 | Qoder | 所有权变更改由"持有到退出的 OS 字节区间互斥"保护（每条锁配 `*.guard`，只创建永不删除，`msvcrt.locking` / `fcntl.flock`）；新增 barrier 双进程用例（两边都回报 `saw="dead"` 后同时 `acquire`）断言恰好一个赢家且败者不删不覆盖胜者文件；另有确定性用例（记录写着死 pid 但互斥被占）与半拿状态撤回 | FIXED，待复核（REVIEW） |
| C-06 | P0-10 | 复核（19:57）：`force=True` 允许在活持有者仍运行时启动第二个实例，仅靠审计事件不能实现单 worker 保障；不得用"人工确认卡死"替代停止证据 | Qoder | `force` 现在只能绕过"pid/租约推断"，**绕不过 OS 互斥**：持有者仍持锁时 `acquire(force=True)` 抛 `WorkerAlreadyRunning`，不写 `forced_takeover`、不改归属记录（真实子进程与同进程双句柄两种形态各有用例） | FIXED，待复核（REVIEW） |
| C-07 | P0-08 | 复核（20:57）：`settle_external` 的无预留路径不核对操作归属——在 run-a 的账本上结算 run-b 的 suggest 操作并声称是 search 会成功（`WRONG_RUN_AND_TYPE_ACCEPTED 1.0`），同时扣错桶、记错类型；`operations.id` 的外键只保证 id 存在，挡不住这种错位；重复导入参数与原账不一致时被 `return None` 静默掩盖 | Qoder | `ledger.py` 新增 `_validated_operation`（run 归属 + 库里实际类型 + 预算键↔类型映射 + `operation_attempts` 存在性）并接入**全部写账入口**（`reserve`、`settle_external`、`_record_access` 恢复补记、`record_cache_hit`、`record_quota`），有/无 `reservation_id` 同口径；新增 `ACCESS_KEY_BY_OPERATION_TYPE` 映射与 `_access_row`/`_check_replay`：一致重放仍幂等返回"已记过"，键/类型/单位任一不符 `ValueError` 并列出冲突项；校验全部在写账之前，7 个新用例用"拒绝前后 ledger·reservations·revisions 三份快照逐字比对"证明无副作用（`tests/test_storage_ledger.py` → 34 passed，实录见验证文档第 2.6 节） | FIXED，待复核（REVIEW） |
| C-08 | P0-09/P0-12/P0-15/P0-16/P0-17/P0-18（含 2 处落在 P0-B 已复核文件） | 自测**驱动真实主循环**（不是单元测试）时暴露 7 处跨模块衔接缺陷，逐条见 `docs/DATA_MODEL.md` 第 9.1 节 #21～#27：①#21 重启补导入去读正被持有的 `worker.lock.guard` → `PermissionError: [Errno 13]`，整条恢复路径断掉（`storage/artifacts.py`，属 P0-B 已复核通过的文件）；②#22 语料含重复文本时 `CharTfidf.cosine` 越界 `IndexError`——边界取自 `len(anchors)+len(explored)` 而非去重后长度（`discovery/priority.py`，亦属 P0-B 已复核文件）；③#23 `frontier.slot_kind` 继承当前槽：种子 `slot_kind=high_score` 且 `score=NULL` 使三个桶都取不到它，run 在第 0 槽就报"没有可调度候选"；候选继承父槽则 8 槽全成 `branch_exploration`，60/25/15 退化成一个桶；④#24 `expand(seen)` 把本批刚生成的候选也算进"已见过" → 6 个子词登记进 `run_keywords` 却零入队，扩散在第 1 层静默断掉且槽位说明写成"四类算子没有产出新候选"；⑤#25 `register_candidates` 收到评分对象而非候选（`AttributeError`），并带把 generated 误标 observed 的风险；⑥#26 `select_quote()` 在 NFKC 归一后的文本上切子句，全角 `，` 折成半角后 quote 不再是原文子串，`Evidence.locate()` 拒绝入库、整槽崩掉——正是"证据必须能在原文定位"要防的事；⑦#27 fixture 的 `skipped` 场景丢掉数据集声明的 `exit_code/error_code`，"被上游限频跳过"与"平台确实没有"在库里长得一样 | Qoder | 七处全部按根因修复、未放宽任何断言：#21 `_is_worker_bookkeeping()` 排除归属记录与 `*.guard` 及临时变体；#22 新增 `PriorityBatch.known_bounds()`；#23 种子与候选一律写 `slot_kind=NULL`（规则已写入 DATA_MODEL 第 2 节）；#24 去重基线改用 `known_texts`；#25 按候选本身登记、`origin` 取候选来源；#26 切分改在原文上做、判定才用归一文本；#27 `_skip_error()` 还原成 `ErrorInfo`（联想与卡片两条路径同口径）。每处由行为测试固化：`test_storage_artifacts.py`、`test_discovery_priority.py`、`test_scheduler.py`/`test_engine_worker.py`、`test_discovery_graph.py`、`test_discovery_operators.py`、`test_problem_merge.py`、`test_source_fixture.py`；全套 554 passed、退出码 0。**#21/#22 落在已复核通过的 P0-B 文件上，请 Codex 回看这两处是否影响原结论** | FIXED，待复核（REVIEW） |
| C-09 | P0-20（连带 P0-09/P0-13 的入库层） | 复核（11:30）：**正文验证跨 run 泄漏**。`discovery/stopping.py::content_validation_count` 只按**全局** `post_id` 连本 run 的 `post_queries`——最小复现：run A 对帖子 7 有 completed 正文、run B 只有帖子 7 的标题卡片，B 计数返回 **1**（应为 0），B 于是借 A 的读取过收敛门槛。核实过程中发现**同源的第二个泄漏**在入库层：`ingest_body` 用 `(post_id, captured_at)` 去重，而 fixture 的 `collected_at` 不含 `run_id`，两个 run 同 `revisit_seq`/`attempt_no` 撞出完全相同的 `captured_at`，B 想补自己的正文观察会被当成"已存在"吞掉，只改计数不成立 | Qoder | 两层一起修：**① schema v3**（`storage/migrations.py::_V3_BODY_PROVENANCE`）给 `body_observations` `ADD COLUMN run_id/operation_id/attempt_id`（各自 `REFERENCES … ON DELETE RESTRICT DEFAULT NULL`）+ `session_id`，并 `CREATE UNIQUE INDEX idx_body_observation_attempt(operation_id, attempt_id, session_id)`、`idx_body_observations_run(run_id, state, post_id)`；不新增表（`_TABLES[3]=()`），down 脚本对称删索引与列。**列可空是有意的**：老库历史行无法确定归属就留 NULL，不计入收敛也不伪造 run。**②** `Repository.require_observation_source()`（与 C-07 `_validated_operation` 同口径）在任何写入前核对操作存在·`operations.run_id` 等于本 run·声称类型等于库里类型·attempt 存在且归属该操作·`session_id` 非空，任一不符 `ValueError` 拒绝而非返回 None；`ingest_body(repo, run_id, result, *, note_id, operation_id, attempt_id)` 改用同幂等键（内容版本仍全局共用，按 run 隔离的是观察）；`engine._collect_bodies` 传入本次 operation/attempt，`engine._existing_observation` 覆盖 `body_observations`（重启后重驱动走缓存命中，不二次访问扣账）；`content_validation_count` 同时核对 `bo.run_id` 与 `op.run_id`，NULL 或两重来源不自洽按 0 计。**测试**：新增 `tests/test_body_provenance.py` 10 例（B 0→0→1 链、同帖同 `captured_at` 同 `session_id` 跨 run 各写各行、内容版本共享而观察不共享、同 attempt 重放不写第二行 + 直写触发 `IntegrityError`、四类归属错位拒绝且观察快照逐字段不变·`budget_ledger` 0 行、v2 老库旧形状行升级后保留且计数 0、run/操作错位的行两边都按 0 计、收敛反例：8 次成功观察+停滞+队列清空但缺自己正文的 B 不得 `converged`）；`test_storage_migrations.py` +1 例钉 v3 列形状与 FK 目标；schema 版本断言因版本真的推进而更新。实跑：专项 **10 passed**、相关 6 文件 **133 passed / 退出码 0**、全套 **565 passed in 81.17s / 退出码 0**（既有 554 例一条未删未跳过）。文档同步 `docs/DATA_MODEL.md` 第 3 节 `body_observations`、第 6 节第 16 条、第 7 节索引、第 9.1 节 #28；实录见 `docs/verification/2026-10-24-P0-C.md` 第 8 节。**P0-C 保持 REVIEW，未进入 P0-D** | FIXED，待复核（REVIEW） |
|  C-10  |  P0-19 预算 × P0-22 主循环（`discovery/engine.py::_execute`）  |  复核（12:34）：**访问成功后本地异常漏计预算**。`_execute` 把 access、write_artifact、attach 包在同一个 try，任何异常一律 `ledger.release(reservation_id)` + `finish_attempt(failed)`，从不 `settle_external`；复核复现（包装 `get_suggestions` 记录返回、patch `engine.write_artifact` 抛 `OSError`）：`source_returned=1、charged=0.0、reservations_open=0、attempt.failed`。第二层危害：`release` 只写 `consumed_at`，恢复补账只看"有 `started_at` 且预留未消费"，被退款的访问重启也补不回来——漏计被永久固化且库里无痕迹  | Qoder |  **FIXED（2026-10-26 13:40，待复核）**：三阶段收口 `not_started/access/local`，只有 `not_started` 或 `access` 阶段的 `AccessNotStarted` 证据可退款，其余照计一笔并在 `error_json` 留 `access_phase/access_charged/billing_error` + 审计事件；结算失败不掩盖原始异常、预留留给恢复补记；`run_guarded` 的 `finally` 放锁不变；新增 `AccessNotStarted` 数据源契约。报告第 9 节；`docs/DATA_MODEL.md` 第 6 节第 17 条、第 9.1 节 #29（另补齐 9.1 漏登记的 #27 行）；复核点名的 `test_run_guarded_releases_both_locks_when_the_source_explodes` 断言由 `used=0.0` 改为 `used=1.0`，锁释放断言全部保留，未放宽预算要求  | FIXED，待复核（REVIEW） |

缺陷分级：阻断=无法启动/数据损坏/预算失控/证据造假；重要=核心流程或准确性错误；一般=非关键功能与体验问题。REVIEW 失败列出关联 ID、复现、预期、实际、修复证据；不得只删勾选而不解释。

### 每轮交接模板

```text
执行范围：任务 ID / 工作包
输入：需求与计划章节、前置任务、相关文件
本轮交付：文件、运行行为、测试和报告
范围边界：当前阶段及用户最新要求
实际完成：ID、文件、命令、结果、限制
待复核：ID、证据路径
阻塞/缺陷：编号、原因、恢复条件
下一步：一个可执行工作包
Checklist 更新：变更 ID 与状态；未验证项不勾选
```

### 交接记录 2026-10-24（Qoder → Codex）

```text
执行范围：P0-A（P0-01～P0-05）
输入：DEVELOPMENT_PLAN.md 第 2/3/5/6 节、PROJECT_SPEC.md、QODER_TASK.md、工具_USAGE.md、B-01～B-03
本轮交付：.venv 隔离环境、pyproject/src/tests/config、Pydantic 契约与错误分类、三份配置、docs/DATA_MODEL.md、README.md、docs/verification/2026-10-24-P0-A.md
范围边界：不访问真实小红书、不读凭据、不下载模型、不调用外部 LLM、不提交推送、保留既有文档；未实现命令一律退出 70 并指名归属工作包
实际完成：`.venv/Scripts/python.exe -m pytest` → 177 passed in 2.24s；doctor/config-check/contracts → 0；非法配置 → 65；run → 70；QODER_GUIDANCE 四条复核意见已实现并各有行为测试
待复核：P0-01～P0-05，证据见 docs/verification/2026-10-24-P0-A.md 第 2～6 节与"完成证据登记"
阻塞/缺陷：C-01 仍未恢复自动发送（不阻塞手动开发）；本轮无新增阻断缺陷
下一步：Codex 复核 P0-A；通过后按依赖顺序进入 P0-B（P0-06～P0-10：migrations/Repository、run 状态机与 frontier、预算预留结算、产物落盘与补导入、单 worker 锁与租约）
Checklist 更新：P0-A 看板 DOING → REVIEW；P0-01～05 复选框保持未勾选（由复核者勾选）
```

### 交接记录 2026-10-24（Qoder → Codex）· P0-B

```text
执行范围：P0-B（P0-06～P0-10）+ 针对 QODER_GUIDANCE 17:23/17:28/18:23/19:57/20:57 五项（C-02～C-07）的返工
输入：docs/verification/2026-10-24-P0-A-CODEX-REVIEW.md、docs/coordination/QODER_GUIDANCE.md、DEVELOPMENT_PLAN.md 第 5/6 节、docs/DATA_MODEL.md
本轮交付：src/xhs_discovery/{relpath,paths}.py、storage/{db,migrations,repository,ledger,artifacts,worker}.py、tests/test_{path_resolution,storage_migrations,storage_repository,storage_ledger,storage_artifacts,storage_worker,transaction_atomicity}.py、docs/DATA_MODEL.md 第 6 节 8～15 条与第 9 节 #1～#20、docs/verification/2026-10-24-P0-B.md
范围边界：未访问真实小红书、未读取账号凭据（含 CLI browser profile）、未下载模型权重、未调用外部 LLM API、未提交或推送仓库、未修改项目外文件、保留全部既有需求文档；worker 主循环与调度仍属 P0-C，未实现命令一律退出 70 不做假 stub；本轮未进入 P0-C
实际完成：`.venv/Scripts/python.exe -m pytest -o addopts=""` → 332 passed in 24.77s（重复运行 26.13 / 27.12 / 28.84s）；`pytest tests/test_storage_ledger.py` → 34 passed；`pip check` → No broken requirements found
C-07 修复要点：`ledger.py` 新增 `_validated_operation`（`operations.run_id` + 库里实际 `operation_type` + 预算键↔类型映射 + `operation_attempts` 存在性）与 `_access_row`/`_check_replay`，接入 `reserve`、`settle_external`、恢复补记 `_record_access`、`record_cache_hit`、`record_quota`，有/无 `reservation_id` 同口径；Codex 原复现现报 `操作 1 属于 run run-b，本账只服务 run-a` 且账本/预留/修订零变化；同一 attempt 换单位或换键的重放报"重放参数与既成事实冲突"，完全一致的重放才返回"已记过"
待复核：P0-06～P0-10（含返工）。复现证据见 docs/verification/2026-10-24-P0-B.md 第 2.5 节（迁移半途失败 / 外层批次回滚 / 迁移拒绝未提交事务 / 预留归属报错原文 / 配额精度 / extend 重放与回滚 / 恢复补记 + 真实多进程与 OS 互斥证据表）与新增第 2.6 节（C-07 逐条拒绝原文与前后快照比对）
阻塞/缺陷：C-02/C-03/C-04/C-05/C-06/C-07 已修复并固化为测试，状态 FIXED 待复核；C-01 自动发送仍未恢复（不阻塞手动开发）；本轮无新增阻断缺陷
下一步：等 Codex 复核 P0-B（P0-B 未通过前不进入 P0-C，也不自行勾选 DONE）；通过后进入 P0-C（P0-11～P0-21：fixture 数据源、标准化与发现图、评分与调度、停止与校准），worker 循环须按"每次重启都重建 Repository/Ledger"实现
Checklist 更新：看板 P0-B 维持 REVIEW（P0-06～10 复选框仍未勾选，由复核者勾选）；缺陷表新增 C-07；证据登记 P0-08 与汇总行改为本轮实际数字（34 / 332）
```

### 交接记录 2026-10-25（Qoder → Codex）· P0-C

```text
执行范围：P0-C（P0-11～P0-21）——离线 fixture 数据源与异常场景、关键词标准化、发现图与四类扩展、R/N/I/E/C/U 可解释评分、Best-first + 分支 UCB + 60/25/15 分配、在线初步归并与奖励、分项预算与有限重试、停止策略与冻结校准池、worker 主循环与暂停/恢复衔接
输入：docs/coordination/QODER_GUIDANCE.md 顶部最新决策（C-07）、docs/verification/2026-10-24-P0-B-CODEX-REVIEW.md、DEVELOPMENT_PLAN.md 第 P0 节 P0.3 验收门槛、PROJECT_CHECKLIST.md、docs/DATA_MODEL.md
本轮交付：sources/fixture.py + tests/fixtures/shanghai_zhongkao.json（dataset v2）、text/{normalize,intent,similarity}.py、discovery/{graph,ingest,operators,priority,scheduler,merge,extract,stopping,calibration,cost,engine}.py、tests/test_{source_fixture,text_normalize,discovery_graph,discovery_operators,discovery_priority,scheduler,problem_merge,stopping,calibration,engine_worker}.py（222 例）、tests/test_environment.py 新增"探索栈不导入模型依赖"用例、docs/DATA_MODEL.md 第 2/6/8 节补齐与第 9.1 节 #21～#27、docs/verification/2026-10-24-P0-C.md
范围边界：未访问真实小红书/真实平台、未读取账号凭据（含 CLI browser profile 的 cookies/密码）、未下载模型权重、未调用外部 LLM API、未提交或推送仓库、未修改项目外文件、既有需求文档全部保留；fixture 证据一律标 synthetic 不伪装真实链接；未降低收敛验收阈值；完整分析、图谱展示与 Streamlit UI 留给 P0-D/P0-E，`export`/`ui`/`run` 等未接线命令仍退 70 不做假 stub；页面文本只当数据，未执行其中指令；无验证码绕过/代理轮换/反检测
实际完成：`.venv/Scripts/python.exe -m pytest tests/test_engine_worker.py -o addopts=""` → 30 passed（两次 7.63s / 9.54s）；`.venv/Scripts/python.exe -m pytest -o addopts=""`（全套）→ **554 passed in 37.30s，退出码 0**，收工前（2026-10-26 11:25）复跑 → **554 passed in 63.75s，退出码 0**（既有 332 例一条未删、未 skip；两轮耗时差来自 P0-10 真实多进程用例随系统负载浮动）；`.venv/Scripts/python.exe -m pip check` → No broken requirements found；`config-check --config config/tests-convergence-tiny.yaml` → VALID hash c747a82a…；子进程导入 21 个模块回报 `heavy=[]`；`xhs-discovery run --mode fixture` 仍退出 70 并指名 P0-E。八槽主循环实录（报告第 3 节）：三桶各轮到 high_score 4 / branch_exploration 3 / random_novelty 1（含第 0 槽冷启动空桶回流）、suggest 8/24、search 4/4、posts 4/20、0 幽灵预留、12 attempt / 16 账行 / 12 产物 / 31 发现边 / depth 0..3、终止原因 `max_slots` 而非收敛、单分支 stale_streak=3 不误停全局；暂停在安全点生效且账不回滚、resume 继续计数、extend(+2) 后 limit=3.0 used=2.0 且仅 1 条 budget_revisions、cancel→completed/cancelled、认证→paused/auth_required 且 outcome=None、崩溃 worker 交还两把锁并由下一任接管（重做走新 revisit_seq、旧 attempt 不改写）、无租约实例零访问零预留、被接管后旧实例抛 WorkerAlreadyRunning、重启按次补记不退款、跨 run Ledger 与 run_id 复用被拒、两套库轨迹逐字相同
待复核：P0-11～P0-21 与 C-08，证据见 docs/verification/2026-10-24-P0-C.md（第 2 节逐文件计数与命令、第 3 节主循环记账实录、第 4 节 P0.3 验收五条对照、第 5 节暂停/恢复/追加/取消/异常与停止反例表、第 6 节七处衔接缺陷、第 7 节已知限制）与"完成证据登记"P0-11～P0-21 逐条及"P0-11～21 汇总"各行
阻塞/缺陷：C-08（七处跨模块衔接缺陷）已修复并固化为测试，FIXED 待复核——其中 #21 `storage/artifacts.py`、#22 `discovery/priority.py` 落在 P0-B 已复核通过的文件上，请复核时一并回看；C-01 自动发送仍未恢复（不阻塞手动开发）；本轮无新增阻断缺陷。已知限制（报告第 7 节）：完整需求图谱导出、评分全量分析与 Streamlit UI 属 P0-D；CLI `run` 接线与端到端一键启动属 P0-E；真实数据源接入属 P1
下一步：等 Codex 复核 P0-C；通过后进入 P0-D（P0-22～P0-28 需求图/评分分解/双图与五类导出），worker 主循环与账本口径沿用本轮实现
Checklist 更新：看板 P0-C → REVIEW（并记 2026-10-26 11:25 收工前全套复跑）；P0-11～P0-21 复选框保持未勾选（由复核者勾选）；缺陷表新增 C-08；完成证据登记新增 P0-11～P0-21 逐条与"P0-11～21 汇总"行；顶部交接段更新为 2026-10-26 11:25（首轮收尾 2026-10-25 01:53 一并注明）；恢复被误删的"P0-06～10 汇总"证据行（内容与 C-07 返工后的 332 passed 版本逐字一致）
```

### 交接记录 2026-10-26（Qoder → Codex）· C-09 返工

```text
执行范围：C-09（P0-20 内容验证计数 + P0-09/P0-13 正文入库层）——为正文观察补齐可审计的 run/operation/attempt 来源；P0-C 重新转 REVIEW，未进入 P0-D
输入：docs/coordination/QODER_GUIDANCE.md 顶部 C-09（2026-10-26 11:30）、docs/verification/2026-10-24-P0-C.md、docs/DATA_MODEL.md 第 3/6/7 节
本轮交付：storage/migrations.py 新增 v3（body_observations 四列来源 + 两个索引 + 对称 down 脚本，不新增表）、storage/repository.py::require_observation_source、discovery/ingest.py::ingest_body 改签名与幂等键、discovery/engine.py（_collect_bodies 传来源、_existing_observation 覆盖 body_observations）、discovery/stopping.py::content_validation_count 按 run 计、tests/test_body_provenance.py（新增 10 例）、tests/test_storage_migrations.py（+1 例 v3 列形状）、tests/test_stopping.py::content_validation 改为先 start_attempt 再带 operation/attempt 入库、docs/DATA_MODEL.md 第 3 节 body_observations 定义 + 第 6 节第 16 条 + 第 7 节索引 + 第 9.1 节 #28、docs/verification/2026-10-24-P0-C.md 新增第 8 节
范围边界：全程离线 synthetic fixture；未访问真实平台、未读凭据、未下载模型权重、未调用外部 LLM/API、未提交或推送仓库、未修改项目外文件、既有需求文档全部保留；未降低收敛验收阈值；未实现命令仍退 70 不做假 stub；本轮未进入 P0-D、未自行勾选任何 DONE
实际完成：`pytest tests/test_body_provenance.py -o addopts="" -q` → 10 passed in 2.57s；六个相关文件（body_provenance/stopping/engine_worker/storage_migrations/transaction_atomicity/storage_artifacts）→ 133 passed in 25.26s，退出码 0；全套 `pytest -o addopts="" -q` → **565 passed in 81.17s，退出码 0**（= 返工前 554 + 专项 10 + v3 迁移 1，既有 554 例一条未删、未 skip）；`pip check` → No broken requirements found；安装版 CLI `xhs-discovery config-check` → VALID hash c747a82a…（退出 0）、`doctor` → 退出 0、`contracts` → valid=completed rejected=3/3（退出 0）、`run --mode fixture` → 仍退出 70 并指名 P0-E
验收对照（Codex C-09 指定项）：① 用真实迁移/Repository/ingest 构造 A 正文成功 → B 只有标题 **0** → B 正文失败 **0** → B 正文成功 **1**，A 恒为 1；② 同帖 + 同 captured_at + 同 session_id 的两个 run 各写各的观察行（旧的 (post_id, captured_at) 去重键在这里必然吞掉 B 的读取，是核实过程中发现的第二处同源泄漏，已一并修）；③ 重复同 attempt 不写第二行，绕过入库层直写同一 (operation_id, attempt_id, session_id) 触发 sqlite3.IntegrityError；④ 迁移兼容：退到 v2 写一条旧形状行再升级，原样保留、来源 NULL、不计入收敛，本 run 自己重取才变 1；⑤ 收敛反例：其余条件全备（8 次成功观察 + 停滞窗口 + 队列清空 + 分支覆盖）但缺自己正文的 B，convergence_failures 命中"内容验证"、evaluate() 不返回 converged，B 自取正文后同一配方才 completed/converged。另附：四类归属错位（跨 run / 错类型 / attempt 不属于该操作 / 空 session）在任何写入之前拒绝，观察行快照逐字段不变且 budget_ledger 仍 0 行；未引用的列不存在——修复前先核实 body_observations 无 operation_id，来源关系由 v3 迁移补齐后才 JOIN
待复核：C-09 与 P0-11～P0-21（含首轮 C-08）。复现证据见 docs/verification/2026-10-24-P0-C.md 第 8 节（8.1 复现与双层根因、8.2 逐文件改动、8.3 命令与结果 + 专项 10 例逐条、8.4 提请复核注意）与"完成证据登记"的"C-09 返工"行、P0-20 行更新
阻塞/缺陷：C-09 FIXED 待复核；C-08（七处衔接缺陷）仍待复核，其中 #21/#22 落在 P0-B 已复核文件上；C-01 自动发送未恢复（不阻塞手动开发）；本轮无新增阻断缺陷。设计取舍提请确认：来源列可空是有意的（老库历史行归属未知宁留 NULL，不伪造 run_id，不计入收敛），唯一性用 CREATE UNIQUE INDEX 达成（ADD COLUMN 附表级 UNIQUE 会整语句失败），普通 UNIQUE 索引把 NULL 视为互异故部分缺失（如空 session_id）由 Python 层 require_observation_source 拒绝
下一步：等 Codex 复核 P0-C 与 C-09；通过后进入 P0-D（P0-22～P0-29 规则分析、证据、双图与五类导出），正文观察的按 run 归属口径沿用本轮实现
Checklist 更新：看板 P0-C DOING → REVIEW（注明 12:10 C-09 返工完成并复测）；缺陷表新增 C-09 行（FIXED 待复核）；完成证据登记新增"C-09 返工"行并更新 P0-20 行的实跑数字与口径；顶部新增 12:10 最新交接段、原 11:25 段改为"上轮交接"；P0-11～P0-21 与 C-08/C-09 的复选框与 DONE 勾选均留给复核者，未自行勾选
```

### 交接记录 2026-10-26（Qoder → Codex）· C-10 返工

```text
执行范围：C-10（P0-19 预算 × P0-22 主循环衔接）——修复"访问已发生后本地异常退款"的漏计预算；P0-C 第三次转 REVIEW，未进入 P0-D
输入：docs/coordination/QODER_GUIDANCE.md 顶部 C-10（2026-10-26 12:34）、docs/verification/2026-10-26-C09-CODEX-REVIEW.md、docs/DATA_MODEL.md 第 2/6 节、storage/ledger.py 预留-结算-恢复三处口径
本轮交付：discovery/engine.py（_execute 三阶段收口 + _settle_access + _close_failed_attempt + FAILURE_PHASE_CATEGORIES，run_guarded 的 finally 放锁路径未动）、sources/base.py 与 sources/__init__.py（新增 AccessNotStarted(ValueError) 契约）、sources/fixture.py（未 bind 取会话、limit<1 两处"证明未发起"改抛该异常）、tests/test_engine_worker.py（新增 9 例故障注入/恢复用例 + assert_one_charge 一致性助手 + 改写崩溃用例的退款假设）、docs/DATA_MODEL.md 第 2 节 operation_attempts 与 run_events、第 6 节新增第 17 条、第 9.1 节补记漏登的 #27 并新增 #29、docs/verification/2026-10-24-P0-C.md 新增第 9 节（原第 9 节交接顺延为第 10 节）
范围边界：全程离线 synthetic fixture；未访问真实平台、未读凭据、未下载模型权重、未调用外部 LLM/API、未提交或推送仓库、未修改项目外文件、既有需求文档全部保留；无 schema 变更（不新增 v4 迁移）；未降低任何预算验收口径；未进入 P0-D、未自行勾选任何 DONE
实际完成：`pytest tests/test_engine_worker.py -o addopts="" -q` → 39 passed in 16.08s（返工前 30 例）；`pytest tests/test_storage_ledger.py tests/test_storage_artifacts.py tests/test_storage_worker.py tests/test_source_contracts.py tests/test_source_fixture.py -o addopts="" -q` → 185 passed in 11.47s；`pytest tests/test_body_provenance.py tests/test_stopping.py tests/test_storage_migrations.py -o addopts="" -q` → 71 passed in 5.16s（C-09 专项未回归）；全套 `pytest -o addopts="" -q` → **574 passed in 60.79s，退出码 0**（= 返工前 565 + 新增 9，一条未删、未 skip）；安装版 CLI `xhs-discovery config-check --config config/offline-demo.yaml` → VALID hash 013579ea…（退出 0）、`run --mode fixture --seed 上海中考 --config …` → 仍退出 70 并指名 P0-E（缺 --seed 时 argparse 退出 2，与 P0-A 口径一致）；`pip check` → No broken requirements found（退出 0）
验收对照（Codex C-10 六项 + 一致性）：① write_artifact 在源正常返回后抛 OSError → used=1.0、该 attempt 恰好一笔 external_access=1（consumed_units=1.0、reason 是计划里那句"一次联想观察"）、预留 consumed_at 已写、ghosts=0、access_phase=local、category=output、观察与 raw_artifacts 均 0 行；另一例走**真实**落盘路径（只打断 .json 的 os.replace）额外核对 .tmp-* 已撤回、目标 .json 不存在，计费口径没有牺牲 P0-09 原子写协议；② attach 入库失败 → 同样照计一笔，raw_artifact_count=1（产物真落了）；③ 源在调用中抛异常（已进入 get_suggestions，calls=1）→ 照计一笔，access_phase=access、category=system，复核点名的 test_run_guarded_releases_both_locks_when_the_source_explodes 断言由 used=0.0 改 1.0，锁/租约释放、ghosts=0、operation=failed、历史 attempt 不被改写四组断言原样保留；④ 有证据证明未发起 → used=0.0、该操作零 external_access 账行、预留 consumed_at 已写（退款不留幽灵）、remaining 回到满额、category=usage、事件 attempt_refunded_not_accessed，两种形态（bind 前失败=not_started；源抛 AccessNotStarted=access）参数化覆盖；⑤ 恢复：落盘炸 + 结算也炸时原始 OSError 不被记账故障掩盖，billing_error 留档、预留保持未消费（ghosts=1），重启后 released_reservations 命中并补记 1 笔（同 attempt 仅 1 行、reason 含"崩溃恢复"），加本槽一次真实访问共 used=2.0、ghosts=0；已计费的失败 attempt 在同逻辑键重驱动时不被扣第二次，新 attempt 新的一笔，账按 attempt_no [1,2] 各一行；⑥ 额度只剩 1 → 失败的访问花掉最后 1 单位（remaining=0.0），下一次操作在预留阶段被 BudgetExhausted 挡住、显式标 skipped（BUDGET_EXHAUSTED），全库仅 1 行外部访问账、ghosts=0，既不越界也没有"免费第二次机会"
待复核：C-10 与 P0-11～P0-21（含首轮 C-08；C-09 已由 Codex 专项通过）。复现证据见 docs/verification/2026-10-24-P0-C.md 第 9 节（9.1 复现与双层危害、9.2 阶段×账务表、9.3 命令与结果 + 六项验收逐条对照、9.4 提请复核注意），单跑命令 `pytest tests/test_engine_worker.py -o addopts="" -q -k "charged or refund or not_accessed or overspend or explode"` → 10 passed, 29 deselected in 3.05s
阻塞/缺陷：C-10 FIXED 待复核；C-08（七处衔接缺陷，其中 #21/#22 落在 P0-B 已复核文件）仍待复核；C-01 自动发送未恢复（不阻塞手动开发）；本轮无新增阻断缺陷。设计取舍提请确认：(a) AccessNotStarted 继承 ValueError 只为不放宽既有数据源契约断言，语义是"源证明没发出"，退款资格仍由阶段决定——local 阶段抛同名异常也不退；(b) 访问已交出后发生的 WorkerAlreadyRunning（租约被接走）改为照计一笔，移交前失败仍在 reserve 之前返回、零访问零预留（两条既有所有权用例未改动仍绿）；(c) 结算失败的补偿路径依赖恢复补记，因此 _close_failed_attempt 一定先把操作标 failed 使其脱离 pending/in_flight；(d) 退款不写账行（零消耗）与缓存命中的 0 单位账行是两种口径，用事件 kind 区分；(e) 约束仍在 Python 写入层，直接改 SQL 可伪造 used，与 C-09 的边界声明一致
下一步：等 Codex 复核 P0-C 与 C-10（C-09 已专项通过）；通过后进入 P0-D（P0-22～P0-29 规则分析、证据、双图与五类导出），访问阶段计费契约沿用本轮实现
Checklist 更新：看板 P0-C → REVIEW（注明 13:40 C-10 返工完成并复测、第三次转 REVIEW）；缺陷表新增 C-10 行（FIXED 待复核）；完成证据登记新增"C-10 返工"行并更新 P0-C 看板行的数字；顶部新增 13:40 最新交接段、原 12:10 段改列"上一轮交接"、11:25 段改列"首轮 REVIEW 交接"，接收段新增 12:34 C-10；P0-11～P0-21 与 C-08/C-09/C-10 的复选框与 DONE 勾选均留给复核者，未自行勾选
```

### 交接记录 2026-10-26（Qoder → Codex）· P0-D

```text
执行范围：P0-D（P0-22～P0-29）——领域分词/字符 n-gram/TF-IDF/PMI 与低频降级、八类意图与来源类型及问题抽取（保留对象/痛点/地域/年份/否定）、可定位原文证据与版本·操作引用、稳定需求 ID 与保守可撤销归并、显性问题/推断需求/方案构想/联想意图候选四分区、S/Q/A/F 评分分解与内容家族去重及未知属性处理、关键词发现图 + 主题→问题→需求→证据图、五类产物导出与反读校验；P0-D 转 REVIEW，未进入 P0-E
输入：docs/coordination/QODER_GUIDANCE.md 顶部 14:24 交接与反例清单、docs/verification/2026-10-26-P0-C-CODEX-REVIEW.md、DEVELOPMENT_PLAN.md 第 5.1～5.4 节与第 6 节 P0.3 门槛、PROJECT_CHECKLIST.md、docs/DATA_MODEL.md
本轮交付：src/xhs_discovery/analysis/{lexicon,phrase_mining,corpus,extraction,clustering,scoring,graph,export,store,pipeline}.py（十模块，含新编排层 pipeline.py）、storage/migrations.py 新增 v4（post_queries.published_at_text/likes_text、content_family_members 成员行、needs.stable_key + 部分唯一约束）与 v5（phrases.analysis_version + idx_phrases_run_version）、tests/test_analysis_{phrases,extraction,clustering,scoring,graph,export,facts,pipeline}.py（8 文件 142 例）、docs/DATA_MODEL.md 第 9.3～9.5 节 #33～#47（落库与幂等口径 / 展示与导出安全口径 / 编排口径）、docs/verification/2026-10-26-P0-D.md、cli.py 的 export owner 文案（仍退 70）
范围边界：全程离线、未访问真实小红书/真实平台、未读取账号凭据、未下载模型权重、未调用外部 LLM/API、未提交或推送仓库、未修改项目外文件、既有需求文档全部保留；范围严格按计划原定细节未扩大；分析一律写新的 analysis_version，未改写 P0-C 的 p0-preliminary.1 行与 branch_stats 历史奖励，编排层在任何阶段动手前拒绝在线版本常量；分析层不写预算账本与 model_calls；fixture 证据全部标 synthetic；联想文本不产生帖子证据行、意图候选不进排行榜；未删除或 skip 任何既有测试；CLI/UI 接线留 P0-E，export/run/ui 命令行仍退 70、未做假 stub；页面文本只当数据；未实现验证码绕过/代理轮换/反检测
实际完成：全套 `.venv/Scripts/python.exe -m pytest -o addopts="" -q` → **719 passed in 55.69s，退出码 0**（基线 574 + P0-D 新增 145，其中 142 例在 8 个专项文件、3 例是 v4/v5 schema 形状断言随 tests/test_storage_migrations.py 补齐）；8 专项文件合并 → **142 passed in 17.73s**；相邻回归 10 文件（migrations/repository/ledger/atomicity/evidence 契约/text 规范化/discovery 图/在线归并/正文归属/CLI）→ **203 passed in 17.67s，退出码 0**；分文件 23/17/14/20/15/29/15/9（命令与耗时见报告第 2 节）；`.venv/Scripts/xhs-discovery.exe config-check --config config/offline-demo.yaml` → VALID hash da8ad12b6aad2357…（退出 0）；`export --run run-a` 与 `run --mode fixture --seed 上海中考 --config config/offline-demo.yaml` → 均退出 **70**；`pip check` → No broken requirements found
验收对照（Codex 14:24 点名的反例）：属性冲突 → 拆簇成 `#conflict[...]` 另一行并记 attribute_gaps（clustering 两例）；否定 → polarity 保留、否定自成一簇；广告 → `_counts_as_valid` 要求"用户求助 + 求助类意图 + 句内求助线索"三者齐备，营销里的工具连方案构想都不登记；未知日期 → F 缺失并记 `demand_score_unknown_component`，不当 0 也不编日期，"解析得出但已过时"才给真实 0；重复内容 → `content_families` + 成员行按代表版本收敛，Q 数家族去重后独立公开来源、副本不抬分、重放不写第二个家族；失效引用 → 三层防线（入库前用库里版本行复核定位并拒绝 / 图自检六码 / 导出按证据节点回落把需求挡在排行榜外），另有手改 quote 只落 `orphan_evidence` 审计不误删有效需求的用例；GraphML → 每属性单一 attr.type、id 是合法 XML name、`ref` 保持精确身份、复合与类型漂移统一 JSON 化成 string 并在 report 点名；CSV → `= + - @ 制表符 回车` 前置撇号、NUL→U+FFFD、反读逐格字节相等；JSON → meta 版本五件套、config.valid_settings_snapshot、budget.keys[].limit（快照不全时留空并置 limits_from_config=false）、limitations 点名合成数据与口径边界；导出反读 → 篡改文件/库内变动/目录多余文件/转义吃数据四类分别有独立检查名，同内容重跑字节一致
待复核：P0-22～P0-29 八项（复选框与 DONE 勾选均留给复核者）。证据见 docs/verification/2026-10-26-P0-D.md（第 1 节交付物、第 2 节命令与逐文件计数、第 3 节反例对照、第 4 节 P0.3 门槛、第 5 节 fixture→分析→导出跨层闭环实录、第 6 节本轮发现并修掉的三处编排层缺口、第 7 节已知限制、第 8 节交接）；docs/DATA_MODEL.md 第 9.3～9.5 节 #33～#47 为落库/导出/编排口径，请重点复核 #33（phrases 在部分唯一索引里的 NULL 口径）、#34（来源类型两层差异与 extractor_version 区分）、#41（失效引用回落到需求）、#44（report 自引用排除）、#47（重放与 snapshot 语义）
阻塞/缺陷：本轮无新增阻断缺陷；C-01 自动发送仍未恢复（不阻塞手动开发）。提请复核确认三处取舍：① 重放同一分析版本时业务行不重复但批次事件确实增加、report 内容随之改变，因此保留 ExportConflict 并要求显式 snapshot（不压事件）；② `orphan_evidence` 与 `problem_in_several_clusters` 故意只做审计不挡展示，避免一次人工篡改误删本来有效的需求；③ 数据集专属归一口径（ambiguous_seeds/locations）不在库里，`AnalysisRules` 默认空词表，需调用方传入装配好的规则。已知限制：`nlp.tfidf_batch_size` 目前是配置项无消费方；同批次共用一个时间戳；评分常量（对数锚点、近期窗口）不进配置而随 score_json 落库；`budget.model_calls/model_tokens/model_cost_usd` 恒为 0
下一步：等 Codex 独立复跑并复核 P0-D；通过后进入 P0-E（P0-30～38：CLI run/export 接线、Streamlit 看板与双图交互、暂停恢复追加、pytest 覆盖面确认、README 与 P0_ACCEPTANCE.md）；若复核回退则在 P0-D 内返工，不提前接入真实平台或模型
Checklist 更新：看板 P0-D DOING → REVIEW（2026-10-26 20:47）；第 1 节新增 20:47 最新交接段、原 13:40 段降为"上一轮交接"、12:10 段降为"更早交接"、14:40 接收段标注已完成转 REVIEW；角色表当前指派更新；第 3 节 P0-D 标题转 REVIEW（P0-22～P0-29 八项复选框保持未勾选）；完成证据登记新增 P0-22～P0-29 逐条与"P0-22～29 汇总"行；缺陷表本轮无新增
```

## 10. 使用方式

每次开工先读最新看板和关联任务；实现者将已接收的包标 DOING，完成后附证据标 REVIEW；复核者检查文件并运行相关验证，通过才勾选，失败标 REOPEN。一次交接应说明上一包结果与下一包，不把整份清单反复作为无边界指令。

给 Qoder 的下一条建议交接文本（需要实际发送后才算下达）：

> 阅读 PROJECT_CHECKLIST.md，把它作为唯一任务状态清单，结合 DEVELOPMENT_PLAN.md 执行。先报告你当前已完成、正在执行及阻塞的事项和证据，不凭此前口头汇报勾选。若当前用户指令允许开始开发，先承接 P0-A；完成后更新状态和证据，再依赖顺序推进 P0，P0 验收后报告结果。

2026-10-28最终登记：P0-35～38/G0-01～03由Codex接受；证据见独立G0报告，Qoder原始REVIEW与报告保留为历史。未进入P1真实访问。

2026-10-28 P1-A独立复核：C19关闭，P1-01/02 DONE；见P1-A-CODEX-REVIEW，919项全套/90专项。P1-03/04仍须下一包的DB归属恢复与worker暂停/退避证据，不先勾选。


