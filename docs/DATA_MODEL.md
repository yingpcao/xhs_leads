# 数据模型与恢复契约（P0-05 冻结）

版本：`schema_version = 1`，`protocol_version = 1`，分析处理版本见 `_version.ANALYSIS_VERSION`。
本文是 P0-B migration 与 Repository 的唯一实现输入：列名、逻辑键、索引与状态迁移规则以本文为准。
产品语义见 `DEVELOPMENT_PLAN.md` 第 4 节，`PROJECT_SPEC.md` 第 8 节是早期草案，冲突时以本文与开发计划为准。

## 0. 分层原则

| 层 | 归属 | 规则 |
|---|---|---|
| 全局身份 | `keywords`、`posts`、`authors` | 跨 run 共享，只存身份与稳定属性，不存任何单一运行的状态 |
| 任务状态 | `runs`、`run_keywords`、`branches`、`frontier`、`operations`、预算与事件 | 全部带 `run_id`，跨 run 完全隔离 |
| 观察记录 | `*_observations`、`*_items`、`post_queries` | 一次采集一条观察；同一词在不同会话可产生多条观察，历史观察不删除 |
| 分析结果 | `phrases`、`problems`、`needs`、`evidence`、`cluster_assignments` | 带 `run_id` + `analysis_version` + `extractor_version`，引用具体内容版本 |
| 运行支持 | `raw_artifacts`、`model_calls`、`artifacts`、`run_events`、`worker_leases`、`schema_migrations` | 可诊断、可迁移、可重放 |

统一约定：`id` 为 `INTEGER PRIMARY KEY`；时间戳一律 `TEXT` 存 ISO-8601 UTC（带 `+00:00`）；
布尔用 `INTEGER` 0/1；枚举用 `TEXT` 并加 `CHECK` 约束；JSON 用 `TEXT` 并保证可反序列化；
`sha256` 用 `TEXT` 且 `length=64`。外键全部 `ON DELETE RESTRICT`，删除只走显式的 run 清理流程。

## 1. 全局身份层

### keywords
| 列 | 类型/约束 | 说明 |
|---|---|---|
| id | PK | |
| normalized_text | TEXT NOT NULL UNIQUE | 只做空白与全半角规范化 |
| first_seen_at | TEXT NOT NULL | 全局首次出现 |
| is_ambiguous_seed | INTEGER NOT NULL DEFAULT 0 | 疑似歧义只提示，不改写 |

`keyword_aliases`：`id PK, keyword_id FK NOT NULL, raw_text TEXT NOT NULL, first_seen_at TEXT NOT NULL`；
`UNIQUE(keyword_id, raw_text)`。原始写法必须保留，检索与展示都取原始写法。

### posts
| 列 | 说明 |
|---|---|
| id PK | |
| note_id | TEXT NOT NULL UNIQUE —— 帖子身份只依 note_id |
| access_url | TEXT NOT NULL —— 完整可访问链接，不得为去重而改写或截断 |
| first_seen_at | |
| author_id | FK → authors NULL —— 作者引用未知时为 NULL，不当作不同作者 |

### post_content_versions（标题/正文的内容版本）
| 列 | 说明 |
|---|---|
| id PK | |
| post_id | FK NOT NULL |
| field | CHECK IN ('title','body') |
| text | TEXT NOT NULL |
| text_sha256 | TEXT NOT NULL |
| captured_at | TEXT NOT NULL |
| is_missing | INTEGER NOT NULL DEFAULT 0 —— 确实为空/不可访问显式表达 |

`UNIQUE(post_id, field, text_sha256)`：文本变化产生新版本，旧证据始终指向旧版本，不被覆盖。

### authors（最小化公开引用）
| 列 | 说明 |
|---|---|
| id PK | |
| public_ref | TEXT UNIQUE NULL |
| kind | CHECK IN ('public_ref','unknown') |
| relevant_post_count | INTEGER NOT NULL DEFAULT 0 |

`public_ref IS NULL` 只允许 `kind='unknown'`。不采集个人属性。

## 2. 任务状态层

### runs
| 列 | 说明 |
|---|---|
| id PK | 文本 UUID 主键（`TEXT PRIMARY KEY`） |
| seed_raw / seed_normalized | 种子原文与规范化形式 |
| mode | CHECK IN ('fixture','live') |
| status | CHECK IN ('queued','running','paused','completed','failed') |
| outcome | CHECK IN ('budget_exhausted','converged','queue_exhausted','no_actions_available','cancelled','fault') NULL |
| pause_reason | CHECK IN ('user','auth_required','action_required','profile_busy','selector_mismatch','network','fault') NULL |
| config_json | 完整配置快照（含 hash） |
| config_hash | TEXT NOT NULL |
| code_version / protocol_version / analysis_version / random_seed | |
| synthetic_fixture | INTEGER NOT NULL —— fixture 运行必须为 1 |
| created_at / started_at / finished_at / updated_at | |
| budget_revision | INTEGER NOT NULL DEFAULT 1 |

状态迁移只允许：`queued→running`、`running→paused|completed|failed`、`paused→running|completed|failed`；
写入必须走 `run_events` 记录原因，禁止 UI 直接改 `status`。

### budget_revisions
`id PK, run_id FK, added_json TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT`。
追加预算重开 run 时新增一行并递增 `runs.budget_revision`，原始事件与观察保留。

### run_keywords（run 内的关键词状态）
| 列 | 说明 |
|---|---|
| id PK | |
| run_id FK | |
| keyword_id FK | |
| UNIQUE(run_id, keyword_id) | 同一关键词在一个 run 内只有一行状态 |
| parent_keyword_id | FK NULL —— 首次发现父节点；多父路径另存于 `discovery_edges` |
| depth | 首次调度深度 |
| branch_id | FK → branches —— 后代继承首次调度归属 |
| origin | CHECK IN ('observed','generated','seed') |
| status | CHECK IN ('queued','explored','skipped','irrelevant','stale') |
| revisit_count | INTEGER NOT NULL DEFAULT 0 —— 重探次数，对应 `revisit_seq` |
| first_seen_at / last_seen_at | |
| irrelevant_reason | 剔除原因必须保存；边界词进有限探索份额而不是直接判死 |

`status` 的写入时机（P0-C 实现口径）：`seed` 与登记的候选一律先 `queued`；被槽位取走并跑完联想观察后转 `explored`；
相关性判定剔除的转 `irrelevant` 并**必须**带 `irrelevant_reason`（CHECK 已约束）；`skipped`/`stale` 由后续工作包写入。
`explored` 表示"本 run 已对它发起过观察"，不等于"已充分探索"——同一 `keyword_id` 可以再开新的 `revisit_seq` 操作行，
队列身份与观察身份是两张表，不能拿 `run_keywords.status` 去抑制重探。

### discovery_edges（发现边；SUGGESTS 只由 observed 产生）
`id PK, run_id FK, parent_keyword_id FK, child_keyword_id FK, rank INTEGER, session_id TEXT, observed_at TEXT, origin CHECK IN ('observed','generated'), operation_id FK, UNIQUE(run_id, parent_keyword_id, child_keyword_id, session_id, origin)`。
`origin='generated'` 的行只是探针线索，不建 SUGGESTS 图边；多父路径全部保留。

### branches / branch_stats
`branches`：`id PK, run_id FK, branch_key TEXT NOT NULL, display_label TEXT NULL, created_at`；`UNIQUE(run_id, branch_key)`。
分支早期使用稳定 `branch_key`（如 `b1`），展示标签可变但主键不变，避免历史奖励无法解释。
`branch_stats`：`run_id+branch_key` 唯一年 `trials, total_reward, mean_reward, stale_streak, min_validation_share_left, updated_at`。

### frontier（持久候选队列）
| 列 | 说明 |
|---|---|
| id PK | |
| run_id FK | |
| keyword_id FK | |
| branch_id FK | |
| score / score_breakdown_json | 归一化分与特征分解（原值+归一化值+权重） |
| slot_kind | CHECK IN ('high_score','branch_exploration','random_novelty') NULL |
| sample_kind | CHECK IN ('active_exploration','coverage_calibration') —— 两类样本必须分开 |
| selection_reason | TEXT NOT NULL —— 选取理由必须可解释 |
| selection_probability | REAL NULL —— 可计算部分才记录 |
| queued_at / attempted_at | |
| status | CHECK IN ('pending','in_flight','done','dropped') |
| active_revisit_seq | INTEGER NULL —— v8（C22）：认领槽位时锁定的活动重探序号 |
| UNIQUE(run_id, keyword_id, branch_id) | 队列按 run 隔离；另一 run 可独立探索同一词 |

`in_flight` 行是崩溃恢复的判断依据：重启后仍为 `in_flight` 且无可靠 operation 结果时标 `uncertain`。

`active_revisit_seq`（C22 未完成槽位恢复）：
- **认领与递增同事务落定**：调度器取走一个 `pending` 槽位时，`Repository.claim_frontier_slot` 在一个事务里
  把 `status` 置 `in_flight`、写入 `active_revisit_seq = run_keywords.revisit_count`（递增前的值）、并把
  `revisit_count + 1`。这样接管的新 worker 知道上一任正在跑第几次重探，能沿用**同一** `revisit_seq`
  重开同一逻辑键的 operation，离线补入库落盘的规范化结果，而不是把它再递增成下一轮、开出第二个 operation
  去重打 CLI（重复扣预算、丢第一次结果）。
