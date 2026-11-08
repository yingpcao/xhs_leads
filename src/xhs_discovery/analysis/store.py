"""分析结果入库（P0-22 起）。

`analysis/` 的统计模块全是纯函数，只有这一层碰数据库，好处是同一批语料的发现结果
可以脱离 SQLite 复核，坏处是任何"库里有什么"的口径都必须在这里显式写出来。

幂等口径与 `discovery/merge.py` 一致：**先按自然键查，命中就复用**，不依赖
`INSERT OR IGNORE`。原因是 `phrases` 的表级 `UNIQUE(run_id, text, kind, source_version_id)`
里 `source_version_id` 对批次级短语恒为 NULL，而 SQLite 认定"NULL 之间互不相等"——
把唯一性交给约束，同一批短语重跑会安静地写出第二份。版本维度的真实约束是 v5 补的
部分唯一索引 `idx_phrases_run_version`，它只覆盖"声明了 analysis_version"的行。

证据与问题同样按自然键查重，并且**每条证据在入库前都用库里的版本行原文复核一遍定位**——
`Evidence.locate` 只能证明"quote 在我构造时拿到的那段文本里"，证明不了它引用的版本 id
就是那段文本。这一步是 P0-24 的失效引用防线，也是需求详情里"原句可点开"的前提。

需求与归属（P0-25）沿用同一套自然键：需求按 `(run, analysis_version, stable_key)` 查重，
问题归属按 `cluster_assignments` 的 `UNIQUE(run_id, problem_id, analysis_version)` 对齐——
一个簇在每个版本下只有一条行，改判走 `superseded_at` 复用同一行并写 `cluster_revoked` 事件，
既不覆盖历史结论，也不让同一个问题在两个簇里各算一次。

分数（P0-27）只回写自己那一批的行：`WHERE id = ? AND analysis_version = ?`。
内容家族则是跨 run 的内容事实（与 `posts`、`post_content_versions` 同口径，表里没有
`run_id`），家族身份用 `(代表版本, 方法, 分析版本)` 查——`content_families` 没有 key 列，
而代表版本取成员里最小 id，新副本只会拿到更大的 id，所以这个键不会随批次漂移。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.analysis.clustering import (
    ATTRIBUTE_NAMES,
    MERGE_METHOD,
    ClusterOutcome,
    NeedPlan,
    attributes_payload,
)
from xhs_discovery.analysis.extraction import EXTRACTION_METHOD, ExtractedProblem, ExtractionOutcome
from xhs_discovery.analysis.phrase_mining import PhraseCandidate, PhraseSet
from xhs_discovery.analysis.scoring import (
    FAMILY_METHOD,
    SCORING_METHOD,
    FamilyPlan,
    NeedScore,
    ScoreOutcome,
)
from xhs_discovery.models.evidence import Evidence
from xhs_discovery.storage.repository import Repository


def _dump_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _container_text(conn, evidence: Evidence) -> str | None:
    """按证据引用的版本行取回容器原文；行不存在就是失效引用。"""
    if evidence.content_version_id is not None:
        row = conn.execute(
            "SELECT text FROM post_content_versions WHERE id = ?",
            (evidence.content_version_id,),
        ).fetchone()
        return None if row is None else str(row["text"])
    if evidence.suggestion_version_id is not None:
        row = conn.execute(
            "SELECT text FROM suggestion_versions WHERE id = ?",
            (evidence.suggestion_version_id,),
        ).fetchone()
        return None if row is None else str(row["text"])
    return None


def insert_evidence(
    repo: Repository, run_id: str, problem: ExtractedProblem, *, at: datetime
) -> tuple[int, bool]:
    """写一条证据，返回 (id, 是否新建)；同 (run, 版本行, quote, 位置, 版本对) 已有行就复用。

    落库前逐条用**库里的版本行原文**复核定位。`Evidence.locate` 只保证 quote 能在构造它时
    传进来的容器里定位，而写进库的版本 id 是另一个数——两者对不上就是失效引用（引用版本 A、
    文本来自 B）。这种行一旦入库，需求详情里的"原文"会永远点开对不上，所以直接拒绝而不是
    标记后放行。

    复用已有行时不改写 `operation_id`：证据身份是"某版本原文的某一段"，同一 run 里两次观察
    到同一篇帖子的同一句话是同一条事实被看到两次，不是两条事实。
    """
    evidence = problem.candidate.evidence
    unit = problem.unit
    if evidence.content_version_id is None and evidence.suggestion_version_id is None:
        raise ValueError("证据必须指向具体内容版本或联想版本，否则无法复核原文定位")
    container = _container_text(repo.conn, evidence)
    if container is None or not evidence.locates_in(container):
        raise ValueError(
            f"证据无法在所引版本行定位（内容版本 {evidence.content_version_id}"
            f" / 联想版本 {evidence.suggestion_version_id}），拒绝入库"
        )
    existing = repo.conn.execute(
        "SELECT id FROM evidence WHERE run_id = ? AND quote = ? AND char_start = ?"
        " AND char_end = ? AND extractor_version = ? AND analysis_version = ?"
        " AND content_version_id IS ? AND suggestion_version_id IS ?",
        (
            run_id,
            evidence.quote,
            evidence.char_start,
            evidence.char_end,
            evidence.extractor_version,
            evidence.analysis_version,
            evidence.content_version_id,
            evidence.suggestion_version_id,
        ),
    ).fetchone()
    if existing is not None:
        return int(existing["id"]), False
    cursor = repo.conn.execute(
        "INSERT INTO evidence(run_id, source_field, quote, char_start, char_end,"
        " content_version_id, suggestion_version_id, container_sha256, operation_id,"
        " attempt_no, session_id, keyword_id, post_id, note_id, extractor_version,"
        " analysis_version, synthetic, created_at, post_observation_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            evidence.source_field.value,
            evidence.quote,
            evidence.char_start,
            evidence.char_end,
            evidence.content_version_id,
            evidence.suggestion_version_id,
            evidence.container_sha256,
            unit.operation_id,
            unit.attempt_no,
            unit.session_id,
            unit.keyword_id,
            unit.post_id,
            evidence.provenance.note_id,
            evidence.extractor_version,
            evidence.analysis_version,
            1 if evidence.synthetic else 0,
            at.isoformat(),
            # C-12：发布时间文本所属卡片观察；解析锚点从这条观察回查，created_at 只记录
            # 分析写入时刻。复用已有行时同样不改写它——锚点跟着第一次观察走。
            unit.card_observation_id,
        ),
    )
    return int(cursor.lastrowid), True


def find_problem(
    repo: Repository, run_id: str, problem: ExtractedProblem
) -> dict | None:
    """问题行的自然键。

    `problems` 表没有 `analysis_version`，版本维度由 `extractor_version` 承担：改规则就是改
    抽取器版本，重跑得到另一批行而不是覆盖上一批——属性、来源与簇计数才各自还能复核。
    """
    candidate = problem.candidate
    return repo.conn.execute(
        "SELECT id FROM problems WHERE run_id = ? AND canonical_text = ? AND source_field = ?"
        " AND extractor_version = ? AND content_version_id IS ? AND suggestion_version_id IS ?",
        (
            run_id,
            candidate.canonical_text,
            candidate.evidence.source_field.value,
            candidate.evidence.extractor_version,
            candidate.evidence.content_version_id,
            candidate.evidence.suggestion_version_id,
        ),
    ).fetchone()


def insert_problem(
    repo: Repository,
    run_id: str,
    problem: ExtractedProblem,
    *,
    extraction_method: str = EXTRACTION_METHOD,
    at: datetime,
) -> int:
    candidate = problem.candidate
    attributes = candidate.attributes
    cursor = repo.conn.execute(
        "INSERT INTO problems(run_id, canonical_text, content_version_id, suggestion_version_id,"
        " source_field, source_type, topic, object, intent, pain_point, location, year, polarity,"
        " extraction_method, extractor_version, counts_as_valid_problem_cluster, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            candidate.canonical_text,
            candidate.evidence.content_version_id,
            candidate.evidence.suggestion_version_id,
            candidate.evidence.source_field.value,
            candidate.source_type.value,
            attributes.topic,
            attributes.object_,
            attributes.intent,
            attributes.pain_point,
            attributes.location,
            attributes.year,
            attributes.polarity,
            extraction_method,
            candidate.evidence.extractor_version,
            1 if problem.counts_as_valid else 0,
            at.isoformat(),
        ),
    )
    return int(cursor.lastrowid)


@dataclass(frozen=True)
class ExtractionWrite:
    evidence_inserted: int
    evidence_reused: int
    problems_inserted: int
    problems_reused: int
    evidence_ids: tuple[int, ...] = ()
    problem_ids: tuple[int, ...] = ()

    def as_event(self) -> dict[str, object]:
        return {
            "evidence_inserted": self.evidence_inserted,
            "evidence_reused": self.evidence_reused,
            "problems_inserted": self.problems_inserted,
            "problems_reused": self.problems_reused,
        }


def record_extraction(
    repo: Repository,
    run_id: str,
    outcome: ExtractionOutcome,
    *,
    extraction_method: str = EXTRACTION_METHOD,
    at: datetime,
) -> ExtractionWrite:
    """一次抽取 = 证据行 + 问题行 + 一条批次事件（含每篇"为什么没问题"的审计）。

    两者必须同事务、且逐条配对写入：只落了问题行而证据失败，需求图里就是一个指不到原句的
    孤立节点（P0.4 验收项"无孤立证据"）。
    """
    evidence_ids: list[int] = []
    problem_ids: list[int] = []
    inserted_evidence = reused_evidence = inserted_problems = reused_problems = 0
    with repo.write():
        for problem in outcome.problems:
            evidence_id, evidence_created = insert_evidence(repo, run_id, problem, at=at)
            known_problem = find_problem(repo, run_id, problem)
            if known_problem is not None:
                problem_id = int(known_problem["id"])
                reused_problems += 1
            else:
                problem_id = insert_problem(
                    repo, run_id, problem, extraction_method=extraction_method, at=at
                )
                inserted_problems += 1
            if evidence_created:
                inserted_evidence += 1
            else:
                reused_evidence += 1
            evidence_ids.append(evidence_id)
            problem_ids.append(problem_id)
        write = ExtractionWrite(
            evidence_inserted=inserted_evidence,
            evidence_reused=reused_evidence,
            problems_inserted=inserted_problems,
            problems_reused=reused_problems,
            evidence_ids=tuple(evidence_ids),
            problem_ids=tuple(problem_ids),
        )
        repo.record_event(
            run_id,
            "analysis_extraction",
            detail={**outcome.summary(), **write.as_event()},
            at=at,
        )
        for miss in outcome.no_problem_documents:
            repo.record_event(run_id, "analysis_no_problem", detail=miss, at=at)
    return write


def find_need_by_stable_key(
    repo: Repository, run_id: str, analysis_version: str, key: str
) -> dict | None:
    """需求行的自然键：`(run, analysis_version, stable_key)`，由 v4 的部分唯一索引保证。"""
    row = repo.conn.execute(
        "SELECT * FROM needs WHERE run_id = ? AND analysis_version = ? AND stable_key = ?",
        (run_id, analysis_version, key),
    ).fetchone()
    return None if row is None else dict(row)


def _need_payload(plan: NeedPlan) -> str:
    """`needs.evidence_complete_json`：成员引用 + 属性 + 缺失记账。

    推断需求与方案构想不占 `cluster_assignments` 行（一个问题是且只是一件显性簇的成员），
    它们与问题/证据的关系全靠这里引用的 id 回溯，所以宁可写全也不能省。
    """
    return _dump_json(
        {
            "evidence_ids": list(plan.evidence_ids),
            "problem_ids": list(plan.problem_ids),
            "attributes": attributes_payload(plan.attributes),
            "attribute_gaps": sorted(set(ATTRIBUTE_NAMES) - set(attributes_payload(plan.attributes))),
            "signature": plan.signature,
            "counts_as_valid_need": plan.counts_as_valid_need,
            **plan.detail,
        }
    )


def insert_need(
    repo: Repository,
    run_id: str,
    plan: NeedPlan,
    *,
    analysis_version: str,
    extractor_version: str,
    at: datetime,
) -> int:
    cursor = repo.conn.execute(
        "INSERT INTO needs(run_id, canonical_text, statement_kind, category, evidence_status,"
        " evidence_complete_json, analysis_version, extractor_version, created_at, updated_at,"
        " stable_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            plan.canonical_text,
            plan.statement_kind.value,
            plan.attributes.intent,
            plan.evidence_status,
            _need_payload(plan),
            analysis_version,
            extractor_version,
            at.isoformat(),
            at.isoformat(),
            plan.stable_key,
        ),
    )
    return int(cursor.lastrowid)


def refresh_need_membership(
    repo: Repository, existing: dict, plan: NeedPlan, *, at: datetime
) -> bool:
    """成员集变了才 UPDATE：重跑同一批问题必须写出逐字相同的 JSON，否则"幂等"只是口号。"""
    payload = _need_payload(plan)
    if str(existing["evidence_complete_json"]) == payload:
        return False
    repo.conn.execute(
        "UPDATE needs SET evidence_complete_json = ?, updated_at = ? WHERE id = ?",
        (payload, at.isoformat(), int(existing["id"])),
    )
    return True


def assignment_row(
    conn, run_id: str, problem_id: int, analysis_version: str, *, active_only: bool = False
) -> dict | None:
    """一个问题在每个分析版本下只有一条关系行——`UNIQUE(run_id, problem_id, analysis_version)`。

    `active_only` 用来区分"当前归属"与"包括已撤销的历史行"：撤销不删行，所以默认查询会把
    已撤销的行也算进来，供重新归并复用。
    """
    sql = (
        "SELECT * FROM cluster_assignments WHERE run_id = ? AND problem_id = ?"
        " AND analysis_version = ?"
    )
    if active_only:
        sql += " AND superseded_at IS NULL"
    row = conn.execute(sql, (run_id, problem_id, analysis_version)).fetchone()
    return None if row is None else dict(row)


def reconcile_assignment(
    repo: Repository,
    run_id: str,
    plan: NeedPlan,
    *,
    problem_id: int,
    need_id: int,
    analysis_version: str,
    at: datetime,
) -> str:
    """把"问题 → 簇"的关系对齐到计划给出的簇，返回 created / reused / revoked。

    撤销写成 `superseded_at` + `auto_merged=0` 并复用同一行，不新增也不删除：
    关系历史留在 `run_events` 与这一行上，而 `UNIQUE(run_id, problem_id, analysis_version)`
    保证任意时刻只有当前归属可被读到。已撤销的行被重新归并回同一个簇时同样算 revoked
    ——调用方需要看得见"这里发生过一次改判"，而不是只看到一次静默复用。
    """
    conflicts = list(plan.detail.get("conflicts") or [])
    auto_merged = not conflicts
    existing = assignment_row(repo.conn, run_id, problem_id, analysis_version)
    if existing is None:
        repo.conn.execute(
            "INSERT INTO cluster_assignments(run_id, problem_id, need_id, method, similarity,"
            " attributes_conflict_json, auto_merged, analysis_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                problem_id,
                need_id,
                MERGE_METHOD,
                1.0 if auto_merged else None,
                _dump_json(conflicts) if conflicts else None,
                1 if auto_merged else 0,
                analysis_version,
                at.isoformat(),
            ),
        )
        return "created"
    changed = (
        int(existing["need_id"]) != need_id
        or existing["superseded_at"] is not None
        or str(existing["method"]) != MERGE_METHOD
    )
    if not changed:
        return "reused"
    previous_need = int(existing["need_id"])
    repo.record_event(
        run_id,
        "cluster_revoked",
        detail={
            "problem_id": problem_id,
            "old_need_id": previous_need,
            "new_need_id": need_id,
            "superseded_at": existing["superseded_at"],
            "analysis_version": analysis_version,
            "conflicts": conflicts,
        },
        at=at,
    )
    repo.conn.execute(
        "UPDATE cluster_assignments SET need_id = ?, method = ?, similarity = ?,"
        " attributes_conflict_json = ?, auto_merged = ?, superseded_at = NULL"
        " WHERE id = ?",
        (
            need_id,
            MERGE_METHOD,
            1.0 if auto_merged else None,
            _dump_json(conflicts) if conflicts else None,
            1 if auto_merged else 0,
            int(existing["id"]),
        ),
    )
    return "revoked"


def revoke_cluster_assignment(
    repo: Repository,
    run_id: str,
    *,
    problem_id: int,
    analysis_version: str,
    reason: str,
    at: datetime,
) -> bool:
    """人工撤销一次归并：只标撤销与关闭自动合并，问题行与证据行原样保留。"""
    existing = assignment_row(repo.conn, run_id, problem_id, analysis_version, active_only=True)
    if existing is None:
        return False
    repo.conn.execute(
        "UPDATE cluster_assignments SET superseded_at = ?, auto_merged = 0 WHERE id = ?",
        (at.isoformat(), int(existing["id"])),
    )
    repo.record_event(
        run_id,
        "cluster_revoked",
        detail={
            "problem_id": problem_id,
            "old_need_id": int(existing["need_id"]),
            "new_need_id": None,
            "reason": reason,
            "analysis_version": analysis_version,
        },
        at=at,
    )
    return True


@dataclass(frozen=True)
class ClusterWrite:
    needs_inserted: int
    needs_reused: int
    needs_updated: int
    assignments_created: int
    assignments_reused: int
    assignments_revoked: int

    def as_event(self) -> dict[str, object]:
        return {
            "needs_inserted": self.needs_inserted,
            "needs_reused": self.needs_reused,
            "needs_updated": self.needs_updated,
            "assignments_created": self.assignments_created,
            "assignments_reused": self.assignments_reused,
            "assignments_revoked": self.assignments_revoked,
        }


def record_clustering(
    repo: Repository, run_id: str, outcome: ClusterOutcome, *, at: datetime
) -> ClusterWrite:
    """落一次归并计划：需求行 + 问题归属 + 一条批次事件 + 每条"为什么没归上"的审计。

    需求行必须**先于**归属行写入（同一事务里按外键顺序排），否则 `cluster_assignments.need_id`
    会指到不存在的簇；反过来撤销永远不删需求行——一个簇被清空成员后仍然留着，
    因为 `run_events` 与导出里都可能引用它，删行会让历史引用断掉。
    """
    inserted = reused = updated = 0
    created = kept = revoked = 0
    need_ids: dict[str, int] = {}
    with repo.write():
        for plan in outcome.plans:
            existing = find_need_by_stable_key(
                repo, run_id, outcome.analysis_version, plan.stable_key
            )
            if existing is None:
                need_id = insert_need(
                    repo,
                    run_id,
                    plan,
                    analysis_version=outcome.analysis_version,
                    extractor_version=outcome.extractor_version,
                    at=at,
                )
                inserted += 1
            else:
                need_id = int(existing["id"])
                reused += 1
                if refresh_need_membership(repo, existing, plan, at=at):
                    updated += 1
            need_ids[plan.stable_key] = need_id
            for problem_id in plan.problem_ids:
                action = reconcile_assignment(
                    repo,
                    run_id,
                    plan,
                    problem_id=problem_id,
                    need_id=need_id,
                    analysis_version=outcome.analysis_version,
                    at=at,
                )
                if action == "created":
                    created += 1
                elif action == "reused":
                    kept += 1
                else:
                    revoked += 1
        repo.record_event(
            run_id,
            "analysis_clustering",
            detail={
                **outcome.summary(),
                **ClusterWrite(inserted, reused, updated, created, kept, revoked).as_event(),
                "merge_method": MERGE_METHOD,
            },
            at=at,
        )
        for skipped in outcome.skipped:
            repo.record_event(
                run_id,
                "cluster_skipped",
                detail={**skipped, "analysis_version": outcome.analysis_version},
                at=at,
            )
    return ClusterWrite(inserted, reused, updated, created, kept, revoked)


# ------------------------------------------------------------------ 评分与内容家族


def find_content_family(
    repo: Repository, representative_version_id: int, *, analysis_version: str
) -> dict | None:
    """家族行的身份只能是（代表版本, 方法, 分析版本）：表里没有 key 列。

    代表版本取成员里**最小**的 id，而版本 id 只会递增——新增一份副本永远拿到更大的 id，
    代表版本不漂移，这个键因此跨批次稳定。
    """
    row = repo.conn.execute(
        "SELECT * FROM content_families WHERE representative_version_id = ? AND method = ?"
        " AND analysis_version = ?",
        (representative_version_id, FAMILY_METHOD, analysis_version),
    ).fetchone()
    return dict(row) if row is not None else None


def insert_content_families(
    repo: Repository,
    families: tuple[FamilyPlan, ...] | list[FamilyPlan],
    *,
    analysis_version: str,
    at: datetime,
) -> tuple[int, int, int]:
    """写家族行与成员行，返回 (新家族, 复用家族, 新成员)。

    `content_families` 没有 `run_id`：一件内容是跨 run 的事实（和 `posts`、
    `post_content_versions` 同一口径），"这个 run 见过它"才由本 run 的 evidence 决定。
    Q 仍然是按 run 计算的——它数的是**本 run 证据够得着**的家族，不是表里的家族总数。
    """
    stamp = at.isoformat()
    families_inserted = families_reused = members_inserted = 0
    for plan in families:
        existing = find_content_family(
            repo, plan.representative_version_id, analysis_version=analysis_version
        )
        if existing is None:
            cursor = repo.conn.execute(
                "INSERT INTO content_families(representative_version_id, method, member_count,"
                " analysis_version, created_at) VALUES (?, ?, ?, ?, ?)",
                (plan.representative_version_id, FAMILY_METHOD, plan.member_count,
                 analysis_version, stamp),
            )
            family_id = int(cursor.lastrowid)
            families_inserted += 1
        else:
            family_id = int(existing["id"])
            families_reused += 1
            if int(existing["member_count"]) != plan.member_count:
                repo.conn.execute(
                    "UPDATE content_families SET member_count = ? WHERE id = ?",
                    (plan.member_count, family_id),
                )
        for version_id in plan.member_version_ids:
            member = repo.conn.execute(
                "SELECT content_version_id FROM content_family_members"
                " WHERE family_id = ? AND content_version_id = ?",
                (family_id, version_id),
            ).fetchone()
            if member is not None:
                continue
            repo.conn.execute(
                "INSERT INTO content_family_members(family_id, content_version_id, similarity,"
                " analysis_version, created_at) VALUES (?, ?, ?, ?, ?)",
                # 相似度恒为 1.0：成员是"规范化后同一句"，不是"有点像"。
                # 留一个阈值参数会诱导后续把它调松，把两件事并成一件。
                (family_id, version_id, 1.0, analysis_version, stamp),
            )
            members_inserted += 1
    return families_inserted, families_reused, members_inserted


def apply_need_score(
    repo: Repository, item: NeedScore, *, analysis_version: str, weights: dict[str, float],
    assumptions: dict[str, object], at: datetime
) -> bool:
    """把一份分数写回**它自己那一批**的需求行。

    `WHERE id = ? AND analysis_version = ?`：不带版本条件，重跑新版本会覆盖旧版本的分数，
    历史奖励就又不可比了。行没被任何版本认领时返回 False，让调用方计数而不是静默跳过。
    """
    payload = _dump_json(item.as_json(weights=weights, assumptions=assumptions))
    cursor = repo.conn.execute(
        "UPDATE needs SET score = ?, score_json = ?, updated_at = ?"
        " WHERE id = ? AND analysis_version = ?",
        (item.score.score, payload, at.isoformat(), item.need_id, analysis_version),
    )
    return cursor.rowcount > 0


@dataclass(frozen=True)
class ScoringWrite:
    families_inserted: int
    families_reused: int
    members_inserted: int
    scores_updated: int
    needs_without_score: int

    def as_event(self) -> dict[str, object]:
        return {
            "families_inserted": self.families_inserted,
            "families_reused": self.families_reused,
            "members_inserted": self.members_inserted,
            "scores_updated": self.scores_updated,
            "needs_without_score": self.needs_without_score,
        }


def record_scoring(
    repo: Repository, run_id: str, outcome: ScoreOutcome, *, at: datetime
) -> ScoringWrite:
    """落一次评分：家族行 + 分数回写 + 一条批次事件 + 每条未知分量的审计。

    未知分量（作者引用全缺 → A、日期解析不出 → F）单独发事件：分数本身仍然可算，
    但"这一条是靠三个分量重归一化出来的"必须在库里看得出来，否则导出表上
    两个同为 0.6 的需求会被当成等价证据。
    """
    with repo.write():
        if outcome.run_id != run_id:
            raise ValueError(f"评分结果属于 {outcome.run_id}，不能写进 {run_id}")
        fam_ins, fam_keep, mem_ins = insert_content_families(
            repo, outcome.families, analysis_version=outcome.analysis_version, at=at
        )
        updated = 0
        without_score = 0
        for item in outcome.scored:
            if apply_need_score(
                repo,
                item,
                analysis_version=outcome.analysis_version,
                weights=outcome.weights,
                assumptions=outcome.assumptions,
                at=at,
            ):
                updated += 1
            if item.score.score is None:
                without_score += 1
                repo.record_event(
                    run_id,
                    "demand_score_undisplayable",
                    detail={
                        "need_id": item.need_id,
                        "stable_key": item.stable_key,
                        "reason": "没有可定位证据，S/Q/A/F 全缺，score=NULL",
                        "analysis_version": outcome.analysis_version,
                    },
                    at=at,
                )
            elif item.unknowns:
                repo.record_event(
                    run_id,
                    "demand_score_unknown_component",
                    detail={
                        "need_id": item.need_id,
                        "stable_key": item.stable_key,
                        "unknowns": list(item.unknowns),
                        "missing_reasons": {
                            name: item.score.features[name].missing_reason
                            for name in item.unknowns
                        },
                        "effective_weight_total": item.score.effective_weight_total,
                        "analysis_version": outcome.analysis_version,
                    },
                    at=at,
                )
        write = ScoringWrite(fam_ins, fam_keep, mem_ins, updated, without_score)
        repo.record_event(
            run_id,
            "analysis_scoring",
            detail={
                **outcome.summary(),
                **write.as_event(),
                "weights": outcome.weights,
                "assumptions": outcome.assumptions,
                "method": SCORING_METHOD,
            },
            at=at,
        )
    return write


def insert_phrases(
    repo: Repository,
    run_id: str,
    candidates: tuple[PhraseCandidate, ...] | list[PhraseCandidate],
    *,
    analysis_version: str,
    extractor_version: str,
    at: datetime,
) -> tuple[int, int]:
    """写入短语行，返回 (新写入, 复用已有)。同 (run, version, text, kind) 只有一条。"""
    inserted = 0
    reused = 0
    stamp = at.isoformat()
    for candidate in candidates:
        existing = repo.conn.execute(
            "SELECT id FROM phrases WHERE run_id = ? AND text = ? AND kind = ?"
            " AND analysis_version IS ? AND source_version_id IS NULL",
            (run_id, candidate.text, candidate.kind, analysis_version),
        ).fetchone()
        if existing is not None:
            reused += 1
            continue
        repo.conn.execute(
            "INSERT INTO phrases(run_id, text, kind, score, freq, source_version_id,"
            " extractor_version, analysis_version) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                run_id,
                candidate.text,
                candidate.kind,
                candidate.score,
                candidate.doc_freq,
                extractor_version,
                analysis_version,
            ),
        )
        inserted += 1
    return inserted, reused


def record_phrase_run(
    repo: Repository,
    run_id: str,
    phrase_set: PhraseSet,
    *,
    analysis_version: str,
    at: datetime,
) -> tuple[int, int]:
    """一批发现 = 短语行 + 一条批次事件 + 每条降级候选一条 `phrase_skipped` 事件。

    批次事件与降级事件都带 `analysis_version`：没有跑发现、跑了但都没过阈值、
    过了阈值但只有词典词，这三种情况在库里必须长得不一样。
    """
    with repo.write():
        inserted, reused = insert_phrases(
            repo,
            run_id,
            phrase_set.phrases,
            analysis_version=analysis_version,
            extractor_version=phrase_set.extractor_version,
            at=at,
        )
        repo.record_event(
            run_id,
            "phrase_mining",
            detail={**phrase_set.summary(), "analysis_version": analysis_version,
                    "inserted": inserted, "reused": reused},
            at=at,
        )
        for skipped in phrase_set.skipped:
            repo.record_event(
                run_id,
                "phrase_skipped",
                detail={**skipped.as_event(), "analysis_version": analysis_version},
                at=at,
            )
    return inserted, reused


__all__ = [
    "ClusterWrite",
    "ExtractionWrite",
    "ScoringWrite",
    "apply_need_score",
    "assignment_row",
    "find_content_family",
    "find_need_by_stable_key",
    "find_problem",
    "insert_content_families",
    "insert_evidence",
    "insert_need",
    "insert_phrases",
    "insert_problem",
    "record_clustering",
    "record_extraction",
    "record_phrase_run",
    "record_scoring",
    "refresh_need_membership",
    "reconcile_assignment",
    "revoke_cluster_assignment",
]
