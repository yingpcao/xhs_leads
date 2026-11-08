"""schema 迁移（P0-06）：空库建库、升级、回滚都要可执行、可验证。

版本划分：
- v1：全局身份 + 任务状态 + 观察记录 + 运行支持（P0-B 就要写入的表）
- v2：分析结果层（`phrases/problems/needs/evidence/cluster_assignments/content_families`，
  P0-D 才写入）——分成两级才真正测到"从上一版本库升级"和回滚路径。

列、逻辑键与索引以 `docs/DATA_MODEL.md` 为准；CHECK 里的取值直接来自 `models.enums`，
这样契约与 schema 不会各自漂移。`evidence` 的"联想观察也算一种版本"在文档里没有落成表，
本迁移用 `suggestion_versions` + 双外键二选一实现，差异已在 DATA_MODEL 第 4 节标注待复核。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from xhs_discovery._version import CODE_VERSION
from xhs_discovery.models.common import utc_now
from xhs_discovery.models.enums import (
    CalibrationSampleKind,
    CandidateOrigin,
    ControlActionType,
    OperationState,
    OperationType,
    PauseReason,
    PostSourceType,
    ResultState,
    RunStatus,
    SlotKind,
    SourceField,
    StatementKind,
    TerminalOutcome,
)
from xhs_discovery.models.run import (
    ACCESS_BUDGET_KEYS,
    BUDGET_KEYS as MODEL_BUDGET_KEYS,
    QUOTA_BUDGET_KEYS,
)


class SchemaError(RuntimeError):
    """版本不符（含"库比代码新"）或迁移语句失败：宁可拒绝打开，也不带部分 schema 运行。"""


def _values(enum_cls_or_seq) -> str:
    items = (getattr(member, "value", member) for member in enum_cls_or_seq)
    return ", ".join(f"'{item}'" for item in items)


def _in(column: str, enum_cls) -> str:
    return f"{column} IN ({_values(enum_cls)})"


# 与 config.BudgetSection 的分项预算键一致（DATA_MODEL 第 2 节）
# 预算键的语义以 models.run 为唯一来源，schema 里的 CHECK 只是它的镜像。
ACCESS_KEYS = ACCESS_BUDGET_KEYS
QUOTA_KEYS = QUOTA_BUDGET_KEYS
BUDGET_KEYS = MODEL_BUDGET_KEYS


_V1_IDENTITY = f"""
CREATE TABLE keywords (
    id INTEGER PRIMARY KEY,
    normalized_text TEXT NOT NULL UNIQUE,
    first_seen_at TEXT NOT NULL,
    is_ambiguous_seed INTEGER NOT NULL DEFAULT 0,
    CHECK (is_ambiguous_seed IN (0, 1))
);
CREATE TABLE keyword_aliases (
    id INTEGER PRIMARY KEY,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    raw_text TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(keyword_id, raw_text)
);
CREATE TABLE authors (
    id INTEGER PRIMARY KEY,
    public_ref TEXT UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('public_ref', 'unknown')),
    relevant_post_count INTEGER NOT NULL DEFAULT 0 CHECK (relevant_post_count >= 0),
    CHECK ((kind = 'unknown' AND public_ref IS NULL)
        OR (kind = 'public_ref' AND public_ref IS NOT NULL))
);
CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    note_id TEXT NOT NULL UNIQUE,
    access_url TEXT NOT NULL CHECK (length(access_url) > 0),
    first_seen_at TEXT NOT NULL,
    author_id INTEGER REFERENCES authors(id) ON DELETE RESTRICT
);
CREATE TABLE post_content_versions (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE RESTRICT,
    field TEXT NOT NULL CHECK (field IN ('title', 'body')),
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    captured_at TEXT NOT NULL,
    is_missing INTEGER NOT NULL DEFAULT 0 CHECK (is_missing IN (0, 1)),
    UNIQUE(post_id, field, text_sha256)
);
"""

_V1_RUN_STATE = f"""
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    seed_raw TEXT NOT NULL,
    seed_normalized TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('fixture', 'live')),
    status TEXT NOT NULL CHECK ({_in('status', RunStatus)}),
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ({_values(TerminalOutcome)})),
    pause_reason TEXT CHECK (pause_reason IS NULL OR pause_reason IN ({_values(PauseReason)})),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    code_version TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    random_seed INTEGER NOT NULL,
    synthetic_fixture INTEGER NOT NULL CHECK (synthetic_fixture IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    budget_revision INTEGER NOT NULL DEFAULT 1 CHECK (budget_revision >= 1),
    CHECK (mode <> 'fixture' OR synthetic_fixture = 1),
    CHECK (status NOT IN ('completed', 'failed') OR outcome IS NOT NULL),
    CHECK (status <> 'paused' OR pause_reason IS NOT NULL)
);
CREATE TABLE budget_revisions (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    added_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE branches (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    branch_key TEXT NOT NULL,
    display_label TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, branch_key)
);
CREATE TABLE branch_stats (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    branch_key TEXT NOT NULL,
    trials INTEGER NOT NULL DEFAULT 0 CHECK (trials >= 0),
    total_reward REAL NOT NULL DEFAULT 0.0,
    stale_streak INTEGER NOT NULL DEFAULT 0 CHECK (stale_streak >= 0),
    min_validation_share_left INTEGER NOT NULL DEFAULT 0 CHECK (min_validation_share_left >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, branch_key)
);
CREATE TABLE run_keywords (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    parent_keyword_id INTEGER REFERENCES keywords(id) ON DELETE RESTRICT,
    depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
    branch_id INTEGER REFERENCES branches(id) ON DELETE RESTRICT,
    origin TEXT NOT NULL CHECK (origin IN ('observed', 'generated', 'seed')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'explored', 'skipped', 'irrelevant', 'stale')),
    revisit_count INTEGER NOT NULL DEFAULT 0 CHECK (revisit_count >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    irrelevant_reason TEXT,
    UNIQUE(run_id, keyword_id),
    CHECK (status <> 'irrelevant' OR irrelevant_reason IS NOT NULL),
    CHECK (parent_keyword_id IS NULL OR parent_keyword_id <> keyword_id)
);
"""

_V1_OPERATIONS = f"""
CREATE TABLE operations (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    operation_type TEXT NOT NULL CHECK ({_in('operation_type', OperationType)}),
    revisit_seq INTEGER NOT NULL DEFAULT 0 CHECK (revisit_seq >= 0),
    status TEXT NOT NULL CHECK ({_in('status', OperationState)}),
    branch_id INTEGER REFERENCES branches(id) ON DELETE RESTRICT,
    slot_kind TEXT CHECK (slot_kind IS NULL OR slot_kind IN ({_values(SlotKind)})),
    sample_kind TEXT CHECK (sample_kind IS NULL OR sample_kind IN ({_values(CalibrationSampleKind)})),
    first_started_at TEXT,
    last_finished_at TEXT,
    UNIQUE(run_id, keyword_id, operation_type, revisit_seq)
);
CREATE TABLE operation_attempts (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE RESTRICT,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result_state TEXT CHECK (result_state IS NULL OR result_state IN ({_values(ResultState)})),
    error_code TEXT,
    error_json TEXT,
    exit_code INTEGER,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    raw_artifact_count INTEGER NOT NULL DEFAULT 0 CHECK (raw_artifact_count >= 0),
    UNIQUE(operation_id, attempt_no)
);
CREATE TABLE discovery_edges (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    parent_keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    child_keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    rank INTEGER,
    session_id TEXT,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL CHECK ({_in('origin', CandidateOrigin)}),
    operation_id INTEGER REFERENCES operations(id) ON DELETE RESTRICT,
    UNIQUE(run_id, parent_keyword_id, child_keyword_id, session_id, origin),
    CHECK (parent_keyword_id <> child_keyword_id)
);
CREATE TABLE frontier (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    score REAL,
    score_breakdown_json TEXT,
    slot_kind TEXT CHECK (slot_kind IS NULL OR slot_kind IN ({_values(SlotKind)})),
    sample_kind TEXT NOT NULL CHECK ({_in('sample_kind', CalibrationSampleKind)}),
    selection_reason TEXT NOT NULL,
    selection_probability REAL,
    queued_at TEXT NOT NULL,
    attempted_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'done', 'dropped')),
    UNIQUE(run_id, keyword_id, branch_id)
);
"""

_V1_OBSERVATIONS = f"""
CREATE TABLE suggestion_observations (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE RESTRICT,
    attempt_id INTEGER NOT NULL REFERENCES operation_attempts(id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK ({_in('state', ResultState)}),
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    source_name TEXT NOT NULL,
    source_version TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
    UNIQUE(operation_id, attempt_id, session_id)
);
CREATE TABLE suggestion_items (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES suggestion_observations(id) ON DELETE RESTRICT,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    text_raw TEXT NOT NULL,
    text_normalized TEXT NOT NULL,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    UNIQUE(observation_id, rank)
);
CREATE TABLE post_observations (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE RESTRICT,
    attempt_id INTEGER NOT NULL REFERENCES operation_attempts(id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK ({_in('state', ResultState)}),
    card_count INTEGER NOT NULL CHECK (card_count >= 0),
    source_name TEXT NOT NULL,
    source_version TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
    UNIQUE(operation_id, attempt_id, session_id)
);
CREATE TABLE post_queries (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    post_observation_id INTEGER NOT NULL REFERENCES post_observations(id) ON DELETE RESTRICT,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE RESTRICT,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    result_rank INTEGER NOT NULL CHECK (result_rank >= 1),
    UNIQUE(run_id, post_observation_id, post_id)
);
CREATE TABLE body_observations (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK ({_in('state', ResultState)}),
    body_is_empty INTEGER CHECK (body_is_empty IS NULL OR body_is_empty IN (0, 1)),
    content_version_id INTEGER REFERENCES post_content_versions(id) ON DELETE RESTRICT,
    captured_at TEXT NOT NULL,
    CHECK (state <> 'completed' OR content_version_id IS NOT NULL OR body_is_empty IS NOT NULL),
    CHECK (state = 'completed' OR body_is_empty IS NULL)
);
"""

_V1_SUPPORT = f"""
CREATE TABLE budget_reservations (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    budget_key TEXT NOT NULL CHECK (budget_key IN ({_values(BUDGET_KEYS)})),
    -- units 用 NUMERIC：外部访问按整次预留，但运行分钟/模型美元的预留天然是小数
    units NUMERIC NOT NULL CHECK (units > 0),
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    operation_id INTEGER REFERENCES operations(id) ON DELETE RESTRICT,
    operation_type TEXT CHECK (operation_type IS NULL OR operation_type IN ({_values(OperationType)}))
);
CREATE TABLE budget_ledger (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    operation_id INTEGER REFERENCES operations(id) ON DELETE RESTRICT,
    budget_key TEXT NOT NULL CHECK (budget_key IN ({_values(BUDGET_KEYS)})),
    operation_type TEXT CHECK (operation_type IS NULL OR operation_type IN ({_values(OperationType)})),
    attempt_no INTEGER CHECK (attempt_no IS NULL OR attempt_no >= 1),
    -- consumed_units 用 NUMERIC：外部访问按整次计，model_cost_usd 是美元小数，整型声明会误导读者。
    consumed_units NUMERIC NOT NULL CHECK (consumed_units >= 0),
    external_access INTEGER NOT NULL CHECK (external_access IN (0, 1)),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (external_access <> 1 OR (attempt_no IS NOT NULL AND operation_id IS NOT NULL
        AND consumed_units >= 1)),
    -- 外部访问为 0 时只有"非访问类配额"（存储/时长/模型额度）可以带消耗；
    -- 缓存命中依旧记 0 外部单元，规则见 DATA_MODEL 第 2 节 budget_ledger。
    -- 非访问行允许带 operation_id：入库篇数/分钟数本来就挂在某次操作上，是审计线索不是计费凭证。
    CHECK (external_access <> 0 OR consumed_units = 0
        OR budget_key IN ({_values(QUOTA_KEYS)}))
);
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT
);
CREATE TABLE control_requests (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK ({_in('action', ControlActionType)}),
    requested_at TEXT NOT NULL,
    applied_at TEXT,
    suggest_budget_delta INTEGER CHECK (suggest_budget_delta IS NULL OR suggest_budget_delta >= 0),
    search_budget_delta INTEGER CHECK (search_budget_delta IS NULL OR search_budget_delta >= 0),
    note TEXT,
    CHECK (action <> 'extend'
        OR COALESCE(suggest_budget_delta, 0) > 0
        OR COALESCE(search_budget_delta, 0) > 0)
);
CREATE TABLE worker_leases (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE RESTRICT,
    owner_pid INTEGER NOT NULL,
    -- PID 会被复用：租约归属还要一个本次进程生成的随机令牌，release/heartbeat 只认同一持有者
    owner_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE raw_artifacts (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    operation_id INTEGER REFERENCES operations(id) ON DELETE RESTRICT,
    attempt_id INTEGER REFERENCES operation_attempts(id) ON DELETE RESTRICT,
    rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('stdout_json', 'stderr_log', 'raw_response', 'export')),
    created_at TEXT NOT NULL,
    UNIQUE(rel_path, sha256)
);
CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    status TEXT NOT NULL CHECK ({_in('status', ResultState)}),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cost_usd REAL CHECK (cost_usd IS NULL OR cost_usd >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_json TEXT,
    output_rel_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('keywords_csv', 'needs_csv', 'evidence_csv', 'graphml', 'report_json')),
    rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    UNIQUE(run_id, kind, rel_path)
);
CREATE TABLE run_retention (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE RESTRICT,
    raw_artifact_days INTEGER NOT NULL CHECK (raw_artifact_days >= 0),
    export_days INTEGER NOT NULL CHECK (export_days >= 0),
    note TEXT
);
"""

_V1_INDEXES = """
CREATE INDEX idx_run_keywords_status ON run_keywords(run_id, status);
CREATE INDEX idx_discovery_parent ON discovery_edges(run_id, parent_keyword_id);
CREATE INDEX idx_discovery_child ON discovery_edges(run_id, child_keyword_id);
CREATE INDEX idx_frontier_pick ON frontier(run_id, status, score DESC);
CREATE INDEX idx_operations_status ON operations(run_id, status);
CREATE INDEX idx_attempts_operation ON operation_attempts(operation_id, attempt_no);
CREATE INDEX idx_ledger_run_key ON budget_ledger(run_id, budget_key);
CREATE INDEX idx_reservations_run_key ON budget_reservations(run_id, budget_key);
CREATE INDEX idx_suggestion_items_rank ON suggestion_items(observation_id, rank);
CREATE INDEX idx_post_queries_keyword ON post_queries(run_id, keyword_id);
CREATE INDEX idx_raw_artifacts_run_sha ON raw_artifacts(run_id, sha256);
CREATE INDEX idx_run_events_run_at ON run_events(run_id, at);
CREATE INDEX idx_control_pending ON control_requests(run_id, applied_at);
CREATE UNIQUE INDEX idx_ledger_external_idem
    ON budget_ledger(run_id, operation_id, attempt_no)
    WHERE external_access = 1 AND operation_id IS NOT NULL;