- **可空是有意为之**：v7 之前的历史 `in_flight` 行补不回当时的序号，恢复时保守处理——保留在飞状态、显式记
  `frontier_legacy_uncertain` 事件交人工核查，绝不猜序号、绝不重发。
- **阻断错误退回 pending 时清序号**：认证/验证/占用这类阻断信号停住的槽位走 `reset_frontier_for_retry`
  （`status='pending'` + `active_revisit_seq=NULL`），人工处置后按新一轮重探重新认领；历史 attempt/预算/观察不动。
- `pending_items` 与分支待办计数（`stopping.collect_signals` / `branch_states`）都把 `in_flight` 计入，
  防止"还剩一个未完成槽位"被误判成队列排空/收敛。
- **落盘未入库的离线补入库**（`engine._maybe_reingest_landed`，接管的 `_execute` 里先于访问循环触发）：
  只认库中登记的、basename 恰为 `{operation_type}.json` 且**不在 `cli/` 目录**下的 `stdout_json` 信封——
  `cli/cli_stdout.json` 是原始工具响应，永不作为规范化结果解析。找回后依次校验：文件存在、`sha256`/`size_bytes`
  与登记一致、可按契约模型解析、`context.{run_id,keyword_id,operation_type,revisit_seq}` 与操作归属一致、
  `session.{source_name,source_version}` 有值且 `session.synthetic` 与 `runs.synthetic_fixture` 相符。
  - 全部通过且该 attempt 尚无观察 → 离线 `attach` 补入库、`record_cache_hit`（不访问、不二次扣费）、复用原
    `attempt_id`，落 `landed_result_reingested` 事件；已有观察则视为历史证据、交正常重放。
  - 文件不可用（登记在库但字节丢失）、损坏、或身份/来源对不上 → 标 operation `uncertain`、置
    `LANDED_RESULT_INVALID` 阻断（`human_action_required`），落 `landed_result_invalid`（含 `normalized_artifact_unavailable`），
    **绝不重发覆盖旧批次**：坏证据槽位保留 `in_flight` 交人工核查，走 C10 既有记账（崩溃那次访问照计，不退款、不二次扣）。

`slot_kind = NULL` 表示"这一行不属于任何固定槽位，任何桶都可以取它"，是 P0-C 的正常入队形态，不是缺省值漏写：
- 种子行入队时还没有分数，若钉成 `high_score` 会让高分桶（要求 `score IS NOT NULL`）和其余两桶都取不到它，队列第一步就停摆；
- 算子扩展出的候选**不继承**发现它的那个槽的类型，否则一次分支探索会把后代全部染成同一类，60/25/15 的分配退化成一个桶。
只有调度器真的按某个桶取走它时，才在槽位上带上 `slot_kind`；`NULL` 行可被任一桶认领（`_pending_for_kind`），
空桶回流也据此把候选让给别的桶。

### operations / operation_attempts
`operations`：`id PK, run_id FK, keyword_id FK, operation_type CHECK IN ('suggest','search','body','author_posts'), revisit_seq INTEGER NOT NULL DEFAULT 0, status CHECK IN ('pending','in_flight','completed','partial','failed','skipped','unsupported','uncertain'), branch_id FK NULL, slot_kind NULL, sample_kind NULL, first_started_at, last_finished_at`。
**逻辑键：`UNIQUE(run_id, keyword_id, operation_type, revisit_seq)`。**

`revisit_seq` 的分配规则（P0-C）：
- `suggest/search` 用被调度关键词的 `run_keywords.revisit_count`，同一个词第二次进入队列自然拿到新序号；历史尝试用完后（`attempt_no > 1 + retry.max_retries`）**只在原行上记 skipped 并说明"要重做得有新的重探序号"，不再访问平台也不扣额度**；
- `body` 每篇帖子是一次独立访问，按 `MAX(revisit_seq)+1`（`_next_seq_for`）取新序号——逻辑键里没有 `note_id`，共用序号会把不同帖子的观察互相覆盖；
- `author_posts` 同样按 `MAX(revisit_seq)+1`；
- 校准样本落在保留段 `revisit_seq >= CALIBRATION_SEQ_BASE (=1000)`，从该词已有最大序号之后递增，主动探索的序号段永不与校准撞键。

`operation_attempts`：`id PK, operation_id FK, attempt_no INTEGER NOT NULL, started_at, finished_at, result_state, error_code, error_json, exit_code INTEGER NULL, duration_ms, raw_artifact_count, UNIQUE(operation_id, attempt_no)`。
重试 = 新 attempt；外部访问成本照计。同一次 attempt 重复导入不重复写观察、证据与预算事件（靠 `raw_artifacts` 内容哈希幂等）。
异常收口的 attempt（引擎捕获的异常，不是"平台返回 failed"）在 `error_json` 里除 `message`/`category` 外还带三个可审计字段：`access_phase`（`not_started`/`access`/`local`，见第 6 节第 17 条）、`access_charged`（这笔访问是否已计入预算，1/0）、`billing_error`（结算自身失败时的原始记录，正常为 NULL）。
`exit_code` 与 `error_code` 都要存：CLI 退出码 5 同时覆盖浏览器、网络、页面与选择器错误，单看退出码无法定性。分类规则（`models/errors.py:classify_cli_failure`）：已知逐词 `error.code` 优先——`NETWORK_ERROR/PAGE_TIMEOUT` 可有限重试，`SELECTOR_MISMATCH/PAGE_ERROR/OUTPUT_ERROR` 暂停诊断，认证与验证类无论退出码为何都优先暂停；只有退出码、没有可识别 `error.code` 时按退出码表归 `page` 并暂停；未登记的退出码或状态一律 `UNKNOWN_CLASSIFICATION` + 需人工核查，禁止静默当"无结果"。

### budget_reservations / budget_ledger
`budget_reservations`：`id PK, run_id FK, budget_key TEXT CHECK(属于预算键), units NUMERIC NOT NULL CHECK(units>0), created_at, consumed_at NULL, operation_id FK NULL, operation_type NULL CHECK(枚举)`。
预留必须有归属才能被释放或结算：`release`/`settle` 都要声明 `budget_key`（结算还声明 `operation_id`），跨 run、跨键、跨操作的认领一律拒绝；同一操作同键同额的预留，结算时忘带 `reservation_id` 会被**自动认领**，不留幽灵预留。
`budget_ledger`：`id PK, run_id FK, operation_id FK NULL, budget_key TEXT NOT NULL, operation_type NULL, attempt_no NULL, consumed_units NOT NULL, external_access INTEGER NOT NULL, reason TEXT NOT NULL, created_at`；
`CHECK (external_access=1 → operation_id NOT NULL AND attempt_no NOT NULL AND consumed_units>=1)`，
`CHECK (external_access=0 → consumed_units=0 OR budget_key 属于非访问配额)`，
`CHECK (external_access=0 → operation_id IS NULL OR …)` 之外还要求"缓存命中行不带 operation_id 时可留空"。
预算键：`suggest_attempts|search_attempts|body_attempts|author_attempts|calibration_suggest_attempts|max_posts_stored|max_runtime_minutes|model_calls|model_tokens|model_cost_usd`。
访问预算键与操作类型一一对应：`suggest→{suggest_attempts, calibration_suggest_attempts}`、`search→search_attempts`、`body→body_attempts`、`author_posts→author_attempts`；把某类操作的花费记进别的桶，等于同时虚增一簿、虚减一簿。
防重复扣费由部分唯一索引 `UNIQUE(run_id, operation_id, attempt_no) WHERE external_access=1` 在库层面保证（详见第 9 节）；索引只保证"同一 attempt 至多一条"，**参数是否还是同一件事**要由写入层核对：键、类型、单位任一不符的重复导入是冲突，不是重复。

### run_events（追加式事实）
`id PK, run_id FK, at TEXT, kind TEXT, detail_json TEXT`；kind 例：`status_changed, paused, resumed, extended, budget_hit, stopped, artifact_orphaned, imported_after_restart, branch_stalled, calibration_drawn`。
P0-C 实际写入的 kind（复核对照用）：`retry_backoff`（有限重试的退避计划：operation_id/attempt_no/planned_seconds/error_code）、
`preliminary_merge`（在线初步归并的簇与证据）、`intent_candidates_recorded`（联想意图候选，不建证据）、
`candidates_dropped`（相关性判定剔除的词与理由）、`graph_path_truncated`（超深/回环路径被截断的记录）、
`branch_suspended`（分支停滞暂缓）、`calibration_pool_frozen`（校准池冻结：成员与版本）、`calibration_sampled`（本次抽到的样本，带"不给主动探索记功"说明）、
`attempt_charged_after_failure`（C-10：访问已发生或无法证明未发生，异常收口仍照计一笔，detail 带 `access_phase`/`access_charged`/`billing_error`）、
`attempt_refunded_not_accessed`（C-10：有证据证明请求没发出，预留退回可用池，detail 同上口径）、
`stop_<status>`（终局决策，detail 内是完整信号快照与 note）。
冻结池放在 `run_events` 而不是新表：它是"某一时刻抽了哪些样本"的追加事实，重放与复核都按时间读，不需要按成员检索。

