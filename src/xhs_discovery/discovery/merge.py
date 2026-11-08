"""在线初步归并与簇记账（P0-18）。

规则：
- 归并键是"核心签名"（规范化的问题文本）。签名相同且关键属性不冲突才自动合并；
  属性冲突时**分裂成不同簇**（键加冲突属性后缀），因为"上海 2025 名额分配"和
  "北京 名额分配"文本再像也不是同一个需求；
- 原句永远保留：`problems` 与 `evidence` 每次抽取都新增行，归并只是可撤销的关系记录；
- 幂等：证据与问题都用自然键查重，重放同一次 attempt 不会写出第二条问题；
- 历史奖励不被后续精修篡改：本层的簇与奖励只按当次观察计算，
  P0-D/P2 的精修用新的 `analysis_version` 另写一份并标 `superseded_at`，
  绝不 UPDATE 这里产生的 `branch_stats` 累计值。

`needs.canonical_text` 在初步阶段就是核心签名本身——需求措辞的模板化表达属于 P0-D 的分析面，
这里不提前发明"获取…方法"这类句式，避免把未验证的表述当成已确认需求。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.discovery.extract import EXTRACTOR_VERSION, IntentCandidateRecord, ProblemRecord
from xhs_discovery.models.enums import StatementKind
from xhs_discovery.models.evidence import ProblemAttributes
from xhs_discovery.storage.repository import Repository

PRELIMINARY_ANALYSIS_VERSION = "p0-preliminary.1"
MERGE_METHOD = "preliminary_core_signature"
ATTRIBUTE_NAMES = ("topic", "object_", "intent", "pain_point", "location", "year", "polarity")


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _attributes_payload(attributes: ProblemAttributes) -> dict:
    return {name: getattr(attributes, name) for name in ATTRIBUTE_NAMES if getattr(attributes, name)}


def conflict_key(canonical: str, conflicts: list[str], attributes: ProblemAttributes) -> str:
    """冲突簇的稳定标识：冲突属性名与取值一起进键，重放得到同一个键。"""
    parts = ",".join(f"{name}={getattr(attributes, name)}" for name in sorted(conflicts))
    return f"{canonical}#conflict[{parts}]"


@dataclass(frozen=True)
class MergeOutcome:
    evidence_ids: tuple[int, ...] = ()
    problem_ids: tuple[int, ...] = ()
    need_ids: tuple[int, ...] = ()
    valid_evidence_ids: tuple[int, ...] = ()
    new_valid_clusters: int = 0
    merged_into_existing: int = 0
    conflicts: tuple[dict, ...] = ()
    reused: int = 0

    @property
    def new_clusters(self) -> int:
        return self.new_valid_clusters


class PreliminaryMerger:
    """按 run 隔离的初步归并器：簇身份是 (run, 签名)，跨 run 不复用。"""

    def __init__(
        self,
        repo: Repository,
        run_id: str,
        *,
        analysis_version: str = PRELIMINARY_ANALYSIS_VERSION,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        self.repo = repo
        self.run_id = run_id
        self.analysis_version = analysis_version
        self.extractor_version = extractor_version
        self.conn = repo.conn

    # ------------------------------------------------------------------ 查询

    def _find_need(self, canonical: str, statement_kind: StatementKind) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM needs WHERE run_id = ? AND canonical_text = ? AND statement_kind = ?"
            " AND analysis_version = ?",
            (self.run_id, canonical, statement_kind.value, self.analysis_version),
        ).fetchone()
        return None if row is None else dict(row)

    def _find_evidence(self, record: ProblemRecord) -> dict | None:
        evidence = record.candidate.evidence
        row = self.conn.execute(
            "SELECT id FROM evidence WHERE run_id = ? AND quote = ? AND char_start = ?"
            " AND char_end = ? AND extractor_version = ? AND analysis_version = ?"
            " AND content_version_id IS ?",
            (
                self.run_id,
                evidence.quote,
                evidence.char_start,
                evidence.char_end,
                self.extractor_version,
                self.analysis_version,
                evidence.content_version_id,
            ),
        ).fetchone()
        return None if row is None else dict(row)

    def _find_problem(self, record: ProblemRecord) -> dict | None:
        row = self.conn.execute(
            "SELECT id FROM problems WHERE run_id = ? AND canonical_text = ? AND source_field = ?"
            " AND extractor_version = ? AND content_version_id IS ?",
            (
                self.run_id,
                record.candidate.canonical_text,
                record.candidate.evidence.source_field.value,
                self.extractor_version,
                record.candidate.evidence.content_version_id,
            ),
        ).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------------ 写入

    def _insert_evidence(self, record: ProblemRecord, *, seen_at: datetime) -> int:
        evidence = record.candidate.evidence
        cursor = self.conn.execute(
            "INSERT INTO evidence(run_id, source_field, quote, char_start, char_end,"
            " content_version_id, suggestion_version_id, container_sha256, operation_id,"
            " attempt_no, session_id, keyword_id, post_id, note_id, extractor_version,"
            " analysis_version, synthetic, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                evidence.source_field.value,
                evidence.quote,
                evidence.char_start,
                evidence.char_end,
                evidence.content_version_id,
                evidence.container_sha256,
                record.operation_id,
                record.attempt_no,
                evidence.provenance.session_id,
                record.keyword_id,
                record.post_id,
                evidence.provenance.note_id,
                self.extractor_version,
                self.analysis_version,
                1 if evidence.synthetic else 0,
                seen_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def _insert_problem(
        self, record: ProblemRecord, *, counts_as_valid: bool, seen_at: datetime
    ) -> int:
        candidate = record.candidate
        attributes = candidate.attributes
        cursor = self.conn.execute(
            "INSERT INTO problems(run_id, canonical_text, content_version_id, suggestion_version_id,"
            " source_field, source_type, topic, object, intent, pain_point, location, year, polarity,"
            " extraction_method, extractor_version, counts_as_valid_problem_cluster, created_at)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                candidate.canonical_text,
                candidate.evidence.content_version_id,
                candidate.evidence.source_field.value,
                candidate.source_type.value,
                attributes.topic,
                attributes.object_,
                attributes.intent,
                attributes.pain_point,
                attributes.location,
                attributes.year,
                attributes.polarity,
                MERGE_METHOD,
                self.extractor_version,
                1 if counts_as_valid else 0,
                seen_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def _create_need(
        self,
        canonical: str,
        *,
        statement_kind: StatementKind,
        evidence_status: str,
        attributes: dict,
        evidence_ids: list[int],
        problem_ids: list[int],
        seen_at: datetime,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO needs(run_id, canonical_text, statement_kind, category, evidence_status,"
            " evidence_complete_json, analysis_version, extractor_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                canonical,
                statement_kind.value,
                attributes.get("intent"),
                evidence_status,
                _dump(
                    {"evidence_ids": evidence_ids, "problem_ids": problem_ids, "attributes": attributes}
                ),
                self.analysis_version,
                self.extractor_version,
                seen_at.isoformat(),
                seen_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def _add_to_need(
        self, need: dict, *, evidence_id: int, problem_id: int, attributes: dict, seen_at: datetime
    ) -> None:
        payload = json.loads(need["evidence_complete_json"] or "{}")
        evidence_ids = list(payload.get("evidence_ids", []))
        problem_ids = list(payload.get("problem_ids", []))
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
        if problem_id not in problem_ids:
            problem_ids.append(problem_id)
        merged = dict(payload.get("attributes", {}))
        for name, value in attributes.items():
            merged.setdefault(name, value)
        payload.update({"evidence_ids": evidence_ids, "problem_ids": problem_ids, "attributes": merged})
        self.conn.execute(
            "UPDATE needs SET evidence_complete_json = ?, updated_at = ? WHERE id = ?",
            (_dump(payload), seen_at.isoformat(), int(need["id"])),
        )
        # 同一批次里后续候选读到的是刚更新过的属性集，否则簇内冲突判定会用陈旧快照
        need["evidence_complete_json"] = _dump(payload)

    def _assign(
        self,
        problem_id: int,
        need_id: int,
        *,
        conflicts: list[str],
        auto_merged: bool,
        seen_at: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO cluster_assignments(run_id, problem_id, need_id, method,"
            " similarity, attributes_conflict_json, auto_merged, analysis_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                problem_id,
                need_id,
                MERGE_METHOD,
                1.0 if auto_merged else None,
                _dump(conflicts) if conflicts else None,
                1 if auto_merged else 0,
                self.analysis_version,
                seen_at.isoformat(),
            ),
        )

    # ------------------------------------------------------------------ 归并

    def _resolve_need(
        self, record: ProblemRecord, *, evidence_id: int, problem_id: int, seen_at: datetime
    ) -> tuple[int, bool, list[str]]:
        """返回 (need_id, 是否新建簇, 冲突属性列表)。冲突不合并：分裂出带后缀的新簇。"""
        candidate = record.candidate
        canonical = candidate.canonical_text
        attributes = _attributes_payload(candidate.attributes)
        need = self._find_need(canonical, StatementKind.explicit_problem)
        if need is None:
            need_id = self._create_need(
                canonical,
                statement_kind=StatementKind.explicit_problem,
                evidence_status="verified",
                attributes=attributes,
                evidence_ids=[evidence_id],
                problem_ids=[problem_id],
                seen_at=seen_at,
            )
            return need_id, True, []

        stored = json.loads(need["evidence_complete_json"] or "{}")
        clash = ProblemAttributes.model_validate(stored.get("attributes") or {}).conflicts_with(
            candidate.attributes
        )
        if not clash:
            self._add_to_need(
                need,
                evidence_id=evidence_id,
                problem_id=problem_id,
                attributes=attributes,
                seen_at=seen_at,
            )
            return int(need["id"]), False, []

        suffix = conflict_key(canonical, clash, candidate.attributes)
        split = self._find_need(suffix, StatementKind.explicit_problem)
        if split is None:
            need_id = self._create_need(
                suffix,
                statement_kind=StatementKind.explicit_problem,
                evidence_status="verified",
                attributes=attributes,
                evidence_ids=[evidence_id],
                problem_ids=[problem_id],
                seen_at=seen_at,
            )
            return need_id, True, clash
        self._add_to_need(
            split,
            evidence_id=evidence_id,
            problem_id=problem_id,
            attributes=attributes,
            seen_at=seen_at,
        )
        return int(split["id"]), False, clash

    def merge(self, records: list[ProblemRecord], *, seen_at: datetime) -> MergeOutcome:
        """一次搜索的观察 → 证据 → 问题 → 簇，全部在同一事务里落地。"""
        evidence_ids: list[int] = []
        problem_ids: list[int] = []
        need_ids: list[int] = []
        valid_evidence: list[int] = []
        conflicts: list[dict] = []
        new_valid = 0
        merged_existing = 0
        reused = 0

        with self.repo.write():
            for record in records:
                existing_evidence = self._find_evidence(record)
                if existing_evidence is not None:
                    evidence_id = int(existing_evidence["id"])
                    reused += 1
                else:
                    evidence_id = self._insert_evidence(record, seen_at=seen_at)
                evidence_ids.append(evidence_id)
                if record.counts_as_valid:
                    valid_evidence.append(evidence_id)

                existing_problem = self._find_problem(record)
                if existing_problem is not None:
                    problem_id = int(existing_problem["id"])
                else:
                    problem_id = self._insert_problem(
                        record, counts_as_valid=record.counts_as_valid, seen_at=seen_at
                    )
                problem_ids.append(problem_id)

                if not record.counts_as_valid:
                    # 营销/机构/经验分享与没有意图线索的标题只留观察，不建需求簇
                    continue

                need_id, is_new, clash = self._resolve_need(
                    record, evidence_id=evidence_id, problem_id=problem_id, seen_at=seen_at
                )
                if is_new:
                    new_valid += 1
                else:
                    merged_existing += 1
                if clash:
                    conflicts.append(
                        {
                            "canonical": record.candidate.canonical_text,
                            "conflicts": clash,
                            "need_id": need_id,
                        }
                    )
                if need_id not in need_ids:
                    need_ids.append(need_id)
                self._assign(
                    problem_id,
                    need_id,
                    conflicts=clash,
                    auto_merged=not clash and not is_new,
                    seen_at=seen_at,
                )
            self.repo.record_event(
                self.run_id,
                "preliminary_merge",
                detail={
                    "records": len(records),
                    "new_valid_clusters": new_valid,
                    "merged_into_existing": merged_existing,
                    "conflicts": len(conflicts),
                    "reused": reused,
                    "analysis_version": self.analysis_version,
                },
                at=seen_at,
            )
        return MergeOutcome(
            evidence_ids=tuple(evidence_ids),
            problem_ids=tuple(problem_ids),
            need_ids=tuple(need_ids),
            valid_evidence_ids=tuple(valid_evidence),
            new_valid_clusters=new_valid,
            merged_into_existing=merged_existing,
            conflicts=tuple(conflicts),
            reused=reused,
        )

    def record_intent_candidates(
        self, records: list[IntentCandidateRecord], *, seen_at: datetime, synthetic: bool
    ) -> int:
        """联想意图候选：单独 statement_kind，永不标 verified，也不参与簇计数。"""
        created = 0
        with self.repo.write():
            for record in records:
                if (
                    self._find_need(record.canonical_text, StatementKind.search_intent_candidate)
                    is not None
                ):
                    continue
                self._create_need(
                    record.canonical_text,
                    statement_kind=StatementKind.search_intent_candidate,
                    evidence_status="unverified_candidate",
                    attributes={
                        "source": "suggestion",
                        "rank": record.rank,
                        "observation_id": record.observation_id,
                        "keyword_id": record.keyword_id,
                    },
                    evidence_ids=[],
                    problem_ids=[],
                    seen_at=seen_at,
                )
                created += 1
            if created:
                self.repo.record_event(
                    self.run_id,
                    "intent_candidates_recorded",
                    detail={"count": created, "synthetic": bool(synthetic)},
                    at=seen_at,
                )
        return created

    # ------------------------------------------------------------------ 统计

    def valid_cluster_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM needs WHERE run_id = ? AND statement_kind = 'explicit_problem'"
            " AND analysis_version = ?",
            (self.run_id, self.analysis_version),
        ).fetchone()
        return int(row["n"]) if row else 0

    def distinct_problem_quotes(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT canonical_text) AS n FROM problems WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def source_type_breakdown(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM problems WHERE run_id = ? GROUP BY source_type"
            " ORDER BY source_type",
            (self.run_id,),
        ).fetchall()
        return {str(row["source_type"]): int(row["n"]) for row in rows}


__all__ = [
    "MERGE_METHOD",
    "PRELIMINARY_ANALYSIS_VERSION",
    "MergeOutcome",
    "PreliminaryMerger",
    "conflict_key",
]