"""

_V1_ROLLBACK = """
DROP TABLE IF EXISTS run_retention;
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS model_calls;
DROP TABLE IF EXISTS raw_artifacts;
DROP TABLE IF EXISTS worker_leases;
DROP TABLE IF EXISTS control_requests;
DROP TABLE IF EXISTS run_events;
DROP TABLE IF EXISTS budget_ledger;
DROP TABLE IF EXISTS budget_reservations;
DROP TABLE IF EXISTS frontier;
DROP TABLE IF EXISTS discovery_edges;
DROP TABLE IF EXISTS operation_attempts;
DROP TABLE IF EXISTS operations;
DROP TABLE IF EXISTS run_keywords;
DROP TABLE IF EXISTS branch_stats;
DROP TABLE IF EXISTS branches;
DROP TABLE IF EXISTS budget_revisions;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS body_observations;
DROP TABLE IF EXISTS post_queries;
DROP TABLE IF EXISTS post_observations;
DROP TABLE IF EXISTS suggestion_items;
DROP TABLE IF EXISTS suggestion_observations;
DROP TABLE IF EXISTS post_content_versions;
DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS authors;
DROP TABLE IF EXISTS keyword_aliases;
DROP TABLE IF EXISTS keywords;
"""

# --------------------------------------------------------------------------- v2

_V2_ANALYSIS = f"""
CREATE TABLE suggestion_versions (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES suggestion_observations(id) ON DELETE RESTRICT,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    captured_at TEXT NOT NULL,
    UNIQUE(observation_id, rank)
);
CREATE TABLE content_families (
    id INTEGER PRIMARY KEY,
    representative_version_id INTEGER NOT NULL REFERENCES post_content_versions(id) ON DELETE RESTRICT,
    method TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count >= 1),
    analysis_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE phrases (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    text TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('word', 'ngram', 'pmi_phrase')),
    score REAL,
    freq INTEGER CHECK (freq IS NULL OR freq >= 0),
    source_version_id INTEGER REFERENCES post_content_versions(id) ON DELETE RESTRICT,
    extractor_version TEXT NOT NULL,
    UNIQUE(run_id, text, kind, source_version_id)
);
CREATE TABLE problems (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    canonical_text TEXT NOT NULL,
    -- 与 evidence 同一套做法：来源版本只会指向两张版本表之一，用双 FK + "恰好一个"约束，
    -- 而不是一个没有外键保护的万能 source_version_id（裸整数无法阻止指向不存在的行）。
    content_version_id INTEGER REFERENCES post_content_versions(id) ON DELETE RESTRICT,
    suggestion_version_id INTEGER REFERENCES suggestion_versions(id) ON DELETE RESTRICT,
    source_field TEXT NOT NULL CHECK ({_in('source_field', SourceField)}),
    source_type TEXT NOT NULL CHECK ({_in('source_type', PostSourceType)}),
    topic TEXT, object TEXT, intent TEXT, pain_point TEXT,
    location TEXT, year TEXT, polarity TEXT,
    extraction_method TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    counts_as_valid_problem_cluster INTEGER NOT NULL CHECK (counts_as_valid_problem_cluster IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK ((content_version_id IS NULL) <> (suggestion_version_id IS NULL)),
    CHECK (source_field <> 'suggestion' OR suggestion_version_id IS NOT NULL)
);
CREATE TABLE needs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    canonical_text TEXT NOT NULL,
    statement_kind TEXT NOT NULL CHECK ({_in('statement_kind', StatementKind)}),
    category TEXT,
    evidence_status TEXT NOT NULL CHECK (evidence_status IN ('verified', 'inferred', 'unverified_candidate')),
    score REAL,
    score_json TEXT,
    evidence_complete_json TEXT,
    analysis_version TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (statement_kind <> 'solution_idea' OR evidence_status = 'unverified_candidate')
);
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    source_field TEXT NOT NULL CHECK ({_in('source_field', SourceField)}),
    quote TEXT NOT NULL,
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end >= char_start),
    content_version_id INTEGER REFERENCES post_content_versions(id) ON DELETE RESTRICT,
    suggestion_version_id INTEGER REFERENCES suggestion_versions(id) ON DELETE RESTRICT,
    container_sha256 TEXT NOT NULL CHECK (length(container_sha256) = 64),
    operation_id INTEGER REFERENCES operations(id) ON DELETE RESTRICT,
    attempt_no INTEGER,
    session_id TEXT NOT NULL,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE RESTRICT,
    post_id INTEGER REFERENCES posts(id) ON DELETE RESTRICT,
    note_id TEXT,
    extractor_version TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK ((content_version_id IS NULL) <> (suggestion_version_id IS NULL)),
    CHECK (source_field = 'suggestion'
        OR (post_id IS NOT NULL AND note_id IS NOT NULL))
);
CREATE TABLE cluster_assignments (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE RESTRICT,
    need_id INTEGER NOT NULL REFERENCES needs(id) ON DELETE RESTRICT,
    method TEXT NOT NULL,
    similarity REAL CHECK (similarity IS NULL OR (similarity >= 0 AND similarity <= 1)),
    attributes_conflict_json TEXT,
    auto_merged INTEGER NOT NULL CHECK (auto_merged IN (0, 1)),
    analysis_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(run_id, problem_id, analysis_version),
    CHECK (auto_merged = 0 OR similarity IS NOT NULL)
);
"""

_V2_INDEXES = """
CREATE INDEX idx_evidence_version ON evidence(run_id, content_version_id);
CREATE INDEX idx_evidence_post ON evidence(run_id, post_id);
CREATE INDEX idx_cluster_need ON cluster_assignments(run_id, need_id);
CREATE INDEX idx_needs_kind ON needs(run_id, statement_kind);
CREATE INDEX idx_problems_version ON problems(run_id, content_version_id);
CREATE INDEX idx_problems_suggestion_version ON problems(run_id, suggestion_version_id);
"""

_V2_ROLLBACK = """
DROP TABLE IF EXISTS cluster_assignments;
DROP TABLE IF EXISTS evidence;
DROP TABLE IF EXISTS needs;
DROP TABLE IF EXISTS problems;
DROP TABLE IF EXISTS phrases;
DROP TABLE IF EXISTS content_families;
DROP TABLE IF EXISTS suggestion_versions;
"""

# --------------------------------------------------------------------------- v3

# C-09：正文观察必须能回答"这次读取属于哪个 run、哪次操作、第几次尝试"。
# 帖子身份（posts）是全局的，内容版本也可以跨 run 共用，但"我取到过正文"是一件按 run 发生的事：
# 只靠 post_id + captured_at 既挡不住 run B 借用 run A 的正文算自己的内容验证量，
# 也会让同帖同时戳的两次不同 run 读取互相吞掉。列可空是有意的：老库升级上来的历史行
# 无法确定归属，宁可留 NULL（不计入收敛，见 DATA_MODEL 第 2 节），也不伪造来源。
_V3_BODY_PROVENANCE = """
ALTER TABLE body_observations ADD COLUMN run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT DEFAULT NULL;
ALTER TABLE body_observations ADD COLUMN operation_id INTEGER REFERENCES operations(id) ON DELETE RESTRICT DEFAULT NULL;
ALTER TABLE body_observations ADD COLUMN attempt_id INTEGER REFERENCES operation_attempts(id) ON DELETE RESTRICT DEFAULT NULL;
ALTER TABLE body_observations ADD COLUMN session_id TEXT DEFAULT NULL;
CREATE UNIQUE INDEX idx_body_observation_attempt ON body_observations(operation_id, attempt_id, session_id);
CREATE INDEX idx_body_observations_run ON body_observations(run_id, state, post_id);
"""

_V3_ROLLBACK = """
DROP INDEX IF EXISTS idx_body_observations_run;
DROP INDEX IF EXISTS idx_body_observation_attempt;
ALTER TABLE body_observations DROP COLUMN session_id;
ALTER TABLE body_observations DROP COLUMN attempt_id;
ALTER TABLE body_observations DROP COLUMN operation_id;
ALTER TABLE body_observations DROP COLUMN run_id;
"""

# --------------------------------------------------------------------------- v4

# P0-D 的规则分析需要三件 v1/v2 没落下来的事实，缺任何一件都只能靠现场编：
# 1. 卡片可见字段（发布时间文本、点赞文本）此前只在内存里过一遍就丢了。F（近期持续性）
#    与"点赞数不是搜索量、只是页面可见文本"的展示口径，都要求分析能只从库里重算出
#    "页面当时显示的是什么"；不落原样文本，就只能把"页面没给"和"分析算不出"混成一个 NULL。
# 2. `content_families` 只有代表版本 + 成员数：Q 要按"家族去重后有几个独立公开来源"计，
#    没有成员行就既无法审计这个数字怎么来的，也无法在改判时把某个版本挪出家族。
# 3. `needs` 需要跨重跑稳定的身份。`canonical_text` 会因为属性冲突拆分而带 `#conflict[...]`
#    后缀，是结果不是身份；同一 `analysis_version` 重跑必须命中同一行需求，才谈得上幂等。
# 新增列一律可空、默认 NULL：v1～v3 的历史行无法事后补上来源，伪造比留空更糟。
_V4_ANALYSIS_FACTS = """
ALTER TABLE post_queries ADD COLUMN published_at_text TEXT DEFAULT NULL;
ALTER TABLE post_queries ADD COLUMN likes_text TEXT DEFAULT NULL;
CREATE TABLE content_family_members (
    family_id INTEGER NOT NULL REFERENCES content_families(id) ON DELETE RESTRICT,
    content_version_id INTEGER NOT NULL REFERENCES post_content_versions(id) ON DELETE RESTRICT,
    similarity REAL CHECK (similarity IS NULL OR (similarity >= 0 AND similarity <= 1)),
    analysis_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (family_id, content_version_id)
);
CREATE INDEX idx_family_members_version ON content_family_members(content_version_id, analysis_version);
ALTER TABLE needs ADD COLUMN stable_key TEXT DEFAULT NULL;
CREATE UNIQUE INDEX idx_needs_stable_key ON needs(run_id, analysis_version, stable_key)
    WHERE stable_key IS NOT NULL;