### control_requests（UI 写、worker 消费）
`id PK, run_id FK, action CHECK IN ('pause','resume','extend','cancel'), requested_at, applied_at NULL, suggest_budget_delta NULL, search_budget_delta NULL, note NULL`；
`CHECK (action='extend' → 至少一个正增量)`；`extend` 是追加额度，绝不重置总量。

### worker_leases
`id PK, run_id FK UNIQUE, owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL, acquired_at, heartbeat_at, expires_at, released_at NULL`。
`owner_token` 是 worker 实例启动时随机生成的 16 字节令牌；租约与进程锁的写入、心跳、释放都按 token 校验，pid 只做存活探测，**不作为所有权凭证**。
判断运行中必须同时核查 OS 进程锁与未过期租约，**遗留 lock 文件本身不足以证明在运行**；进程存活探测返回三态 `live/dead/unknown`，`unknown`（如权限不足查不到进程）**永不**当作已死。跨 run 另有一把全局锁 `data/worker-global.lock`，同一台机器只允许一个 run 的 worker 在跑。

## 3. 观察记录层

### suggestion_observations
`id PK, run_id FK, operation_id FK, attempt_id FK, session_id TEXT NOT NULL, keyword_id FK NOT NULL, observed_at TEXT NOT NULL, state CHECK IN ('completed','partial','failed','skipped','unsupported'), item_count INTEGER NOT NULL, source_name, source_version, synthetic INTEGER NOT NULL, UNIQUE(operation_id, attempt_id, session_id)`。
确认空结果是一条 `state='completed', item_count=0` 的有效观察；失败观察 `state='failed'` 且不计入需求缺失。

### suggestion_items
`id PK, observation_id FK, rank INTEGER NOT NULL, text_raw TEXT NOT NULL, text_normalized TEXT NOT NULL, keyword_id FK NOT NULL, UNIQUE(observation_id, rank)`。
保留原始顺序；`rank` 参与 S 信号计算。

### post_observations / post_queries
`post_observations`：`id PK, run_id FK, operation_id FK, attempt_id FK, session_id, keyword_id, observed_at, state, card_count, source_name, source_version, synthetic, UNIQUE(operation_id, attempt_id, session_id)`。
`post_queries`：`id PK, run_id FK, post_observation_id FK, post_id FK, keyword_id FK, result_rank INTEGER NOT NULL, UNIQUE(run_id, post_observation_id, post_id)` —— 来源查询词与顺序可追溯。
再加 schema v4 补的两列页面可见文本：`published_at_text TEXT NULL, likes_text TEXT NULL`。
它们记的是**这次结果页显示的原文**，不是解析后的日期或数字：F（近期持续性）要靠原文重算，"点赞数不等于搜索量/人数"的展示口径要靠原文守住（第 8 节）。放在 `post_queries` 而不是全局 `posts`，因为同一篇帖子在不同观察里的点赞会变，写进身份表会互相覆盖。缺失就是 NULL：`published_at_missing` 的卡片落 NULL，分析层据此报"页面没给"，绝不代填。升级上来的历史行两列一律 NULL（第 9.1 节 #30）。
再加 schema v7 补的一列观察版本引用：`title_version_id FK NULL` —— 这次卡片观察实际看见的是 `post_content_versions` 里的哪一版标题。
`post_content_versions` 按 `(post_id, field, text_sha256)` 去重是**内容层**事实，"这次观察看到了哪一版"是**观察层**事实，两层不能混：标题 A→B→A 时第三次观察复用 A 的旧行（`captured_at` 仍停在第一次），重放/语料/评分若按"观察时刻前最新版本"推断就会把第三次读成 B（第 9.6 节 C-15）。v7 之前的历史行该列为 NULL：当时没记下看见的是哪一版，事后猜比留空更糟；对 NULL 引用，`title_version_for_observation` 只在观察时刻前**唯一**已捕获版本时作可解释推断，多个候选一律 unknown（第 9.6 节 #49 C-15 r3），这是老库兼容口径，不是当次观察的事实。

### body_observations
`id PK, post_id FK, state, body_is_empty, content_version_id FK, captured_at`，再加 schema v3 补的四列来源：`run_id FK NULL, operation_id FK NULL, attempt_id FK NULL, session_id TEXT NULL`，并带 `UNIQUE(operation_id, attempt_id, session_id)`（索引 `idx_body_observation_attempt`）与 `body_observations(run_id, state, post_id)`。
"我这个 run 取到过这篇帖子的正文"是**按 run 发生的事实**：`post_id` 与 `post_content_versions` 照旧全局共用（同一段文本不因 run 不同存两份），但观察行、以及由它派生的内容验证量必须归本 run。老库升级上来的历史行来源列为 NULL——归属无法确定就不计入收敛，也不事后编一个 run（详见第 9.1 节 C-09）。

`post_content_versions` 的 `is_missing` 与 `body_observations`（`state='completed'` 时 `content_version_id` 与 `body_is_empty` 至少有一个不为空，见表上的 CHECK）共同表达“不可访问”“确实为空”与“取到了正文”的区别。

## 4. 分析结果层

### suggestion_versions（联想词内容版本）
`id PK, observation_id FK NOT NULL, rank INTEGER NOT NULL CHECK(rank>=1), text TEXT NOT NULL, text_sha256 TEXT NOT NULL(length=64), captured_at TEXT NOT NULL, UNIQUE(observation_id, rank)`。
suggestion 证据与问题也要有"可定位、可校验哈希"的版本对象，`Evidence.locate()` 才能在同一套口径下算偏移；它挂在观察上，因此删观察受 `RESTRICT` 保护，历史引用不会悬空。

### content_families（近重复抑制，不删除帖子/作者）
`id PK, representative_version_id FK, method TEXT, member_count INTEGER, analysis_version TEXT, created_at`。
成员关系由 schema v4 的 `content_family_members` 表达：`family_id FK NOT NULL, content_version_id FK NOT NULL, similarity REAL NULL(0..1), analysis_version TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(family_id, content_version_id)`，另有 `idx_family_members_version(content_version_id, analysis_version)`。
只有 `member_count` 没有成员行时，"Q 个独立公开来源"这类计数无法审计，改判时也无处把某个版本挪出家族；成员行让去重是可核对的关系而不是一个自说自话的数字。帖子与作者身份照旧不删。

### phrases
`id PK, run_id FK, text TEXT, kind CHECK IN ('word','ngram','pmi_phrase'), score REAL, freq INTEGER, source_version_id FK, extractor_version, UNIQUE(run_id, text, kind, source_version_id)`。
小样本低于 `nlp.min_phrase_freq` 时跳过并在 `run_events` 记录 `phrase_skipped`，不硬造短语。

### problems
| 列 | 说明 |
|---|---|
| id PK / run_id FK | |
| canonical_text | 原句，永远保留 |
| content_version_id / suggestion_version_id | 两个可空 FK，**恰好一个非空**：title/body 问题指 `post_content_versions`，suggestion 问题指 `suggestion_versions`；不用无外键保护的万能 `source_version_id` |
| source_field | CHECK IN ('suggestion','title','body') |
| source_type | CHECK IN ('user_help','experience_share','institution','marketing','unknown') |
| topic / object / intent / pain_point / location / year / polarity | 结构属性，NULL=未知（不等于任意兼容） |
| extraction_method / extractor_version | |
| counts_as_valid_problem_cluster | INTEGER —— 营销、经验分享、纯资源标题默认 0 |

### needs（需求簇）
`id PK, run_id FK, canonical_text, statement_kind CHECK IN ('explicit_problem','inferred_need','solution_idea','search_intent_candidate'), category TEXT NULL, evidence_status CHECK IN ('verified','inferred','unverified_candidate'), score REAL NULL, score_json TEXT, evidence_complete_json TEXT NULL, analysis_version, extractor_version, stable_key TEXT NULL（schema v4）, created_at, updated_at`，并带 `idx_needs_stable_key`：`UNIQUE(run_id, analysis_version, stable_key) WHERE stable_key IS NOT NULL`。
`stable_key` 是需求身份的持久键（由规范化后的簇签名派生，见第 9.1 节 #32）：同一 `analysis_version` 重跑必须命中同一行，`canonical_text` 会因为属性冲突拆分而带 `#conflict[...]` 后缀，是结果不是身份。键跨版本可以重复——新版本改判写新行，旧版本的历史行一字不动。P0-C 的 `p0-preliminary.1` 历史行 `stable_key` 为 NULL，多条 NULL 互不冲突。
方案构想永远 `evidence_status='unverified_candidate'` 且不参与有效需求计数。
`evidence_complete_json.attributes` 只装**全体成员共同已知**的属性交集：一处 `location`/`year`/`polarity` 缺失（NULL）不等于与别组成员的已知值兼容——缺口记进 `attribute_gaps`，已知与缺失的分裂簇另起一行（`#conflict[...]` 后缀），归属行 `auto_merged=0` 且 `attributes_conflict_json` 带该属性；`members[].attributes`/`members[].attribute_gaps` 逐成员留存各自的已知与缺口，绝不用别组的值把缺失粉饰成已知。
推断需求（`statement_kind='inferred_need'`）同口径（C-11-r2）：模板渲染文字相同而父簇属性轮廓不同**不算同一条需求**——`scope_of` 会去掉已被 topic 包含的地域，`pain_point`/`polarity` 等又不进模板，因此相同展示文本不能当归并身份。基线轮廓（含最小成员问题 id 的父簇）保持裸签名，其余轮廓在 `canonical_text` 上带 `#conflict[...]` 后缀并各持自己的 `stable_key`；`evidence_ids` 逐组独立，`derived_from_signatures`/`derived_from_stable_keys` 列出该组全部父簇引用（`derived_from`/`derived_from_stable_key` 是其中最小签名父簇的主引用），`conflicts`/`gap_attributes`/`split_from_signature` 记非等价分裂。同轮廓多父簇并成一组时证据取并集、引用全列，不丢组、不丢证据、不伪装属性。

