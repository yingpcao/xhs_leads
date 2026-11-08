"""保守归并、稳定需求 ID 与四类陈述（P0-25 / P0-26）。

这一层只做**计划**：读库里的 `problems`/`evidence`/语料，算出"应该怎么归并"，不写一行库。
写库集中在 `analysis/store.py`，好处是同一批问题行脱离 SQLite 也能复核归并结论，
坏处是归并口径必须全部能从这里读到的数据重算——不能依赖采集时的内存对象。

四条不能让步的规则：

1. **保守归并**：同一条问题签名只在**属性轮廓逐位一致**时合并——七个关键属性
   （`attributes_profile`）每一位都必须同值，"一方已知、另一方缺失"同样不算兼容
   （DEVELOPMENT_PLAN §5.4"缺失属性不视为任意兼容"）。已知值互不相同是冲突分裂
   （`conflicts_with` 口径），已知对缺失是缺口分裂（`gap_attributes`）；两者都分裂成
   `conflict_key` 带后缀的另一行，**绝不把某个成员的已知属性补进缺它的成员**——
   簇的 `attributes` 就是成员共同的属性轮廓，`attribute_gaps` 就是这一轮廓的缺位，
   每个成员的已知/缺失逐条记在 `members` 里可回溯。冲突判定与分裂键沿用
   `discovery.merge.conflict_key`——换版本时两种簇还能逐字对照，而不是各发明一套键。
   基线簇（含最小 problem_id 的轮廓）保持裸签名，其余轮廓按问题 id 顺序加后缀；
   同一轮廓的多名成员完全同属性，合并是无损的，`auto_merged=1`；分裂侧 `auto_merged=0`
   并带 `relation="non_equivalent_attribute_split"`，看板与导出据此显示"非等价关联"。
   抽取层配合这条规则：从不拿主题词去填空位（宁可 NULL）。
2. **稳定需求 ID**：`needs.stable_key` 是 `{statement_kind, signature}` 的 sha256。
   签名本身已经带上冲突属性，所以冲突分裂得到的是不同的键、不同的行；
   键是**归并结果的标识**，不是帖子的身份——同一条原句在另一个版本下换一套规则，
   就该拿到另一个键、另写一行，旧行不改写。
3. **可撤销**：`cluster_assignments` 有 `UNIQUE(run_id, problem_id, analysis_version)`，
   一个问题在每个版本下只有一条关系行。撤销 = 标 `superseded_at` 并把 `auto_merged` 归零
   （历史留在行上，不删），重新归并复用同一行并写一条 `cluster_revoked` 事件记录 old→new。
   原句与证据永远在 `problems`/`evidence` 里，撤销只动关系。
   也正因为这条唯一约束，**问题只归显性问题簇**：推断需求、方案构想、联想意图候选
   是显性簇的派生表达，靠 `needs.evidence_complete_json` 里的引用回溯，不抢同一个问题的
   第二个归属。
4. **四类陈述分区**：显性问题有可定位证据才算有效需求；推断需求用保守模板表达并标
   `inferred`，模板文字相同而父簇属性轮廓不同**不能并成同一行**：按轮廓分裂成不同行，
   各组证据、缺口与派生引用各自可追溯（C-11-r2）；方案构想只有当**原句里同时出现产物词与
   自动化词**时才登记，状态恒为
   `unverified_candidate`（表的 CHECK 也这么要求），不凭空替用户发明工具；
   联想意图候选只带 `suggestion_version_id` 等来源引用，永不建证据、永不标 verified——
   平台联想出来的句子不是帖子事实。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Iterable

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.corpus import Corpus
from xhs_discovery.analysis.extraction import segments
# 复用在线层的冲突键写法：分裂出来的簇在两个版本里都能一眼看出是同一件文本的哪一侧。
from xhs_discovery.discovery.extract import core_text
from xhs_discovery.discovery.merge import conflict_key
from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.enums import PostSourceType, SourceField, StatementKind
from xhs_discovery.models.evidence import ProblemAttributes
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.intent import IntentMatcher

MERGE_METHOD = "rule_conservative_signature"

ATTRIBUTE_NAMES = ("topic", "object_", "intent", "pain_point", "location", "year", "polarity")

REASON_NO_EVIDENCE = "problem_row_without_locatable_evidence"
REASON_NO_OBJECT = "inferred_need_requires_an_object"
REASON_ADVERTISED_TOOL = "solution_mention_comes_from_marketing_or_institution"

# 方案构想必须"产物 + 自动化"两边都出现才登记：只有"工具"可能是问工具有没有卖，
# 只有"自动"可能是抱怨自动分流。两边都在，才像在描述一件可以做出来的东西。
ARTIFACT_TERMS = ("工具", "软件", "网站", "小程序", "app", "APP", "清单", "表格", "模板", "计算器", "系统")
AUTOMATION_TERMS = ("自动", "一键", "智能", "推送", "提醒", "匹配", "生成")

# 推断需求的保守模板：措辞只重组已有属性，不引入语料里没有的动词宾语。
INFERRED_TEMPLATES = {
    "method_guidance": "获取{scope}{object}的方法",
    "information_query": "查证{scope}{object}的信息",
    "cost_constraint": "了解{scope}{object}的费用",
    "risk_avoidance": "规避{scope}{object}的风险",
    "comparison_decision": "在{scope}比较{object}并做出选择",
}

EVIDENCE_STATUS = {
    StatementKind.explicit_problem: "verified",
    StatementKind.inferred_need: "inferred",
    StatementKind.solution_idea: "unverified_candidate",
    StatementKind.search_intent_candidate: "unverified_candidate",
}


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def attributes_payload(attributes: ProblemAttributes) -> dict[str, str]:
    """只带非空属性：空位留给缺失记账，不写成一堆 null 让"缺失"看起来像"值"。"""
    return {name: value for name in ATTRIBUTE_NAMES if (value := getattr(attributes, name))}


def attributes_from_row(row: dict) -> ProblemAttributes:
    """从 `problems` 行重建属性：列名 `object` 对应字段别名 `object_`。"""
    return ProblemAttributes(
        **{
            "object_": row.get("object"),
            **{name: row.get(name) for name in ("topic", "intent", "pain_point", "location", "year", "polarity")},
        }
    )


def stable_key(statement_kind: StatementKind, signature: str) -> str:
    """需求稳定 ID：语句类别 + 归并签名 → sha256。

    签名已经携带冲突后缀，所以不需要再把属性表塞进哈希——同一条签名下的成员属性
    必须彼此兼容，属性集是簇的**结果**，簇的键变了就换行。
    """
    return sha256_text(_dump({"statement_kind": statement_kind.value, "signature": signature}))


def attributes_profile(attributes: ProblemAttributes) -> tuple[str | None, ...]:
    """属性轮廓：七个关键属性逐位取值，空串按缺失（None）归一。

    轮廓相等才是"这一簇的成员在说同一件有同样已知/缺位的事"。已知对已知同值才算同，
    已知对缺失不算——缺失不是通配符。
    """
    return tuple(getattr(attributes, name) or None for name in ATTRIBUTE_NAMES)


def is_solution_idea(text: str) -> bool:
    body = core_text(text)
    return any(term in body for term in ARTIFACT_TERMS) and any(term in body for term in AUTOMATION_TERMS)


def scope_of(attributes: ProblemAttributes) -> str:
    """需求措辞的范围串：被更长成分包含的地域/年份不重复出现（"上海"+"上海中考"→只留后者）。"""
    candidates = [value for value in (attributes.topic, attributes.year, attributes.location) if value]
    kept = [
        value
        for value in candidates
        if not any(value != other and value in other for other in candidates)
    ]
    return "".join(kept)


def inferred_text(attributes: ProblemAttributes) -> str | None:
    """推断需求的保守表达；缺对象或缺可套模板的意图就不生成。"""
    template = INFERRED_TEMPLATES.get(attributes.intent or "")
    if template is None or not attributes.object_:
        return None
    return template.format(scope=scope_of(attributes), object=attributes.object_)


@dataclass(frozen=True)
class NeedPlan:
    """一条待落库的需求簇：归并键、展示文本、属性、成员引用。"""

    statement_kind: StatementKind
    signature: str
    canonical_text: str
    stable_key: str
    attributes: ProblemAttributes
    evidence_ids: tuple[int, ...] = ()
    problem_ids: tuple[int, ...] = ()
    counts_as_valid_need: bool = False
    detail: dict = field(default_factory=dict)

    @property
    def evidence_status(self) -> str:
        return EVIDENCE_STATUS[self.statement_kind]


@dataclass(frozen=True)
class ClusterOutcome:
    run_id: str
    analysis_version: str
    extractor_version: str
    plans: tuple[NeedPlan, ...]
    conflict_splits: tuple[dict, ...]
    skipped: tuple[dict, ...]
    problems_considered: int
    problems_not_counted: int

    def by_statement_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for plan in self.plans:
            key = plan.statement_kind.value
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    @property
    def valid_needs(self) -> tuple[NeedPlan, ...]:
        return tuple(plan for plan in self.plans if plan.counts_as_valid_need)

    def summary(self) -> dict[str, object]:
        return {
            "analysis_version": self.analysis_version,
            "extractor_version": self.extractor_version,
            "problems_considered": self.problems_considered,
            "problems_not_counted": self.problems_not_counted,
            "needs": len(self.plans),
            "valid_needs": len(self.valid_needs),
            "conflict_splits": len(self.conflict_splits),
            "skipped": len(self.skipped),
            "by_statement_kind": self.by_statement_kind(),
        }


class ConservativeMerger:
    """按 run 与 `analysis_version` 隔离的归并计划器。

    簇身份是 `(run, analysis_version, stable_key)`：换 run 不复用（别的任务看到的语料不同），
    换版本另写一批（精修不覆盖历史结论）。
    """

    def __init__(
        self,
        repo: Repository,
        run_id: str,
        *,
        analysis_version: str = ANALYSIS_VERSION,
        extractor_version: str = EXTRACTOR_VERSION,
        matcher: IntentMatcher,
    ) -> None:
        if not run_id:
            raise ValueError("归并必须按 run 进行：跨 run 合并簇会把两个任务的证据算成同一个需求")
        self.repo = repo
        self.conn = repo.conn
        self.run_id = run_id
        self.analysis_version = analysis_version
        self.extractor_version = extractor_version
        # matcher 是必填而不是可选：意图候选的"算不算疑问"必须用与抽取同一套规则，
        # 换一套默认判定就会在两处产出对不上的候选清单。
        self.matcher = matcher

    # ------------------------------------------------------------------ 读取

    def problems(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM problems WHERE run_id = ? AND extractor_version = ? ORDER BY id",
            (self.run_id, self.extractor_version),
        ).fetchall()
        return [dict(row) for row in rows]

    def evidence_for(self, row: dict) -> dict | None:
        """问题行的可定位证据：同一版本行里 `core_text(quote)` 与簇签名相同的那条。

        归属靠"内容版本 + 规范化后同句"而不是插入顺序：`record_extraction` 逐条配对写，
        但归并层要能在只有库文件的情况下独立复核，就必须能用库里的数据重算出同一个引用。
        """
        candidates = self.conn.execute(
            "SELECT * FROM evidence WHERE run_id = ? AND extractor_version = ?"
            " AND analysis_version = ? AND source_field = ? AND content_version_id IS ?"
            " AND suggestion_version_id IS ? ORDER BY id",
            (
                self.run_id,
                self.extractor_version,
                self.analysis_version,
                row["source_field"],
                row["content_version_id"],
                row["suggestion_version_id"],
            ),
        ).fetchall()
        for candidate in candidates:
            if core_text(str(candidate["quote"])) == row["canonical_text"]:
                return dict(candidate)
        return None

    def _evidence_id_for_text(self, unit, canonical: str) -> int | None:
        """给方案构想找可指认的证据行；找不到就不硬造引用（构想允许无证据）。"""
        rows = self.conn.execute(
            "SELECT id, quote FROM evidence WHERE run_id = ? AND extractor_version = ?"
            " AND analysis_version = ? AND source_field = ? AND content_version_id IS ?"
            " ORDER BY id",
            (self.run_id, self.extractor_version, self.analysis_version, unit.source_field.value,
             unit.content_version_id),
        ).fetchall()
        for row in rows:
            if core_text(str(row["quote"])) == canonical:
                return int(row["id"])
        return None

    # ------------------------------------------------------------------ 显性问题簇

    def plan_explicit_needs(self, rows: list[dict]) -> tuple[list[NeedPlan], list[dict], list[dict]]:
        """按"属性轮廓精确一致"分簇：已知/缺失/冲突都不等价自动归并（C-11）。

        旧实现在成员合并时用 `setdefault` 把先到的已知属性补进缺它的成员，未知地域因此
        被伪装成已知地域、`attribute_gaps` 也不再显示缺口——违反 §5.4"缺失属性不视为
        任意兼容"。现在：同一 canonical 先按 `attributes_profile` 分组，轮廓不同一律
        分裂成不同簇（基线轮廓=含最小问题 id 的那组，保持裸签名；其余加 `conflict_key`
        后缀）。分裂名里双方都已知而不同记进 `conflicts`，已知对缺失记进 `gap_attributes`；
        成员按 problem id 排序处理，输入顺序置换给出逐字相同的结果。
        """
        skipped: list[dict] = []
        members_by_canonical: dict[str, list[dict]] = {}
        for row in rows:
            if not int(row["counts_as_valid_problem_cluster"]):
                continue
            evidence = self.evidence_for(row)
            if evidence is None:
                # 没有可定位证据的问题永远不能变成展示出来的需求：宁可少一个簇，
                # 也不能让看板上出现点不开原文的"需求"。
                skipped.append(
                    {"reason": REASON_NO_EVIDENCE, "problem_id": int(row["id"]),
                     "canonical_text": row["canonical_text"]}
                )
                continue
            attributes = attributes_from_row(row)
            payload = attributes_payload(attributes)
            members_by_canonical.setdefault(str(row["canonical_text"]), []).append(
                {
                    "problem_id": int(row["id"]),
                    "evidence_id": int(evidence["id"]),
                    "quote": str(evidence["quote"]),
                    "attributes": attributes,
                    "payload": payload,
                    "attribute_gaps": sorted(set(ATTRIBUTE_NAMES) - set(payload)),
                    "profile": attributes_profile(attributes),
                }
            )

        groups: dict[str, dict] = {}
        splits: list[dict] = []
        for canonical in sorted(members_by_canonical):
            members = sorted(members_by_canonical[canonical], key=lambda item: item["problem_id"])
            by_profile: dict[tuple, list[dict]] = {}
            for member in members:
                by_profile.setdefault(member["profile"], []).append(member)
            profiles = sorted(by_profile, key=lambda profile: by_profile[profile][0]["problem_id"])
            base_profile = profiles[0]
            for profile in profiles:
                part = by_profile[profile]
                differences: list[str] = []
                gap_names: list[str] = []
                for name, mine, base in zip(ATTRIBUTE_NAMES, profile, base_profile):
                    if mine == base:
                        continue
                    differences.append(name)
                    if bool(mine) != bool(base):
                        gap_names.append(name)
                # 无差异就是签名本身：`conflict_key` 是给分裂簇加后缀用的，主簇不该带空后缀。
                signature = (
                    conflict_key(canonical, differences, part[0]["attributes"])
                    if differences
                    else canonical
                )
                group = groups.get(signature)
                if group is None:
                    groups[signature] = {
                        "canonical": canonical,
                        "conflicts": differences,
                        "gap_attributes": gap_names,
                        # 簇属性 = 本轮廓的已知位；缺位保持缺，不取任何成员的补位。
                        "attributes": dict(part[0]["payload"]),
                        "evidence_ids": [],
                        "problem_ids": [],
                        "texts": [],
                        "members": [],
                    }
                    group = groups[signature]
                for member in part:
                    if member["evidence_id"] not in group["evidence_ids"]:
                        group["evidence_ids"].append(member["evidence_id"])
                        group["problem_ids"].append(member["problem_id"])
                        group["texts"].append(member["quote"])
                    group["members"].append(
                        {
                            "problem_id": member["problem_id"],
                            "evidence_id": member["evidence_id"],
                            "attributes": member["payload"],
                            "attribute_gaps": member["attribute_gaps"],
                        }
                    )
                if differences:
                    for member in part:
                        splits.append(
                            {
                                "canonical_text": canonical,
                                "conflicts": differences,
                                "gap_attributes": gap_names,
                                "signature": signature,
                                "problem_id": member["problem_id"],
                                "attribute_gaps": member["attribute_gaps"],
                                "relation": "non_equivalent_attribute_split",
                            }
                        )

        plans: list[NeedPlan] = []
        for signature in sorted(groups):
            group = groups[signature]
            merged = ProblemAttributes.model_validate(group["attributes"])
            payload = group["attributes"]
            gaps = sorted(set(ATTRIBUTE_NAMES) - set(payload))
            plans.append(
                NeedPlan(
                    statement_kind=StatementKind.explicit_problem,
                    signature=signature,
                    canonical_text=signature,
                    stable_key=stable_key(StatementKind.explicit_problem, signature),
                    attributes=merged,
                    evidence_ids=tuple(int(value) for value in group["evidence_ids"]),
                    problem_ids=tuple(int(value) for value in group["problem_ids"]),
                    counts_as_valid_need=True,
                    detail={
                        "problem_text": group["canonical"],
                        "attributes": payload,
                        "attribute_gaps": gaps,
                        "conflicts": group["conflicts"],
                        "gap_attributes": group["gap_attributes"],
                        "members": group["members"],
                        "equivalence": (
                            "non_equivalent_attribute_split" if group["conflicts"]
                            else "exact_attribute_profile"
                        ),
                        "split_from_signature": (
                            group["canonical"] if group["conflicts"] else None
                        ),
                        "quotes": group["texts"],
                        "merge_method": MERGE_METHOD,
                    },
                )
            )
        return plans, splits, skipped

    # ------------------------------------------------------------------ 推断需求

    def plan_inferred_needs(self, explicit: Iterable[NeedPlan]) -> tuple[list[NeedPlan], list[dict]]:
        """派生需求按 **模板文字 × 父簇属性轮廓** 双键归组（C-11-r2）。

        `scope_of` 会去掉已被 topic 包含的地域，year/polarity/pain_point 又不全进模板，
        于是**不同属性轮廓可以渲染出同一句话**。旧实现在这里按文字 `seen` 去重，第二组
        连同它的 evidence_ids 被静默略过——展示文本相同不能当归并身份。现在与显性侧同一
        口径：同文字先按 `attributes_profile` 分组，基线轮廓（最小父簇签名）保持裸签名，
        其余轮廓按 `conflict_key` 加后缀、另起稳定键；每组只带自己成员的已知属性与缺口，
        evidence_ids 与 derived_from 引用逐组各自可追溯。同轮廓的多个父簇可以并成一组
        （属性逐位相同，合并无损），证据取并集、父簇引用全列。
        """
        plans: list[NeedPlan] = []
        skipped: list[dict] = []
        groups: dict[str, dict[tuple, list[NeedPlan]]] = {}
        for plan in sorted(explicit, key=lambda item: item.signature):
            text = inferred_text(plan.attributes)
            if text is None:
                skipped.append(
                    {
                        "reason": REASON_NO_OBJECT,
                        "intent": plan.attributes.intent,
                        "object": plan.attributes.object_,
                        "from_signature": plan.signature,
                    }
                )
                continue
            by_profile = groups.setdefault(core_text(text), {})
            by_profile.setdefault(attributes_profile(plan.attributes), []).append(plan)
        for signature in sorted(groups):
            by_profile = groups[signature]
            # 基线轮廓 = 含最小成员问题 id 的那组（与显性侧同一口径），其余按问题 id 顺序加后缀。
            profiles = sorted(
                by_profile,
                key=lambda profile: (
                    min((pid for parent in by_profile[profile] for pid in parent.problem_ids), default=math.inf),
                    min(parent.signature for parent in by_profile[profile]),
                ),
            )
            base_profile = profiles[0]
            for profile in profiles:
                parents = by_profile[profile]
                differences: list[str] = []
                gap_names: list[str] = []
                for name, mine, base in zip(ATTRIBUTE_NAMES, profile, base_profile):
                    if mine == base:
                        continue
                    differences.append(name)
                    if bool(mine) != bool(base):
                        gap_names.append(name)
                own_signature = (
                    conflict_key(signature, differences, parents[0].attributes)
                    if differences
                    else signature
                )
                evidence_ids: list[int] = []
                for parent in parents:
                    for evidence_id in parent.evidence_ids:
                        if evidence_id not in evidence_ids:
                            evidence_ids.append(int(evidence_id))
                payload = attributes_payload(parents[0].attributes)
                plans.append(
                    NeedPlan(
                        statement_kind=StatementKind.inferred_need,
                        signature=own_signature,
                        canonical_text=own_signature,
                        stable_key=stable_key(StatementKind.inferred_need, own_signature),
                        attributes=parents[0].attributes,
                        evidence_ids=tuple(evidence_ids),
                        counts_as_valid_need=True,
                        detail={
                            "attributes": payload,
                            "attribute_gaps": sorted(set(ATTRIBUTE_NAMES) - set(payload)),
                            "derived_from": parents[0].signature,
                            "derived_from_stable_key": parents[0].stable_key,
                            "derived_from_signatures": [parent.signature for parent in parents],
                            "derived_from_stable_keys": [parent.stable_key for parent in parents],
                            # 措辞是模板生成的，句子不是用户写过的：导出与看板都靠这个标记区分。
                            "generated_expression": True,
                            "template": INFERRED_TEMPLATES[str(parents[0].attributes.intent)],
                            "conflicts": differences,
                            "gap_attributes": gap_names,
                            "equivalence": (
                                "non_equivalent_attribute_split" if differences
                                else "exact_attribute_profile"
                            ),
                            "split_from_signature": signature if differences else None,
                        },
                    )
                )
        return plans, skipped

    # ------------------------------------------------------------------ 方案构想

    def plan_solution_ideas(self, corpus: Corpus) -> tuple[list[NeedPlan], list[dict]]:
        plans: list[NeedPlan] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for unit in corpus.units:
            if unit.source_field is SourceField.suggestion:
                continue
            source_type = self.matcher.source_type(unit.text)
            if source_type in (PostSourceType.marketing, PostSourceType.institution):
                # 广告与机构稿里的"自动填报系统"是卖点，不是用户想要工具的线索。
                skipped.append(
                    {"reason": REASON_ADVERTISED_TOOL, "source_type": source_type.value,
                     **unit.source_ref()}
                )
                continue
            for segment in segments(unit.text):
                if not is_solution_idea(segment):
                    continue
                canonical = core_text(segment)
                if canonical in seen:
                    continue
                seen.add(canonical)
                evidence_id = self._evidence_id_for_text(unit, canonical)
                plans.append(
                    NeedPlan(
                        statement_kind=StatementKind.solution_idea,
                        signature=canonical,
                        canonical_text=canonical,
                        stable_key=stable_key(StatementKind.solution_idea, canonical),
                        attributes=ProblemAttributes(topic=unit.keyword.normalized),
                        evidence_ids=() if evidence_id is None else (evidence_id,),
                        detail={
                            **unit.source_ref(),
                            "source_type": source_type.value,
                            "idea_text_generated_from_quote": True,
                            "counts_as_valid_need": False,
                        },
                    )
                )
        return sorted(plans, key=lambda plan: plan.signature), skipped

    # ------------------------------------------------------------------ 联想意图候选

    def plan_intent_candidates(self, corpus: Corpus) -> list[NeedPlan]:
        plans: list[NeedPlan] = []
        seen: set[str] = set()
        for unit in corpus.by_source(SourceField.suggestion):
            if not (self.matcher.has_help_marker(unit.text) or self.matcher.match(unit.text)):
                continue
            signature = core_text(unit.text)
            if signature in seen:
                continue
            seen.add(signature)
            plans.append(
                NeedPlan(
                    statement_kind=StatementKind.search_intent_candidate,
                    signature=signature,
                    canonical_text=signature,
                    stable_key=stable_key(StatementKind.search_intent_candidate, signature),
                    attributes=ProblemAttributes(topic=unit.keyword.normalized),
                    detail={
                        "source": "suggestion",
                        "rank": unit.result_rank,
                        "observation_id": unit.observation_id,
                        "operation_id": unit.operation_id,
                        "keyword_id": unit.keyword_id,
                        "suggestion_version_id": unit.suggestion_version_id,
                        # 联想是平台返回的搜索意图线索：能证明"平台觉得有人在这么搜"，
                        # 证明不了任何用户真的问过，所以不带 evidence_id、也永不 verified。
                        "post_fact": False,
                    },
                )
            )
        return sorted(plans, key=lambda plan: plan.signature)

    # ------------------------------------------------------------------ 汇总

    def plan(self, corpus: Corpus) -> ClusterOutcome:
        if corpus.run_id != self.run_id:
            raise ValueError("语料与归并器的 run 不一致：会把别的任务的联想文本登记成本 run 的意图候选")
        rows = self.problems()
        explicit, splits, skipped = self.plan_explicit_needs(rows)
        inferred, inferred_skipped = self.plan_inferred_needs(explicit)
        ideas, idea_skipped = self.plan_solution_ideas(corpus)
        plans = [
            *explicit,
            *inferred,
            *ideas,
            *self.plan_intent_candidates(corpus),
        ]
        return ClusterOutcome(
            run_id=self.run_id,
            analysis_version=self.analysis_version,
            extractor_version=self.extractor_version,
            plans=tuple(plans),
            conflict_splits=tuple(splits),
            skipped=tuple([*skipped, *inferred_skipped, *idea_skipped]),
            problems_considered=len(rows),
            problems_not_counted=sum(
                1 for row in rows if not int(row["counts_as_valid_problem_cluster"])
            ),
        )


__all__ = [
    "ARTIFACT_TERMS",
    "AUTOMATION_TERMS",
    "ATTRIBUTE_NAMES",
    "ClusterOutcome",
    "ConservativeMerger",
    "INFERRED_TEMPLATES",
    "MERGE_METHOD",
    "REASON_ADVERTISED_TOOL",
    "REASON_NO_EVIDENCE",
    "REASON_NO_OBJECT",
    "NeedPlan",
    "attributes_from_row",
    "attributes_payload",
    "attributes_profile",
    "inferred_text",
    "is_solution_idea",
    "scope_of",
    "stable_key",
]