"""

_V4_ROLLBACK = """
DROP INDEX IF EXISTS idx_needs_stable_key;
DROP INDEX IF EXISTS idx_family_members_version;
DROP TABLE IF EXISTS content_family_members;
ALTER TABLE needs DROP COLUMN stable_key;
ALTER TABLE post_queries DROP COLUMN likes_text;
ALTER TABLE post_queries DROP COLUMN published_at_text;
"""

# --------------------------------------------------------------------------- v5

# `phrases` 缺 `analysis_version`：DATA_MODEL 第 1 节总览写明分析结果层每张表都带
# run_id + analysis_version + extractor_version，第 4 节的 phrases 明细漏了它。没有版本列，
# 换版本重跑要么撞 `UNIQUE(run_id, text, kind, source_version_id)`，要么——批次级短语的
# `source_version_id` 恒为 NULL，而 SQLite 认定 UNIQUE 里的 NULL 互不相等——安静地写出
# 第二份同词行，谁都不知道哪条是当前结论。
# 只补可空列 + **部分唯一索引**（仅约束"声明了版本且无来源版本"的行）：P0-C 之前遗留的
# 无版本短语行不改写、不补版本，历史留空比伪造一个版本号诚实。
_V5_PHRASE_VERSIONING = """
ALTER TABLE phrases ADD COLUMN analysis_version TEXT DEFAULT NULL;
CREATE UNIQUE INDEX idx_phrases_run_version ON phrases(run_id, analysis_version, text, kind)
    WHERE analysis_version IS NOT NULL AND source_version_id IS NULL;