### evidence
| 列 | 说明 |
|---|---|
| id PK / run_id FK | |
| source_field | suggestion/title/body |
| quote | 原文片段 |
| char_start / char_end | 在对应内容版本中的位置 |
| content_version_id / suggestion_version_id | 两个可空 FK，**恰好一个非空**：suggestion 证据指 `suggestion_versions`，title/body 证据指 `post_content_versions` |
| container_sha256 | 与所指内容版本一致 |
| operation_id / attempt_no / session_id | 采集来源 |
| keyword_id | 来源关键词 |
| post_id NULL / note_id NULL | 内容类证据必填 |
| post_observation_id NULL（schema v6，C-12） | 发布时间文本所属的卡片观察（`post_observations.id`）。评分解析相对日期只认这条观察的 `observed_at`；`created_at` 记录分析写入时刻，永远不是解析锚点。v5 之前的历史行为 NULL——锚点无法确定就保守 unknown，不事后编一个观察 |
| extractor_version / analysis_version / synthetic | |

写入前必须由 `Evidence.locate()` 计算偏移并校验 `container_text[start:end] == quote`；不能定位的引用直接拒绝入库。

### cluster_assignments
`id PK, run_id FK, problem_id FK, need_id FK, method TEXT, similarity REAL NULL, attributes_conflict_json TEXT, auto_merged INTEGER, analysis_version, superseded_at TEXT NULL, UNIQUE(run_id, problem_id, analysis_version)`。
归并可撤销：改判写新行并把旧行 `superseded_at` 置位，不删除原问题、不改写历史。

## 5. 运行支持层

- `raw_artifacts`：`id PK, run_id FK, operation_id FK NULL, attempt_id FK NULL, rel_path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER, kind CHECK IN ('stdout_json','stderr_log','raw_response','export'), created_at`；`UNIQUE(rel_path, sha256)`。先写临时文件、原子改名，再在事务里写引用与状态。
  `operation_id/attempt_id` 可空只有一种合法来源：重启补导入扫到**无法归属**的孤儿文件时，仍把它登记成事实并写 `artifact_orphaned` 事件，绝不让它悄悄存在或被删除。
- `model_calls`：`id PK, run_id FK, task, model, prompt_version, input_hash, status, input_tokens, output_tokens, cost_usd REAL NULL, latency_ms, error_json, output_rel_path`；`cost_usd` 在价格未知时为 NULL 并显示 unknown，不写 0。凭据永不入库与日志。
- `artifacts`（导出）：`id PK, run_id FK, kind CHECK IN ('keywords_csv','needs_csv','evidence_csv','graphml','report_json'), rel_path, sha256, created_at, analysis_version`。
- `schema_migrations`：`version INTEGER PRIMARY KEY, applied_at, code_version`。
- `run_retention`：`run_id FK PRIMARY KEY, raw_artifact_days, export_days, note`，清理只删目标 run 私有数据，被其他 run 引用的全局身份与内容版本保留。

## 6. 幂等与恢复规则