"""

_V5_ROLLBACK = """
DROP INDEX IF EXISTS idx_phrases_run_version;
ALTER TABLE phrases DROP COLUMN analysis_version;
"""

# --------------------------------------------------------------------------- v6

# C-12：相对发布时间被分析批次时间改写。`evidence.created_at` 是分析写入时刻，不是观察
# 时刻；解析页面相对日期（"3天前"）必须钉在发布时间文本所属的那次卡片观察上。证据行落库
# 时保存这个卡片观察 id：`observed_at` 与 `published_at_text` 都从它回查，不另存副本，
# 审计只有一份真相。可空、默认 NULL：v5 之前的历史证据行没有可靠锚点，事后编一个比留空
# 更糟——评分层对 NULL 锚点一律保守 unknown。
_V6_EVIDENCE_ANCHOR = """
ALTER TABLE evidence ADD COLUMN post_observation_id INTEGER REFERENCES post_observations(id) ON DELETE RESTRICT DEFAULT NULL;
CREATE INDEX idx_evidence_observation ON evidence(run_id, post_observation_id);
"""

_V6_ROLLBACK = """
DROP INDEX IF EXISTS idx_evidence_observation;
ALTER TABLE evidence DROP COLUMN post_observation_id;
"""

# --------------------------------------------------------------------------- v7

# C-15：观察重放不能用 captured_at 猜"当时看到的是哪版标题"。
# `post_content_versions` 按 (post_id, field, text_sha256) 去重：标题改走 A→B→A 时，第三次
# 观察复用 A 的旧行（captured_at 仍停在第一次），"取观察时刻前最新版本"的推断会把这次读成 B。
# 版本内容去重与"这次卡片观察实际看见哪个版本"是两层事实：后者是观察记录，落在
# `post_queries.title_version_id`。可空、默认 NULL：v6 之前的历史行补不回来当时看见的是
# 哪一版，事后猜一个比留空更糟——重放与评分对 NULL 引用只做保守推断（只取观察时刻之前
# 已捕获的版本，绝不借未来值），并把该口径作为老行兼容明确记录（DATA_MODEL 第 9.6 节）。
_V7_TITLE_OBSERVATION_REF = """
ALTER TABLE post_queries ADD COLUMN title_version_id INTEGER REFERENCES post_content_versions(id) ON DELETE RESTRICT DEFAULT NULL;
"""

_V7_ROLLBACK = """
ALTER TABLE post_queries DROP COLUMN title_version_id;
"""

# --------------------------------------------------------------------------- v8

# C22：崩溃的槽位必须知道"自己正在第几次重探"，否则接管的新 worker 只会把它重新 increment 成
# 下一轮 revisit，于是同一未完成槽位被开成两个不同 logical key 的操作——第二次会真的重打 CLI，
# 既重复扣预算又丢第一次的落盘结果。`active_revisit_seq` 记录领取槽位时锁定的那一次重探序号，
# 认领与 revisit 递增在同一事务里完成（见 Repository.claim_frontier_slot）。
# 可空、默认 NULL：v7 之前的历史 in_flight 行补不回当时的序号，宁可让恢复把它归为保守 uncertain
# （不猜序号、不重打 CLI），也不伪造一个可能撞开错误操作的序号。
_V8_FRONTIER_ACTIVE_REVISIT_SEQ = """
ALTER TABLE frontier ADD COLUMN active_revisit_seq INTEGER DEFAULT NULL;
"""

_V8_ROLLBACK = """
ALTER TABLE frontier DROP COLUMN active_revisit_seq;
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "全局身份 + 任务状态 + 观察记录 + 运行支持",
        _V1_IDENTITY + _V1_RUN_STATE + _V1_OPERATIONS + _V1_OBSERVATIONS + _V1_SUPPORT + _V1_INDEXES,
        _V1_ROLLBACK,
    ),
    Migration(
        2,
        "分析结果层（P0-D 写入）",
        _V2_ANALYSIS + _V2_INDEXES,
        _V2_ROLLBACK,
    ),
    Migration(
        3,
        "正文观察的 run/operation/attempt 来源（C-09）",
        _V3_BODY_PROVENANCE,
        _V3_ROLLBACK,
    ),
    Migration(
        4,
        "分析事实：卡片可见字段、内容家族成员、需求稳定键（P0-D）",
        _V4_ANALYSIS_FACTS,
        _V4_ROLLBACK,
    ),
    Migration(
        5,
        "短语行的分析版本键（P0-22）",
        _V5_PHRASE_VERSIONING,
        _V5_ROLLBACK,
    ),
    Migration(
        6,
        "证据行的发布时间观察锚点（C-12）",
        _V6_EVIDENCE_ANCHOR,
        _V6_ROLLBACK,
    ),
    Migration(
        7,
        "卡片观察的标题版本显式引用（C-15）",
        _V7_TITLE_OBSERVATION_REF,
        _V7_ROLLBACK,
    ),
    Migration(
        8,
        "未完成槽位的活动重探序号（C22）",
        _V8_FRONTIER_ACTIVE_REVISIT_SEQ,
        _V8_ROLLBACK,
    ),
)

HEAD_VERSION = MIGRATIONS[-1].version

_TABLES: dict[int, tuple[str, ...]] = {
    1: (
        "keywords", "keyword_aliases", "authors", "posts", "post_content_versions",
        "runs", "budget_revisions", "branches", "branch_stats", "run_keywords",
        "operations", "operation_attempts", "discovery_edges", "frontier",
        "suggestion_observations", "suggestion_items", "post_observations", "post_queries",
        "body_observations", "budget_reservations", "budget_ledger", "run_events",
        "control_requests", "worker_leases", "raw_artifacts", "model_calls", "artifacts",
        "run_retention",
    ),
    2: (
        "suggestion_versions", "content_families", "phrases", "problems", "needs",
        "evidence", "cluster_assignments",
    ),
    # v3 不新增表：只给 body_observations 补来源列与索引，用列存在性验证（见迁移测试）。
    3: (),
    4: ("content_family_members",),
    # v5 不新增表：只给 phrases 补 analysis_version 与部分唯一索引，用列存在性验证。
    5: (),
    # v6 不新增表：只给 evidence 补观察锚点列与索引，用列存在性验证（C-12）。
    6: (),
    # v7 不新增表：只给 post_queries 补标题版本显式引用列，用列存在性验证（C-15）。
    7: (),
    # v8 不新增表：只给 frontier 补活动重探序号列，用列存在性验证（C22）。
    8: (),
}