1. **重复导入**：同一 `(operation_id, attempt_id, session_id)` 的响应按 `raw_artifacts.sha256` 去重；观察与派生结果只在首次插入，预算账本同理由 `(run_id, operation_id, attempt_no)` 唯一。
2. **重启补导入**：扫描 `data/runs/<run>/operations/**` 中未入库产物，按内容哈希幂等导入；导入完成后仍无可靠结果的操作标 `uncertain`。
3. **已完成不重放**：`operations.status IN ('completed','partial','unsupported','skipped')` 不重跑。
4. **不宣称 exactly-once**：外部浏览器访问的尝试成本照计；是否补采形成新 attempt，允许重复网络访问但绝不允许重复记账。
5. **跨 run 隔离**：所有状态查询必须带 `run_id`；同一关键词在两个 run 中可有完全不同的状态与队列位置。
6. **事务纪律**：写事务保持短小；`PRAGMA foreign_keys=ON`、`journal_mode=WAL`、`busy_timeout=5000`；迁移从空库与上一版本库分别验证。
7. **关键词身份（契约）**：`operations` 逻辑键里的关键词标识在**调度与预算扣减之前必须已是入库的 `keyword_id`**。仅有关键词尚未入库的预检阶段允许 `OperationContext.allow_pre_registry=True`，此时身份取 `norm:<keyword.normalized>`（同一词的别名写法规范化后共享身份，不同词必然不同身份）；`keyword_id` 为空又未显式声明 `allow_pre_registry` 的上下文由 Pydantic 直接拒绝，防止两个未落库的词共享 `None` 幂等键。入库后重建上下文必须回到 `keyword_id`，两套身份不混用。
8. **产物路径归属**：`raw_artifacts.path` 只允许 run 目录内的相对路径；绝对路径、盘符、UNC、根相对路径与任何形式的 `..`（含 `..\`）在模型层即拒绝。重启补导入时除字符串检查外，还要在解析后确认目标仍在本次 run 目录内（`RuntimePaths.resolve_artifact(rel, under=run_dir)`），避免把其他 run 或项目外的文件当作本次结果导入。
9. **迁移原子性**：迁移脚本逐语句在显式 `BEGIN … COMMIT` 中执行，中途失败整段 `ROLLBACK`；不用 `executescript()` 的自动提交，否则失败会留下"半张 schema + 已登记的版本号"这种最坏状态。语句切分用 `sqlite3.complete_statement` 判定终结（一行两条语句、串内分号都要正确切开）。迁移入口（升级、回滚）在连接已有未提交事务时**直接拒绝**，不在 `_atomic` 内部才检查——Python 传统隔离模式下 DDL 会隐式提交外层批次，先检查才能保证"迁移独占结构变更"且"不顺手提交别人的批次"。以旧代码打开高版本库直接拒绝，不带部分 schema 运行。
10. **崩溃窗口只认 `in_flight`**：恢复时 `in_flight` 且有已完成 attempt 的按最新 attempt 的 `result_state` 回填；`in_flight` 且没有任何结果的才标 `uncertain` 并记 `artifact_orphaned`。`pending` 是正常排队状态，不是崩溃证据，恢复流程不得改它。终态（`completed/partial/failed/skipped/unsupported`）永不重放。
11. **预留的存留与归属**：`release_stale_reservations` 只放开"没有归属操作"或"操作已不在 `pending/in_flight`"的预留；还在排队或在途的操作继续占额度，避免恢复后超预算访问。放开时按预留所属 run/键校验，跨 run 或换键释放直接拒绝。上限口径为 `base + Σ budget_revisions.added_json`，**从库派生**，进程重启重放同一条 `extend` 不会产生第二份额度，外层批次回滚也不会留下"版本增了、额度没增"的裂口。崩溃恢复对"已 `started_at` 的 attempt"按每次真实访问补记消耗（`uncertain ≠ 没发生`），已发起的预留不退款。
12. **worker 运行判定**：`正在运行 = 进程锁指向的 owner 在 OS 层未被证明死亡（live 或 unknown） AND 租约未过期且未释放`，两样缺一才判为"未在运行、可接管"。所有权凭证是 `owner_token`（实例启动时随机生成），不是 pid——pid 会被复用，同 pid 的第二个进程不算同一个 worker；心跳与释放都带 `WHERE owner_token = ?`，被接管后的僵尸实例写不回锁与租约。**归属记录文件（`worker.lock`）之外的 `worker.lock.guard` 才是独占的真身**：worker 拿到它之后一直持到 `release()` 或进程退出（进程被杀时由 OS 释放），该文件只创建、永不删除，因此"probe→unlink→create"这种按旧快照删掉胜者锁的竞态不存在。接管遗留记录必须写 `stale_lock_removed` / `lease_taken_over` 事件；对"记录写着仍存活/无法判定的 pid"，`force=True` 只绕过这层**推断**并额外写 `forced_takeover` 事件，**绕不过 guard**——guard 被别人持着时第二个 worker 一定起不来（审计事件不实现单 worker 保障）。拿不到 guard 或中途失败时必须撤回已写的记录并交还全部互斥，不留半拿状态。同一台机器跨 run 还要抢 `data/worker-global.lock`（同样配 guard），per-run 锁不能替代它。
13. **事务所有权**：存储层唯一写入口是 `db.transaction(conn)`——外层无事务时 `BEGIN … COMMIT`，已在外层事务里时降级为 `SAVEPOINT`，只回滚自己那段。存储层禁止 `with conn:`（它会提交调用方的批次）。迁移、落盘、账本、Repository 批次都必须走这个入口，入口失败要保证"外层已写的行不丢、内层失败的不残留"。
14. **预算单位口径**：按次访问的键（`*_attempts`）只接受整数且预留/结算至少 1 单位；`max_runtime_minutes`、`model_cost_usd` 允许小数；其余配额键拒绝小数而不是静默截断。0 单位不是合法预留，缓存命中应记 0 消耗的账行而不是"预留 0"。
15. **写账入口的归属核对与幂等口径**：`budget_ledger`/`budget_reservations` 的每一个写入口（`reserve`、`settle_external`、恢复补记、`record_cache_hit`、`record_quota`）在动手之前都要核对操作归属，**带不带 `reservation_id` 同口径**：① `operations.run_id` 必须等于本账的 run；② 调用方声称的 `operation_type` 必须等于库里登记的类型；③ 访问预算键必须属于该操作类型（`suggest→{suggest_attempts, calibration_suggest_attempts}`、`search→search_attempts`、`body→body_attempts`、`author_posts→author_attempts`）；④ 结算的 `attempt_no` 必须在 `operation_attempts` 里真存在（该行存在即表示访问已发起，`started_at NOT NULL`）。外键只证明 id 曾存在，不证明它是"本次结算声称的那个操作"，因此这四项不能交给 FK 兜底。幂等只承认"同一件事"：同一 `(run_id, operation_id, attempt_no)` 的外部访问若 `budget_key`/`operation_type`/`consumed_units` 全一致则不重复插入（返回"已记过"），任一不符是两笔互相矛盾的账，必须报错拒绝，不得用 `None` 掩盖。任何一条拒绝都发生在写账之前，不产生账本行、不消费别人的预留、不改配额。
16. **正文观察的归属**：`ingest_body` 与联想/卡片观察同口径，幂等键是 `(operation_id, attempt_id, session_id)`，不再是 `(post_id, captured_at)`——fixture 的 `collected_at` 不含 run，两个 run 同序号同尝试会撞出同一时间戳，旧键让后来者永远补不上自己的正文观察。写库前先调 `Repository.require_observation_source()` 核对声称的来源（① 操作存在且 `operations.run_id` 等于本 run；② 声称的 `operation_type` 等于库里登记的类型；③ attempt 存在且归属于该操作；④ `session_id` 非空），任一不符在**任何写入之前**拒绝，不产生观察行也不消费别人的键。`content_validation_count` 同时核对 `body_observations.run_id` 与 `operations.run_id`，两重来源不自洽的行按 0 计。

17. **账跟着访问阶段走**：一次 attempt 的异常收口分三段 —— `not_started`（请求还没交出去）、`access`（请求已交出、结果没回来或在返回前炸）、`local`（结果已返回，只是落盘/入库失败）。**只有 `not_started`，或数据源抛出 `AccessNotStarted`（其唯一语义是"我证明这次调用没碰平台"）才允许把预留退回可用池**；其余一律 `settle_external` 照计一笔，"源在调用中途抛异常"不能自动等同未访问。退款在这里是双重错误：既抹掉一次真实成本，又因为 `release` 只写 `consumed_at`，让被退掉的预留从此对恢复补账（`release_stale_reservations` 只看"已发起且未消费"）隐身，重启也补不回来。每 attempt 至多一笔访问账由结算幂等 + `budget_ledger` 部分唯一索引（第 7 节）共同保证；结算自身失败时不得用记账故障盖住原始异常，预留保持未消费、由恢复按既成事实补记。attempt 的 `error_json` 必须留 `access_phase / access_charged / billing_error`，并写 `attempt_charged_after_failure` 或 `attempt_refunded_not_accessed` 事件供逐条回查。落盘/入库失败也不得伪装成"零成本的第二次机会"：产物已落则 `raw_artifact_count` 照记，观察未落则库里不留半行。

## 7. 索引清单

```
run_keywords(run_id, status)                      run_keywords(run_id, keyword_id) UNIQUE
discovery_edges(run_id, parent_keyword_id)        discovery_edges(run_id, child_keyword_id)
frontier(run_id, status, score DESC)              frontier(run_id, keyword_id, branch_id) UNIQUE
operations(run_id, status)                        operations(run_id, keyword_id, operation_type, revisit_seq) UNIQUE
operation_attempts(operation_id, attempt_no) UNIQUE
budget_ledger(run_id, budget_key)                 budget_reservations(run_id, budget_key)
evidence(run_id, content_version_id)              evidence(run_id, post_id)
cluster_assignments(run_id, need_id)              needs(run_id, statement_kind)
problems(run_id, content_version_id)              problems(run_id, suggestion_version_id)
post_queries(run_id, keyword_id)
body_observations(operation_id, attempt_id, session_id) UNIQUE  body_observations(run_id, state, post_id)
content_family_members(family_id, content_version_id) PK        content_family_members(content_version_id, analysis_version)
needs(run_id, analysis_version, stable_key) UNIQUE WHERE stable_key IS NOT NULL
suggestion_items(observation_id, rank)            raw_artifacts(run_id, sha256)
suggestion_versions(observation_id, rank) UNIQUE  worker_leases(run_id) UNIQUE
budget_ledger(run_id, operation_id, attempt_no) UNIQUE WHERE external_access = 1
control_requests(run_id, applied_at)              run_events(run_id, at)
```

## 8. 展示与统计口径（写入 report.json 的局限）

- 评分只做同一采样条件下的样本内比较；`likesText` 不当搜索需求量，帖子数不等于用户数。
- 发布时间原文无法可靠解析时 `F=unknown`；作者引用缺失时 `A` 分量记 unknown 并重归一化。
- 样本量低于 `scoring.quantile_min_sample` 时分数标 `provisional` 并显示原始计数。
- 覆盖校准只报告冻结候选池的相对覆盖，不宣称平台总体覆盖率。
- 失败、验证码、无内容预算都不计入收敛判据；`converged` 必须写明触发条件。
- 内容验证量**按 run 统计**：只数本次 run 自己有 `completed` 正文观察、且观察行与所挂操作的 `run_id` 都等于本 run 的帖子数；同篇帖子在别的 run 取到过正文不算本 run，来源为 NULL 的历史行也不算（第 3 节 `body_observations`、第 6 节第 16 条）。

## 9. P0-B 落库时对原稿的收紧与偏差（提请复核确认）

实现时发现原稿有几处"写出来但库拦不住"的地方，P0-B 一律往**更严格**的方向补齐；下表同时列出需要同步回契约的少数放宽。

| # | 位置 | 原稿 | 落地 | 理由 |
|---|---|---|---|---|
| 1 | `budget_ledger` | 无 `operation_id`；只写 `CHECK(external_access=1 → attempt_no NOT NULL)` | 增 `operation_id FK NOT NULL`（外部访问行必填）+ `UNIQUE(run_id, operation_id, attempt_no) WHERE external_access=1` | 原口径下同一操作同一次 attempt 重复导入能插两条账，"绝不重复扣费"只停留在文字；部分唯一索引让它变成数据库事实 |
| 2 | `budget_ledger.consumed_units` | `INTEGER NOT NULL`，且 `external_access=0 → consumed_units=0` | `NUMERIC NOT NULL CHECK(>=0)`；非访问行允许带消耗，但仅限 `QUOTA_KEYS`（存储/时长/模型额度），缓存命中与访问键仍必须为 0 | `model_cost_usd` 是美元小数，整型声明会误导（0.01 在整型亲和下不会被截断，但列类型与真实语义不符）；原稿的"非访问必为 0"又让"入库 3 篇"这种配额计量无法记账 |
| 3 | `budget_ledger` 非访问行 | 未说明能否关联操作 | 允许（并可空）`operation_id` | 存储篇数/缓存命中都是某次操作产生的事实，留空会丢审计线索 |
| 4 | `problems.source_version_id` | "FK，指向 suggestion/title/body 的具体版本" | 拆成 `content_version_id` / `suggestion_version_id` 两个可空 FK + `CHECK` 恰好一个非空 + `source_field='suggestion' → suggestion_version_id NOT NULL` | 单列多态外键在 SQLite 里无法指向两张表，写成 `NOT NULL` 整数等于没有完整性，能存幽灵 id |
| 5 | `suggestion_versions` | 只在 evidence 备注里暗示"联想观察也算一种版本" | 正式建表（v2）：`UNIQUE(observation_id, rank)`、`text_sha256 length=64`、FK `RESTRICT` | suggestion 证据也要能被 `Evidence.locate()` 定位与校验哈希，口径必须与帖子内容版本一致 |
| 6 | `raw_artifacts` | `operation_id FK, attempt_id FK`（写法上非空） | 两列改为可空，仅"补导入扫到无法归属的孤儿文件"时使用，并强制写 `artifact_orphaned` 事件 | 孤儿文件必须登记为事实；悄悄丢弃或伪造归属都不可审计 |
| 7 | 迁移层数 | 未规定切分 | v1=身份+任务状态+观察+运行支持（写入层），v2=分析层；`rollback(1)` / `rollback(0)` 各有测试 | 升级与回滚要真的被演练，只有一层就无从验证 |
| 8 | 迁移执行 | 未规定 | 逐语句在显式 `BEGIN` 中执行，失败整段回滚（第 6 节第 9 条） | `executescript()` 自动提交，中途失败会留下半张 schema 且版本号已登记 |
| 9 | `run_events.kind` | 用"例："列举 | 追加 `imported_after_restart, artifact_orphaned, stale_lock_removed, lease_taken_over, forced_takeover, status_changed, resumed, extended, stopped` 等实现用到的取值 | 恢复、worker 接管与强制接管都要留痕；kind 本就不是闭集，故不加 CHECK |
| 10 | 恢复窗口 | "重启后仍为 `in_flight` 且无可靠 operation 结果时标 `uncertain`" | 落实为：`in_flight`+有已完成 attempt → 按最新 `result_state` 回填；`in_flight`+无结果 → `uncertain`；`pending` 一律不动 | `pending` 是正常排队状态，恢复若把它标成 `uncertain` 会污染队列且掩盖真实崩溃窗口 |
| 11 | 预留回收 | 未规定 | 只放开"无归属操作"或"操作已离开 `pending/in_flight`"的预留 | 否则恢复即退款，等于允许越预算发起 |
| 12 | 运行判定 | "必须同时核查 OS 进程锁与未过期租约" | `running = pid 存活 AND 租约未过期且未释放`；`pid_alive` 用 Windows `GetExitCodeProcess`/POSIX `kill(pid,0)`；同 pid 重入算同一主人 | 遗留 lock 文件不是运行证据；接管与僵尸心跳冲突都要有确定的先后规则 |
| 13 | `models.run.BudgetLedgerEntry`（P0-A 已冻结契约） | `consumed_units` 只允许 0/1 语义、非访问行必须为 0 | 改为 `float >= 0`，并按第 2 类/第 3 类键分别校验（外部访问键仍禁止非零非访问消耗） | 与上表 #2/#3 同步；这是**放宽**，需复核确认口径没有被削弱 |
| 14 | `runs` 状态写入 | "非终态不得携带 outcome"未明说 | `set_run_status` 拒绝非终态带 `outcome`；`paused` 必带 `pause_reason`（schema CHECK 同向） | `RunTermination` 要求 paused 也带 reason，原 `repository` 守卫与之冲突，属实现内部矛盾修正 |

复核（2026-10-24 17:23～18:23）指出前 14 条之外还有四处口径缺口，本轮一并补齐：

| # | 位置 | 原稿/首轮实现 | 落地 | 理由 |
|---|---|---|---|---|
| 15 | 事务所有权 | 首轮 `Repository` 批次与 `apply_migrations` 各自用 `with conn:`/隐式提交 | 唯一入口 `db.transaction()`：外层无事务才 `BEGIN/COMMIT`，嵌套降 `SAVEPOINT`；存储层不再出现 `with conn:` | 外层批次里的失败会被内层提交成"部分落盘"，Codex 复现的 `rollback-probe` 残留即此因 |
| 16 | 迁移与外层事务 | 只在 `_atomic` 内部检查 | `apply_migrations`/`rollback` 入口即拒绝未提交事务 | Python 传统隔离模式下 DDL 会**隐式提交**外层批次，晚检查等于先把用户未完成的写事务落盘 |
| 17 | `budget_reservations` | 无键 CHECK、`units` 整型、无 `operation_type` | 键枚举 CHECK + `units NUMERIC NOT NULL CHECK(units>0)` + `operation_type` 枚举列 | 预留要能被结算核对归属（run/键/操作），并支持小数配额预留；0/负预留与跨键结算都是实测缺陷 |
| 18 | `worker_leases` | 仅 `owner_pid` | 增 `owner_token TEXT NOT NULL`，心跳/释放/接管全部按 token 校验；进程存活探测三态 `live/dead/unknown`，锁内容另加 `invalid` | pid 会被复用，僵尸实例的写回会被当成合法心跳；权限受限查不到进程时若当作已死就会双开 worker、双倍扣费 |
| 19 | worker 独占机制 | 首轮用 probe→unlink→`os.link` 抢"归属记录"文件 | 每条锁配一个 `*.guard` 文件，用 OS 字节区间排他锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）**持到退出**；所有权变更只在临界区内改写归属记录，guard 文件永不删除；`force` 不能绕过 guard；半拿状态一律撤回 | 复核（19:57）：两个进程都按旧快照看到同一把 dead 锁时，后到者会 unlink 掉前者刚建好的活锁，两边都"成功"；单纯再读一次 token 仍有检查后删除窗口。只有把变更放进 OS 互斥（进程死了我自动失锁）才没有这个窗口 |
| 20 | `settle_external` 的操作核对（C-07） | 只有带 `reservation_id` 的分支查 `operations`；无预留路径靠外键放行，重复导入一律 `return None` | 每个写账入口（`settle_external`、`_record_access`、`reserve`、`record_cache_hit`、`record_quota`）在写账前核对 `operations.run_id`、库里的实际 `operation_type`、预算键↔类型映射与 `operation_attempts` 里该 attempt 是否存在；同一 attempt 的重放只在 `budget_key`/`operation_type`/`consumed_units` 全一致时幂等，任一不符 `ValueError` | 复核（20:57）实测：`Ledger(conn,'run-a',…).settle_external(operation_id=<run-b 的 suggest 操作>, operation_type=search, attempt_no=1, key='suggest_attempts')` 会成功并扣掉 run-a 的额度（`WRONG_RUN_AND_TYPE_ACCEPTED 1.0`）——外键只保证 id 存在，不保证它是"本次结算声称的那个操作"。而"已存在就返回 None"会把参数冲突的补导入伪装成幂等重复，账本被悄悄改写 |

### 9.1 P0-C 主循环衔接时对原稿的补齐（提请复核确认）

跑通"预留→访问→落盘→入库→结算→归并→奖励→扩展→入队"这条真实链路后，暴露出 7 处单模块测试看不见、只有跨模块才现形的口径缺口（#21～#27），全部往**更严格**方向修；2026-10-26 Codex 独立复核又追加两处，同属这一类"只在跨模块/跨 run 时才暴露"的缺口：**#28（C-09，正文观察跨 run 泄漏）**与 **#29（C-10，访问已发生却被本地异常退款）**：

| # | 位置 | 首轮实现 | 落地 | 实测现象 |
|---|---|---|---|---|
| 21 | `raw_artifacts` 补导入扫描（P0-09） | 把 run 目录下所有未登记文件当"孤儿产物"登记 | `_is_worker_bookkeeping()` 先排除 worker 归属记录与 OS 互斥文件（`worker.lock`、`worker-global.lock`、`*.guard`）及其临时变体 | 重启补导入去 `read_bytes()` 一把正被持有的 `worker.lock.guard`，直接 `PermissionError: [Errno 13]`，恢复路径整条断掉；这些文件本来也不是采集产物 |
| 22 | `PriorityBatch` 语料边界（P0-16） | R/N 用 `len(anchors)+len(explored)` 当 IDF 语料的分界 | 新增 `known_bounds()`，按 `corpus()` 的**去重后**前缀长度给边界 | 锚词与已探索词里有重复文本时语料比长度小，`CharTfidf.cosine` 越界 `IndexError: list index out of range` |
| 23 | `frontier.slot_kind`（P0-17） | 种子行与候选行都继承当前槽的 `slot_kind` | 两者都写 `NULL`（见第 2 节 frontier 的 NULL 规则） | 种子 `slot_kind=high_score` + `score=NULL` → 三个桶都取不到它，run 在第 0 槽就报"没有可调度候选"而暂停；候选继承父槽类型则 8 个槽全变成 `branch_exploration`，60/25/15 退化成一个桶 |
| 24 | 算子扩展的去重基线（P0-15） | `expand(seen_normalized=全部已探索词)`，而"全部"包含本批刚生成的候选自己 | 从 `seen` 里剔除本批新登记的子词（`known_texts`） | 6 个子词登记进 `run_keywords` 却一个都没入队，槽位说明写成"四类算子本次没有产出新候选"，扩散在第 1 层断掉 |
| 25 | 候选登记入参（P0-15） | `register_candidates(..., [candidate for candidate, _ in scored])` 之外还传了评分对象 | 按候选本身登记，`origin` 取候选来源 | `AttributeError: 'PriorityInput' object has no attribute 'keyword'`；顺带把 generated 候选误标成 observed 的风险也在这一步 |
| 26 | 证据 quote 的原文一致性（P0-18/P0-24） | `select_quote()` 在 `normalize_text()` 之后的文本里切子句 | 切分改在**原文**上做，判定才用规范化文本 | `normalize_text` 走 NFKC，把全角 `，` 折成半角 `,`；标题"上海中考志愿不要这样排，我踩坑了"的 quote 变成带 `,` 的串，`Evidence.locate()` 拒绝入库（"quote 在对应内容版本原文中不存在"），整槽崩掉——这正是"证据必须能在原文定位"要防的事 |
| 27 | `sources/fixture.py` 的 skipped 场景（P0-12） | `skipped` 只带出一条 warning，数据集里声明的 `exit_code/error_code/message` 被丢掉 | 新增 `_skip_error()`，联想与卡片两条路径同口径还原成 `ErrorInfo` | 操作层查不到"为什么没采"，`skipped` 与"平台确实没有"在库里长得一样；预算未达、能力缺失、数据集主动跳过三种形态无法区分 |
| 28 | 正文观察的 run 归属（C-09，独立复核发现） | `body_observations` 只有 `post_id + captured_at`，`content_validation_count` 按**全局** `post_id` 连本 run 的 `post_queries` | schema v3 给 `body_observations` 补 `run_id/operation_id/attempt_id/session_id`（可空，附 `UNIQUE(operation_id, attempt_id, session_id)`）；`ingest_body` 改用与联想/卡片同口径的幂等键并在写库前核对来源（第 6 节第 16 条）；计数同时核对观察行与 `operations` 的 `run_id`；升级后来源为 NULL 的历史行保留但不计入 | 复核给的最小复现：A 有 completed 正文、B 只有同篇帖子的卡片 → B 计数 1（应为 0），B 能借 A 的读取过收敛门槛。同一改动的第二处泄漏：fixture 的 `collected_at` 不含 run，同序号同尝试下两个 run 撞出同一 `captured_at`，旧的 `(post_id, captured_at)` 去重键让 B 永远补不上自己的正文观察 |
| 29 | 访问成功后本地异常的退款（C-10，独立复核发现） | `_execute` 把 access、write_artifact、attach 包在同一个 try，任何异常都 `release()` 预留并 `finish_attempt(failed)`，从不结算 | 一次 attempt 分 `not_started / access / local` 三段收口（第 6 节第 17 条）：只有 `not_started` 或数据源抛 `AccessNotStarted` 才退回预留，其余一律 `settle_external` 照计；attempt 的 `error_json` 记 `access_phase/access_charged/billing_error`，并写 `attempt_charged_after_failure` / `attempt_refunded_not_accessed` 事件；结算本身失败不盖住原始异常，预留保持未消费交给恢复补记 | 复核给的最小复现：包装 `source.get_suggestions` 正常返回、patch `engine.write_artifact` 抛 `OSError` → `source_returned=1、charged=0.0、reservations_open=0`，一次已经发生的访问被退款。第二层危害更隐蔽：`release` 只写 `consumed_at`，被退掉的预留对恢复补账（只看"已发起且未消费"）就此隐身，重启也补不回这笔真实成本 |

另记三条实现口径（不算缺陷，供复核对照）：引擎结算**一律显式带 `reservation_id`**（`tests/test_engine_worker.py::test_settlement_carries_the_reservation_it_reserved` 逐笔核对预留与结算同源同额同操作）；有限重试的退避只把 `planned_seconds` 落成事件、fixture 路径不真的 `sleep`，避免测试计时依赖墙钟；引擎的重启缓存命中检查 `_existing_observation()` 现在同样覆盖 `body_observations`，重启后重驱动一个已完成的正文操作走缓存路径返回，不再二次访问、二次扣账。

### 9.2 P0-D 规则分析层对原稿的补齐（schema v4，提请复核确认）

原稿把分析层（v2）的表列齐了，但有三处"要用的事实上面没有"——不补齐就只能靠现场编，正好都是 P0-D 验收要防的那类造假。三处都以 **schema v4** 落库，列一律可空、默认 NULL，历史行不事后伪造：

| # | 位置 | 原稿 | 落地 | 为什么必须这样 |
|---|---|---|---|---|
| 30 | 卡片可见字段（`post_queries`，第 3 节） | 只有 `result_rank`，发布时间/点赞文本只在内存对象上存在 | v4 补 `published_at_text TEXT NULL, likes_text TEXT NULL`，`_ingest_cards` 写入、`replayed_posts()` 读回；引擎恢复路径改调同一个 `replayed_posts()`，不再自己写一份查询 | F（近期持续性）与"点赞≠搜索量"的展示口径都要求能从库里重算出"页面当时显示成什么样"。原口径下丢失后只有两种选择：凭帖子身份编日期，或把"页面没给"和"分析算不出"混成同一个 NULL。两处各写一份重建 SQL 还会让恢复路径与重放路径读出不同事实（这正是第 6 节幂等口径要避免的） |
| 31 | 内容家族的成员（`content_families`，第 4 节） | 只有 `representative_version_id + member_count`，没有成员行 | v4 新增 `content_family_members(family_id FK, content_version_id FK, similarity NULL, analysis_version, created_at, PRIMARY KEY(family_id, content_version_id))` | `member_count` 是一个自说自话的数字：Q 要按"家族去重后有几个独立公开来源"计，没有成员行就无法审计、无法在改判时把版本挪出家族，也无法验证"同一个版本不会同时属于两个家族"。帖子与作者身份照旧不删，家族只表达"这几份文本是同一件内容" |
| 32 | 需求稳定身份（`needs`，第 4 节） | 只有 `canonical_text`，没有跨重跑稳定的键 | v4 补 `stable_key TEXT NULL` + `UNIQUE(run_id, analysis_version, stable_key) WHERE stable_key IS NOT NULL` | `canonical_text` 会因为属性冲突拆分带上 `#conflict[...]` 后缀——那是归并**结果**不是身份；同版本重跑必须命中同一行才谈得上幂等。键只在 `(run_id, analysis_version)` 内唯一，新版本改判写新行、旧行一字不动，这与"分析使用新版本、不改写历史"的要求一致；P0-C 的 `p0-preliminary.1` 历史行 `stable_key` 为 NULL，多条 NULL 不互相冲突 |