def tables_for(version: int) -> tuple[str, ...]:
    return _TABLES.get(version, ())


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL,
               code_version TEXT NOT NULL
           )"""
    )


def current_version(conn: sqlite3.Connection) -> int:
    _bootstrap(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _strip_comments(statement: str) -> str:
    return "\n".join(
        line for line in statement.splitlines() if not line.strip().startswith("--")
    ).strip()


def _iter_statements(script: str) -> list[str]:
    """按分号切分脚本；纯注释片段继续缓冲，避免把注释当成一条空语句执行。

    只在分号处问一次 `complete_statement`：这样一行里写两条语句也能拆开执行，
    而字符串字面量里的分号不会提前截断（此时 complete_statement 仍是 False）。
    """
    statements: list[str] = []
    buffer = ""
    for char in script:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            body = _strip_comments(buffer)
            if body:
                statements.append(body)
            buffer = ""
    tail = _strip_comments(buffer)
    if tail:
        statements.append(tail)
    return statements


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """显式事务：DDL 在默认隔离级别下是自动提交的，必须 BEGIN 才能整段回滚。

    迁移不接嵌套：调用方已经开着事务时，这里若 `commit()` 就把别人没写完的批次替它落地了，
    若 SAVEPOINT 又改变不了"迁移是结构性变更"这件事。策略是明确拒绝，
    让调用方先提交或回滚，而不是静默地把外层事务带走。
    """
    _reject_open_transaction(conn)
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    for statement in _iter_statements(script):
        conn.execute(statement)


def _reject_open_transaction(conn: sqlite3.Connection) -> None:
    """迁移入口就要拒绝：`_bootstrap` 的 DDL 会把没显式 BEGIN 的连接直接提交掉，
    所以等到 `_atomic` 里再判断已经晚了——外层批次早被顺手落地了。
    """
    if conn.in_transaction:
        raise SchemaError(
            "已有未提交事务时不得执行迁移：迁移要独占结构性变更，"
            "请先 commit/rollback（Repository 的批次写入不参与迁移）"
        )


def apply_migrations(conn: sqlite3.Connection, *, target: int = HEAD_VERSION) -> tuple[int, ...]:
    """按顺序补齐缺失版本；每个迁移一个事务，失败即整段回滚并抛出，不留半成品 schema。"""
    _reject_open_transaction(conn)
    _bootstrap(conn)
    version = current_version(conn)
    if version > target:
        raise SchemaError(
            f"数据库 schema 版本 {version} 高于代码支持版本 {target}：拒绝以旧代码打开新库"
        )
    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version <= version or migration.version > target:
            continue
        try:
            with _atomic(conn):
                _run_script(conn, migration.up)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at, code_version) VALUES (?, ?, ?)",
                    (migration.version, utc_now().isoformat(), CODE_VERSION),
                )
        except sqlite3.Error as exc:
            raise SchemaError(f"迁移 {migration.version} 失败：{exc}") from exc
        applied.append(migration.version)
    return tuple(applied)


def rollback(conn: sqlite3.Connection, *, to_version: int = 0) -> tuple[int, ...]:
    """倒序执行 down 语句；同样整段事务，只用于开发与迁移验证。"""
    _reject_open_transaction(conn)
    _bootstrap(conn)
    if to_version < 0 or to_version > HEAD_VERSION:
        raise SchemaError(f"目标版本 {to_version} 超出支持范围 0..{HEAD_VERSION}")
    version = current_version(conn)
    rolled: list[int] = []
    for migration in reversed(MIGRATIONS):
        if migration.version <= to_version or migration.version > version:
            continue
        try:
            with _atomic(conn):
                _run_script(conn, migration.down)
                conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migration.version,))
        except sqlite3.Error as exc:
            raise SchemaError(f"回滚迁移 {migration.version} 失败：{exc}") from exc
        rolled.append(migration.version)
    return tuple(sorted(rolled, reverse=True))


def applied_versions(conn: sqlite3.Connection) -> tuple[int, ...]:
    _bootstrap(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return tuple(int(row["version"]) for row in rows)


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


__all__ = [
    "Migration",
    "MIGRATIONS",
    "HEAD_VERSION",
    "BUDGET_KEYS",
    "ACCESS_KEYS",
    "QUOTA_KEYS",
    "SchemaError",
    "apply_migrations",
    "applied_versions",
    "current_version",
    "rollback",
    "table_names",
    "tables_for",
]