### 9.3 分析层落库与幂等口径（P0-22～P0-27，提请复核确认）

实现到"要把结论写进库、还要能被撤销与复核"这一步，又出现五处原稿没规定、但决定了库里那行是不是事实的地方：

| # | 位置 | 原稿/直觉做法 | 落地 | 为什么必须这样 |
|---|---|---|---|---|
| 33 | `phrases` 的版本分区（第 4 节，schema **v5**） | 明细表没有 `analysis_version`（与第 1 节"分析层每表都带版本"自相矛盾）；直觉补列 + `UNIQUE(run_id, analysis_version, text, kind)` | v5 加 `analysis_version TEXT DEFAULT NULL`，唯一性用**部分**索引 `idx_phrases_run_version ... WHERE analysis_version IS NOT NULL`，查找一律用 `IS` 而不是 `=` | 没有版本列，同一个 run 的短语批次和 needs/problems 不同版本分区，report 里的短语统计与需求统计就不是同一次分析。而 SQLite 的 UNIQUE 视 NULL 互不相等：P0-C 留下的历史短语行版本为 NULL，普通 UNIQUE 允许同一 `(run, NULL, text, kind)` 每次重跑多插一条；带版本的行由部分索引约束住，NULL 历史行不参与唯一性——事后给它们填版本才是伪造归属 |
| 34 | 来源类型判定（P0-23，`analysis/extraction.py::_source_type`） | 复用在线层的 `IntentMatcher.source_type`（那里宁可错认求助也不漏提问） | 分析层在 matcher 结果之上加一层修正：命中经验分享标记且没有显式求助措辞的，从 `user_help` 降成 `experience_share`；**在线层不动** | 计划 §5.2 写明"经验分享不自动算用户求助"，但在线层的口径已经进了 P0-C 的簇计数与依赖它的奖励；在两处用同一函数就等于要么改历史、要么让分析层违背计划。差异由 `extractor_version` 分开（`p0-rule-extract.2` vs `p0-preliminary-extract.1`），两层各自可复核 |
| 35 | 分句标点（P0-23，`extraction.segments()`） | 按标点切句、片段只留文字 | 句读是断点，**疑问号留在句子内**，只剩标点的片段丢弃 | 问号被剥掉后，"名额分配到底怎么排"这类句子在 `has_help_marker` 与来源判定里读不到疑问线索，会抽成一条没有问题记号的"问题"，需求图里就出现一堆不像疑问的展示需求 |
| 36 | 证据入库前的定位复核（P0-24，`store.insert_evidence()`） | 构造 `Evidence` 时已 `locate()` 过一次，入库直接写 | 写库前再用**库里那一行版本原文**复核一次，定位不上 `ValueError` 拒绝，不标记放行 | 构造时的容器是内存对象，写进库的版本 id 是另一个数——两者对不上就是"引用版本 A、文本来自 B"的失效引用。这种行一旦入库，需求详情里的"原文"永远点开对不上，而它看起来完全正常 |
| 37 | 归并撤销（P0-25/26，`cluster_assignments`） | "撤销"读成删行：删归属、删空簇、顺手删需求 | 撤销只写 `superseded_at` 并把 `auto_merged` 置 0；`needs/problems/evidence` 一律不删，空簇保留行 | `run_events` 与已发出的导出都按 id 引用这些行，删行等于把历史引用改成指向不存在的东西；保留空簇才能解释"这批曾被归进来、后来被推翻" |
| 38 | 内容家族的键（P0-27，`store.find_content_family()`） | 第 4 节只写家族有代表版本与 `member_count`，没规定身份 | 身份 = `(representative_version_id, FAMILY_METHOD, analysis_version)`；表里**没有** `run_id`、也没有 key/hash 列 | 一件内容是跨 run 的事实（与 `posts`、`post_content_versions` 同口径），"本 run 见过它"由本 run 的 evidence 决定。代表版本取成员里最小 id，而 id 只递增，新增副本永远拿到更大的 id，代表不漂移，这个键才跨批次稳定；"按 run 数家族"是 Q 的口径，不是表的分区 |
| 39 | 问题行的版本维度（P0-23/24，`store.find_problem()`） | 直觉给 `problems` 也加 `analysis_version`，并挂 `keyword_id` | 自然键 = `(run_id, canonical_text, source_field, extractor_version, 内容/联想版本 id)`；主题与关键词从 `evidence.keyword_id` 走 | `problems` 是抽取器的产物，不是归并的产物：同一抽取器读同一文本必然得出同一条问题，跨分析版本复用才是对的；改规则改的是 `extractor_version`，重跑得到另一批行而不是覆盖上一批。主题挂在证据上，一条问题若被两个关键词观察到也不会被算成两条 |

### 9.4 展示与导出安全口径（P0-28/P0-29，提请复核确认）

五个文件是交付给人复核的，因此这一节全部是"别人按我们的文件复核时会被骗"的口径：

| # | 位置 | 直觉做法 | 落地 | 为什么必须这样 |
|---|---|---|---|---|
| 40 | 需求图的自检码与排行榜 | 自检只报告，不动展示 | `DISPLAY_BLOCKING_CODES` 里的码（`need_without_evidence`、`evidence_row_missing`、`evidence_quote_broken`、`suggestion_as_post_fact`、`candidate_uses_post_evidence`）让需求**掉出** `needs.csv`；`orphan_evidence`、`problem_in_several_clusters` 只留审计不挡展示 | "所有展示需求须有有效证据"是硬要求：点不开原文或来源类型造假的需求不配占名次。反过来，一条没被任何需求引用的证据行是审计信息，删掉它反而会破坏引用它的历史 |
| 41 | 失效引用落到哪个节点 | 自检违规一律按 `node_id` 回查需求 | `evidence_quote_broken` 挂在**证据节点**上，需求的 id 只在 `detail.need_node` 里；`_broken_references()` 两种落点都认，任一证据坏掉就让整条需求不展示 | 只认前一种的话，复核改坏一条证据的 quote，需求照样带着分数出现在排行榜上——"证据失效但结论不退"正是这轮验收点名要防的 |
| 42 | GraphML 的 id 与属性类型 | 节点 id 直接用业务标识 `topic:上海中考`；同名属性按首次出现的类型声明 | id 用 `<类型前缀>_<序号>`（`kw_/topic_/prob_/need_/ev_/post_/sugg_`），精确身份另存 `ref` 属性；每个属性只声明一种 `attr.type`，复合值（列表/字典）与类型不一致的同名属性统一 JSON 化成 `string`，被降级的属性在 report 里点名 | `topic:` 后面跟中文不是合法的 XML `ID`，别家解析器直接报错；同名属性一行 int 一行 bool 会让读侧的类型推断随机失败。宁可可读地降级，也不静默改变类型 |
| 43 | CSV 单元格 | 按 `csv` 模块原样写 | 以 `=`、`+`、`-`、`@`、制表符、回车开头的值前置撇号；NUL 换成 U+FFFD；字典/列表按排序键 JSON；反读校验逐格比对 | 帖子标题本身就是用户输入，`=HYPERLINK(...)` 在电子表格里会被当公式执行；反读必须能原样读回，否则"转义吃掉了数据"和"文件被改过"分不开 |
| 44 | `report.json` 的自引用 | 报告登记自己的哈希、自己的写入事件与 `file_written` | 三者都排除：`EXPORT_SELF_EVENTS`（`analysis_export`、`export_verification_failed`）不进统计与异常，本次这五个路径不进 `exported.history`，`LandedFile.as_dict()` 不含 `file_written` | 报告若包含自己那次写入的痕迹，第二次导出内容必然变——等于把幂等重跑变成伪冲突，反读校验也永远追不上自己那条事件。落盘与否仍由 `ExportResult.files[].written` 交给调用方 |
| 45 | 配置快照不全时的预算 | 拿不到快照就按默认阈值猜上限 | `budget.keys[].limit` 留空并置 `limits_from_config=false`，同时在 `limitations` 里说明"当前 run 的 config_json 不是完整快照" | 把猜出来的上限写进交付文件，读者会以为那是验收过的预算；留空 + 局限说明才复核得动 |

### 9.5 编排口径（P0-22～29 串成一次调用，提请复核确认）

| # | 位置 | 说明 |
|---|---|---|
| 46 | `analysis/pipeline.py::run_analysis()` | 顺序是硬约束：语料 → 短语发现 → 抽取入库 → 归并（只认库里的 `problems` 行）→ 评分（只认库里的 `needs`/`evidence` 行）→ 双图与自检 → 导出与反读。后一层不接受前一层内存里的对象，否则导出内容与数据库分成两套真相。整批共用一个时间戳；参数全部来自 `Settings.nlp` / `Settings.scoring`，词典与意图规则按配置路径加载，缺文件直接报错不降级。 |
| 47 | 历史与预算边界 | 编排拒绝 `analysis_version=p0-preliminary.1` 与 `extractor_version=p0-preliminary-extract.1`（这两份常量是 P0-C 在线层自己的地盘，补写进去等于改写已复核过的在线簇与依赖它的 `branch_stats` 奖励）；分析是纯本地计算，不写 `budget_ledger`/`budget_reservations` 一行。同一分析版本重放：业务行不新增，但批次事件确实变多了，report 内容随之改变，`ExportConflict` 要求显式换 `snapshot`，绝不覆盖已登记的历史导出。歧义种子与地域词表是数据集声明的、库里没有副本，需要数据集专属口径时由调用方把装配好的 `AnalysisRules` 传进来。 |

### 9.6 C-15：观察标题的版本引用不许用时间猜（schema v7，提请复核确认）

| # | 位置 | 原口径（错） | 落地 | 为什么必须这样 |
|---|---|---|---|---|
| 48 | `post_queries.title_version_id`（第 3 节）与 `discovery.ingest.title_version_for_observation` | 重放/语料/评分按"`captured_at <= observed_at` 的最新标题版本"推断当次观察看见的是哪一版 | v7 给 `post_queries` 补可空列 `title_version_id FK`（`REFERENCES post_content_versions(id)`，默认 NULL），`_ingest_cards` 写入本次观察实际对应的版本 id；`replayed_posts`、`corpus.title_units`（走同一个 `replayed_posts`）与 `scoring._title_visible_sources` 统一走 `title_version_for_observation`：有显式引用按引用取，NULL 历史引用不再"取最新"（见 #49） | 版本表按 `(post_id, field, text_sha256)` 去重：A→B→A 的第三次观察复用 A 的旧行、旧 `captured_at`，时间推断必然把第三次读成 B（Codex 探针 A/B/A 与同时间不同会话两例都错）。原始观察事实不能被时间猜测替换；同样不许把 A 的 `captured_at` 改成第三次来"修"回放——那会破坏第一次观察。历史行引用为 NULL 不伪造、不删旧行；升级/回滚对称（v7 只加/删这一列） |
| 49 | C-15 r3：`title_version_for_observation` 的 NULL 引用兼容与来源自洽 | r2 兼容口径对 NULL 引用取"`observed_at` 前最新"，多候选仍确定地给一版 | 真实 V6 的 A→B→A 升级后第三次仍被回放成 B 并进入 corpus（Codex `2026-10-27-c15-legacy-codex.py` 退出 1）。改为：只有观察时刻前**唯一**已捕获版本才作可解释推断返回，多候选一律 `(NULL, NULL)` unknown；corpus/`_title_visible_sources` 见 unknown 即跳过，不作确定语料/证据/S 会话。正文来源聚合（`scoring._body_observation_sources` 与 `corpus.body_units`）补齐自洽：`op.run_id = bo.run_id`、`op.operation_type='body'`、`att.operation_id = bo.operation_id`，只按 `bo.run_id`+`JOIN op.id` 取关键词会认领别的 run 借来的操作与挂错操作的尝试 | "过去出现过"不证明"这次看见过"：无可靠原始产物可恢复精确关联时，猜"最新"就是把猜测标题做成确定事实。唯一候选可解释推断与多候选不确定必须区分；引用 NULL/失效/不自洽都不落成别帖、别 run 的事实。不改写旧版本 `captured_at`、不伪造指针；有证据（显式引用/唯一候选）才落，无证据留 unknown（提请复核确认） |
