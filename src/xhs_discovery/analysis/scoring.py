"""需求评分分解与内容家族（P0-27）。

`PROJECT_SPEC.md` 第 9 节把需求分数定成 `.35S+.30Q+.20A+.15F`，四个分量各有各的"不许糊"：

- **S 推荐信号**：位置、多路径出现、跨会话稳定性——都是"平台反复把这句话递到我面前"
  的强度，不是"很多人在问"。证据事实与观察信号是两层（C-13）：同一事实被第二次会话
  看到，仍只有一条证据行（不复制证据来抬 S），但会话/路径/名次要从本 run 的真实观察
  记录聚合——受 run、内容版本可见性、操作/尝试关联约束，不借别的 run 或别的内容
  版本的最优值。
- **Q 去重帖子数**（C-14）：口径是"表达明确问题的去重公开帖子数"，与内容家族抑制
  联立：同帖多 field/多版本产生多个家族，帖子仍只有一篇——家族并入所属帖子；
  同一家族出现在多篇帖子，那是同一件内容的复制——帖子按共享家族并成一体。
  Q = 按共享家族聚类并查帖后的组件数。帖子数、家族数、最终 Q 计数与抑制原因
  分开留在 signals 里审计，家族与原文不因抑制而删。
- **A 来源支持度**：只数**已知**的公开作者引用；作者缺失的帖子不能各算一个独立来源，
  否则一次刷屏就能把 A 顶满。一个已知作者都没有时 A 记 unknown，不是记 0。
- **F 近期持续性**：发布时间原文解析不出完整日期、或它所属的观察锚点缺失时 F=unknown。
  页面写"09-01"不补年份、写"最近"不当成今天——猜出来的日期会直接进入排序，比缺一个
  日期更难被发现。相对写法按**该文本所属卡片观察**的时刻解析：同一观察换分析批次，
  发布日期不许变（C-12）；近期窗口的参照时刻与解析锚点分离，单独可审计。

缺失不直接当零：`WeightedScore` 只对可用分量按原始权重重新归一化，并输出有效权重与
证据完整度（`models/scoring.py` 已经钉好的口径，这里只负责把四个分量喂进去）。
分位数在小样本上不稳定，所以低于 `quantile_min_sample` 时整批判 provisional 并保留原始计数，
不包装成精确排名。

权重与 `quantile_min_sample` 走配置（`scoring.weights` / `scoring.quantile_min_sample`）。
对数缩放的饱和锚点和近期窗口不同：它们还只是**待实验的起点值**，写死在模块常量里并随
分数一起进 `score_json`。配置文件里给它们留字段，等于在验收阶段悄悄改掉 config 哈希，
而它们没有任何实测依据支持被当成"用户可调项"。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from xhs_discovery._version import ANALYSIS_VERSION
from xhs_discovery.analysis.corpus import SUCCESSFUL_STATES
from xhs_discovery.config import DemandWeights
from xhs_discovery.discovery.extract import core_text
from xhs_discovery.discovery.ingest import title_version_for_observation
from xhs_discovery.models.common import sha256_text
from xhs_discovery.models.enums import SourceField, StatementKind
from xhs_discovery.models.scoring import (
    FeatureValue,
    WeightedScore,
    feature,
    missing_feature,
)

SCORING_METHOD = "rule_demand_s_q_a_f"
SCORING_MODEL_VERSION = "p0-demand-sqaf.2"
# .2 登记 C-15 r3 对 S 观察来源聚合的口径变更：标题版本历史引用不再"取最新"（多候选 unknown）、
# 正文来源补齐 run/operation/attempt 自洽；S 的会话/路径/名次随之改变，故提升模型版本。
FAMILY_METHOD = "rule_normalized_exact"

# 对数缩放的饱和锚点：8 个独立来源/会话之后 S 与 A 基本到顶，位置按前 20 名计。
CONSENSUS_CAP = 8
RANK_CAP = 20
# 近期窗口：90 天内仍算"最近还在被提"。起点值，P2 用真实时间分布再调。
RECENT_WINDOW_DAYS = 90

QUANTILE_PROVISIONAL_REASON = "样本量低于分位数稳定门槛，只显示原始计数"

ISO_DATE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})")
CJK_DATE = re.compile(r"^(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日")
DAYS_AGO = re.compile(r"^(?P<days>\d+)\s*天前")
HOURS_AGO = re.compile(r"^(?P<hours>\d+)\s*(?:小时|个钟头)前")
MINUTES_AGO = re.compile(r"^(?P<minutes>\d+)\s*分(?:钟)?前")


def log_scale(count: float, cap: float) -> float:
    """对数缩放并压到 [0,1]：计数翻倍不等于强度翻倍，到锚点就饱和。"""
    if count <= 0:
        return 0.0
    return round(min(1.0, math.log1p(count) / math.log1p(cap)), 6)


def _timestamp(stamp: object) -> datetime | None:
    """库里的观察时刻 → datetime；NULL（旧证据行没有锚点）就是 None，不凑一个。"""
    if stamp is None:
        return None
    return datetime.fromisoformat(str(stamp))


def parse_published_at(text: str | None, *, seen_at: datetime) -> datetime | None:
    """发布时间原文 → 日期。解析不出完整日期就返回 None，不做任何补齐。

    相对写法（"3 天前"、"昨天"）以**文本所属那次卡片观察**为锚点（`seen_at`）：锚点取自
    证据行回链的 `post_observations.observed_at`，不是分析批次时间，所以同一条观察在两个
    分析批次里得到同一个日期；换批次会变的只有近期窗口参照，不在这条解析里。
    `09-01` 这类缺年份的写法不在支持之列——补上来的年份会直接改变 F 的排序。
    """
    if not text:
        return None
    body = text.strip().replace("公元", "")
    absolute = ISO_DATE.match(body) or CJK_DATE.match(body)
    if absolute is not None:
        try:
            return datetime(
                int(absolute.group("year")), int(absolute.group("month")), int(absolute.group("day")),
                tzinfo=seen_at.tzinfo,
            )
        except ValueError:
            return None
    if body in ("今天", "今日"):
        return seen_at
    if body in ("昨天", "昨日"):
        return seen_at - timedelta(days=1)
    relative = (
        (DAYS_AGO, "days", timedelta(days=1)),
        (HOURS_AGO, "hours", timedelta(hours=1)),
        (MINUTES_AGO, "minutes", timedelta(minutes=1)),
    )
    for pattern, group, unit in relative:
        match = pattern.match(body)
        if match is None:
            continue
        return seen_at - int(match.group(group)) * unit
    return None


@dataclass(frozen=True)
class FamilyPlan:
    """一件"内容家族"：同一 field 下 `core_text` 完全相同的一组版本行。

    判定只做"去标点与空白后逐字相同"这一档：更模糊的近似需要一个没有被实测过的阈值，
    把两件事误判成一件，比承认它们不同更糟。`similarity` 因此恒为 1.0——它是**规范化后
    同一句**，不是"有点像"。
    """

    key: str
    field_name: str
    representative_version_id: int
    member_version_ids: tuple[int, ...]

    @property
    def member_count(self) -> int:
        return len(self.member_version_ids)


def family_key(field_name: str, text: str) -> str:
    return sha256_text(f"{field_name}|{core_text(text)}")


def build_families(conn, run_id: str, *, analysis_version: str) -> tuple[FamilyPlan, ...]:
    """本 run 证据引用到的正文/标题版本 → 家族计划（顺序确定，可按代表版本回查）。"""
    rows = conn.execute(
        "SELECT DISTINCT e.content_version_id AS id, v.field, v.text FROM evidence e"
        " JOIN post_content_versions v ON v.id = e.content_version_id"
        " WHERE e.run_id = ? AND e.analysis_version = ? AND e.content_version_id IS NOT NULL"
        " ORDER BY id",
        (run_id, analysis_version),
    ).fetchall()
    buckets: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        buckets.setdefault(family_key(str(row["field"]), str(row["text"])), []).append(
            (int(row["id"]), str(row["field"]))
        )
    plans = [
        FamilyPlan(
            key=key,
            field_name=members[0][1],
            representative_version_id=min(version_id for version_id, _ in members),
            member_version_ids=tuple(sorted(version_id for version_id, _ in members)),
        )
        for key, members in buckets.items()
    ]
    return tuple(sorted(plans, key=lambda plan: (plan.field_name, plan.representative_version_id)))


@dataclass(frozen=True)
class NeedScore:
    need_id: int
    stable_key: str
    statement_kind: StatementKind
    canonical_text: str
    score: WeightedScore
    signals: dict[str, object] = field(default_factory=dict)
    unknowns: tuple[str, ...] = ()

    def as_json(self, *, weights: dict[str, float], assumptions: dict[str, object]) -> dict:
        return {
            "score": self.score.model_dump(mode="json"),
            "signals": self.signals,
            "unknowns": list(self.unknowns),
            "weights": weights,
            "assumptions": assumptions,
            "method": SCORING_METHOD,
            "statement_kind": self.statement_kind.value,
            "stable_key": self.stable_key,
        }


@dataclass(frozen=True)
class ScoreOutcome:
    run_id: str
    analysis_version: str
    scored: tuple[NeedScore, ...]
    families: tuple[FamilyPlan, ...]
    provisional: bool
    weights: dict[str, float]
    assumptions: dict[str, object] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "analysis_version": self.analysis_version,
            "scoring_model_version": SCORING_MODEL_VERSION,
            "scored_needs": len(self.scored),
            "families": len(self.families),
            "family_members": sum(family.member_count for family in self.families),
            "duplicated_versions": sum(family.member_count - 1 for family in self.families),
            "provisional": self.provisional,
            "unknown_component_runs": sum(1 for score in self.scored for _ in score.unknowns),
            "needs_without_score": sum(1 for score in self.scored if score.score.score is None),
            "by_statement_kind": _count_kinds(self.scored),
        }


def _count_kinds(scored: tuple[NeedScore, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in scored:
        counts[item.statement_kind.value] = counts.get(item.statement_kind.value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


class DemandScorer:
    """按 run 与 `analysis_version` 给需求簇算 S/Q/A/F 分解。

    S 的会话/路径/名次来自本 run 真实观察记录的聚合（C-13），Q 用"帖子去重×家族抑制"
    的联合口径（C-14）；两者都不通过复制证据行、帖子行或家族行来实现。

    只给 `explicit_problem` 与 `inferred_need` 打分：方案构想与联想意图候选不是已验证需求，
    给它们一个可比较的分数等于把它们摆上同一个排行榜。

    `reference_at` 是**近期窗口**的参照时刻（显式批次评分时间，随 score_json 审计）；
    它不参与发布日期解析——相对日期的锚点逐行取证据所属卡片观察（C-12）。不给时窗口
    参照退回本簇观察锚点的最大值，跨分析批次不变。
    """

    SCORED_KINDS = (StatementKind.explicit_problem, StatementKind.inferred_need)

    def __init__(
        self,
        repo,
        run_id: str,
        *,
        analysis_version: str = ANALYSIS_VERSION,
        weights: DemandWeights | None = None,
        quantile_min_sample: int = 30,
        reference_at: datetime | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("评分必须按 run 计算：跨 run 的帖子数与来源数不是同一个样本")
        if reference_at is not None and reference_at.tzinfo is None:
            raise ValueError("近期窗口参照时刻必须带时区： naive 时间会把窗口整体挪错方向")
        self.repo = repo
        self.conn = repo.conn
        self.run_id = run_id
        self.analysis_version = analysis_version
        self.weights = weights or DemandWeights()
        self.quantile_min_sample = quantile_min_sample
        self.reference_at = reference_at
        self.families = build_families(self.conn, run_id, analysis_version=analysis_version)
        self._family_by_version = {
            version_id: index
            for index, family in enumerate(self.families)
            for version_id in family.member_version_ids
        }
        # 观察聚合按 (post, version) 惰性缓存：同一 run 里多次评分不重查，
        # 结果也不随评分顺序漂移（C-13 的"当时可见"判定只依赖库里的观察行）。
        self._title_sources: dict[int, list[tuple[int, str, int, int | None]]] = {}
        self._body_sources: dict[int, list[tuple[str, int]]] = {}
        self._card_ranks: dict[tuple[int, str], list[int]] = {}
        self._version_captured: dict[int, str | None] = {}

    # ------------------------------------------------------------------ 取数

    def needs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM needs WHERE run_id = ? AND analysis_version = ? ORDER BY id",
            (self.run_id, self.analysis_version),
        ).fetchall()
        return [dict(row) for row in rows if str(row["statement_kind"]) in self.SCORED_KINDS]

    def _evidence(self, evidence_ids: list[int]) -> list[dict]:
        if not evidence_ids:
            return []
        marks = ",".join("?" for _ in evidence_ids)
        # 发布时间文本与观察锚点必须来自**同一条**卡片观察：证据行回链的
        # post_observation_id 是唯一入口，不按 (run, post) 的首条/末条记录猜
        # （C-12）。缺链的旧证据行文本与锚点都为 NULL，逐行保守 unknown。
        rows = self.conn.execute(
            f"SELECT e.id, e.content_version_id, e.post_id, e.session_id, e.keyword_id,"
            f" e.created_at, e.source_field, e.post_observation_id,"
            f" po.observed_at AS observation_observed_at,"
            f" pq.published_at_text AS published_at_text,"
            f" v.field, v.text, p.author_id, a.kind AS author_kind,"
            f" a.public_ref AS author_ref"
            f" FROM evidence e"
            f" LEFT JOIN post_observations po ON po.id = e.post_observation_id"
            f"  AND po.run_id = e.run_id"
            f" LEFT JOIN post_queries pq ON pq.post_observation_id = e.post_observation_id"
            f"  AND pq.post_id = e.post_id AND pq.run_id = e.run_id"
            f" LEFT JOIN post_content_versions v ON v.id = e.content_version_id"
            f" LEFT JOIN posts p ON p.id = e.post_id"
            f" LEFT JOIN authors a ON a.id = p.author_id"
            f" WHERE e.id IN ({marks}) AND e.run_id = ? AND e.analysis_version = ? ORDER BY e.id",
            (*evidence_ids, self.run_id, self.analysis_version),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 观察来源聚合（C-13）

    def _title_visible_sources(self, post_id: int) -> list[tuple[int, str, int, int | None]]:
        """本 run 里看到过这篇帖子各标题版本的卡片观察：(当时可见版本, 会话, 关键词, 名次)。

        "这次观察看见的是哪一版标题"与重放、语料同一口径（`title_version_for_observation`，
        C-15）：有显式 `post_queries.title_version_id` 就按引用取，历史 NULL 引用才退回
        保守推断（只取观察时刻之前已捕获的版本，绝不借未来值）。"captured_at 前最新版本"
        的猜测在 A→B→A 的第三次观察会把 A 读成 B，让这份事实的会话/名次错记到 B 头上。
        所以"同 run、同内容版本的第二次会话观察"计入 S 信号，而看到过**别的版本**的观察
        不计入这一份事实——不借不相干帖子、别的 run 的名次。
        联想观察（无 `post_queries` 行）本来就进不来。
        """
        cached = self._title_sources.get(post_id)
        if cached is not None:
            return cached
        headers = self.conn.execute(
            "SELECT po.id, po.session_id, po.keyword_id, po.observed_at,"
            " pq.title_version_id, pq.result_rank"
            " FROM post_observations po"
            " JOIN post_queries pq ON pq.post_observation_id = po.id"
            "  AND pq.post_id = ? AND pq.run_id = po.run_id"
            f" WHERE po.run_id = ? AND po.state IN {SUCCESSFUL_STATES}"
            " ORDER BY po.id",
            (post_id, self.run_id),
        ).fetchall()
        sources: list[tuple[int, str, int, int | None]] = []
        for header in headers:
            visible, _title = title_version_for_observation(
                self.conn,
                post_id,
                str(header["observed_at"]),
                header["title_version_id"],
            )
            if visible is None:
                continue
            sources.append(
                (
                    visible,
                    str(header["session_id"]),
                    int(header["keyword_id"]),
                    int(header["result_rank"]),
                )
            )
        self._title_sources[post_id] = sources
        return sources

    def _body_observation_sources(self, content_version_id: int) -> list[tuple[str, int]]:
        """本 run 对这一正文版本的读取观察：(会话, 操作关键词)。

        正文观察是按 run 发生的事件（C-09 的 run/operation/attempt/session 来源列就是
        关联约束）；来源缺失或**不自洽**的旧行不认领，与会语层 `corpus.body_units` 同一口径。
        自洽 = `bo.run_id` 与操作同 run（`op.run_id`）、attempt 真属于该操作
        （`att.operation_id = bo.operation_id`）、操作类型确为 body。只按 `bo.run_id` 过滤、
        `JOIN op.id` 取关键词而漏掉这三条，就会把别的 run 借来的操作、挂在错操作上的尝试
        算成本 run 的会话/路径信号（C-15 r3 复核）。
        """
        cached = self._body_sources.get(content_version_id)
        if cached is None:
            cached = [
                (str(row["session_id"]), int(row["keyword_id"]))
                for row in self.conn.execute(
                    "SELECT bo.session_id, op.keyword_id FROM body_observations bo"
                    " JOIN operations op ON op.id = bo.operation_id"
                    "  AND op.run_id = bo.run_id AND op.operation_type = 'body'"
                    " JOIN operation_attempts att ON att.id = bo.attempt_id"
                    "  AND att.operation_id = bo.operation_id"
                    " WHERE bo.run_id = ? AND bo.content_version_id = ?"
                    " AND bo.state = 'completed' AND bo.session_id IS NOT NULL"
                    " ORDER BY bo.id",
                    (self.run_id, content_version_id),
                )
            ]
            self._body_sources[content_version_id] = cached
        return cached

    def _version_captured_at(self, version_id: int) -> str | None:
        if version_id not in self._version_captured:
            row = self.conn.execute(
                "SELECT captured_at FROM post_content_versions WHERE id = ?", (version_id,)
            ).fetchone()
            self._version_captured[version_id] = None if row is None else str(row["captured_at"])
        return self._version_captured[version_id]

    def _post_card_ranks(self, post_id: int, not_before: str) -> list[int]:
        """正文版本被捕获之后（含同刻），本 run 卡片结果页给出这篇帖子的名次。

        正文本身不在卡片上露出；能约束"当时可见"的只有"该版本在观察时刻已经取到"。
        早于正文捕获的卡片不算这篇帖子的这个版本的名次。
        """
        key = (post_id, not_before)
        cached = self._card_ranks.get(key)
        if cached is None:
            cached = [
                int(row["result_rank"])
                for row in self.conn.execute(
                    "SELECT pq.result_rank FROM post_queries pq"
                    " JOIN post_observations po ON po.id = pq.post_observation_id"
                    f" WHERE pq.run_id = ? AND po.run_id = ? AND pq.post_id = ?"
                    f" AND po.state IN {SUCCESSFUL_STATES} AND po.observed_at >= ?"
                    " ORDER BY pq.id",
                    (self.run_id, self.run_id, post_id, not_before),
                )
            ]
            self._card_ranks[key] = cached
        return cached

    # ------------------------------------------------------------------ 打分

    def score_need(self, need: dict, *, sample_size: int) -> NeedScore:
        payload = json.loads(need["evidence_complete_json"] or "{}")
        rows = self._evidence(list(payload.get("evidence_ids", [])))
        signals: dict[str, object] = {}
        unknowns: list[str] = []
        if not rows:
            return self._no_evidence_score(need)

        # 事实层：去重后的证据行各自记录的会话与关键词（"这条事实第一次是在哪被看到的"）。
        fact_sessions = {str(row["session_id"]) for row in rows if row["session_id"]}
        fact_paths = {int(row["keyword_id"]) for row in rows if row["keyword_id"] is not None}
        # C-13：S 的会话/路径/名次另从本 run 的**真实观察记录**聚合（受版本可见性与
        # 操作/尝试/观察关联约束，见 `_title_visible_sources` 等方法）。同事实被第二个
        # 会话再看到是推荐信号，不是第二条证据——证据、帖子、内容家族都不因此复制。
        # 联想证据的语料行本身就是逐观察一份（版本行按 observation 分），天然不丢会话，
        # 事实集已覆盖，不再另找观察行。
        obs_sessions = set(fact_sessions)
        obs_paths = set(fact_paths)
        ranks: list[int] = []
        for row in rows:
            if row["content_version_id"] is None or row["post_id"] is None:
                continue
            version_id = int(row["content_version_id"])
            post_id = int(row["post_id"])
            source_field = str(row["source_field"])
            if source_field == SourceField.title.value:
                for visible, session_id, keyword_id, rank in self._title_visible_sources(post_id):
                    if visible == version_id:
                        obs_sessions.add(session_id)
                        obs_paths.add(keyword_id)
                        if rank is not None:
                            ranks.append(rank)
            elif source_field == SourceField.body.value:
                for session_id, keyword_id in self._body_observation_sources(version_id):
                    obs_sessions.add(session_id)
                    obs_paths.add(keyword_id)
                captured = self._version_captured_at(version_id)
                if captured is not None:
                    ranks.extend(self._post_card_ranks(post_id, captured))
        # 位置信号只在**有名次**时参与：正文-only 的观察没有排名，猜一个中性值
        # 会让"没采到位置"和"位置一般"算出同一个分数。
        consensus = log_scale(len(obs_sessions) + len(obs_paths), CONSENSUS_CAP)
        if ranks:
            position = round(1.0 - (min(ranks) - 1) / RANK_CAP, 6)
            s_value = round((consensus + position) / 2.0, 6)
        else:
            position = None
            s_value = consensus
        signals["sessions"] = len(obs_sessions)
        signals["parent_paths"] = len(obs_paths)
        signals["evidence_row_sessions"] = len(fact_sessions)
        signals["evidence_row_parent_paths"] = len(fact_paths)
        signals["best_rank"] = min(ranks) if ranks else None
        signals["consensus_signal"] = consensus
        signals["position_signal"] = position

        family_indexes = {
            self._family_by_version[int(row["content_version_id"])]
            for row in rows
            if row["content_version_id"] is not None
            and int(row["content_version_id"]) in self._family_by_version
        }
        posts = {row["post_id"] for row in rows if row["post_id"] is not None}
        # C-14：Q 与家族抑制的联合口径——把帖子按"共享内容家族"聚类并查：
        # - 同帖多 field/多版本给出多个家族：家族并入所属帖子，帖子不因字段数抬成多篇；
        # - 同一家族出现在多篇帖子：那是同一件内容的复制，这些帖子并成一个计数；
        # 所以 Q = 并查后的组件数。原帖子数与家族数是审计事实，谁都不许被改写成对方。
        post_families: dict[int, set[int]] = {}
        for row in rows:
            if row["post_id"] is None or row["content_version_id"] is None:
                continue
            index = self._family_by_version.get(int(row["content_version_id"]))
            if index is None:
                continue
            post_families.setdefault(int(row["post_id"]), set()).add(index)
        parent: dict[tuple[str, int], tuple[str, int]] = {}

        def find(node: tuple[str, int]) -> tuple[str, int]:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for post_id, indexes in post_families.items():
            for index in indexes:
                parent[find(("post", post_id))] = find(("family", index))
        q_raw = len({find(("post", post_id)) for post_id in post_families})
        family_suppressed_posts = len(post_families) - q_raw
        suppression_reasons: set[str] = set()
        post_absorbed_families = 0
        for indexes in post_families.values():
            if len(indexes) > 1:
                post_absorbed_families += len(indexes) - 1
                fields = {self.families[index].field_name for index in indexes}
                if len(fields) < len(indexes):
                    suppression_reasons.add("same_post_multiple_versions")
                if len(fields) > 1:
                    suppression_reasons.add("same_post_multiple_fields")
        if family_suppressed_posts > 0:
            suppression_reasons.add("cross_post_duplicate_content")
        signals["evidence_rows"] = len(rows)
        signals["distinct_posts"] = len(posts)
        signals["content_families"] = len(family_indexes)
        signals["q_counted_posts"] = q_raw
        signals["q_family_suppressed_posts"] = family_suppressed_posts
        signals["q_post_absorbed_families"] = post_absorbed_families
        signals["q_suppression_reasons"] = sorted(suppression_reasons)
        signals["family_representatives"] = sorted(
            self.families[index].representative_version_id for index in family_indexes
        )
        signals["duplicate_versions_suppressed"] = max(0, len(posts) - q_raw)

        known_authors = {
            row["author_ref"] for row in rows if row["author_kind"] == "public_ref" and row["author_ref"]
        }
        authorless_posts = {
            row["post_id"] for row in rows if row["post_id"] is not None and not row["author_ref"]
        }
        signals["distinct_known_authors"] = len(known_authors)
        signals["posts_without_author_ref"] = len(authorless_posts)

        # C-12：解析锚点逐行取该证据所属卡片观察的 observed_at——不用别的帖子、别的
        # 观察或分析批次时间的最大值套在本行上。锚点缺失（旧库 NULL 链）时整行保守
        # unknown，不拿 created_at 之类的时间凑数。
        published: list[datetime] = []
        anchors: list[datetime] = []
        without_anchor = 0
        for row in rows:
            anchor = _timestamp(row["observation_observed_at"])
            if anchor is not None:
                anchors.append(anchor)
            if row["source_field"] != SourceField.suggestion.value and anchor is None:
                without_anchor += 1
            if anchor is None or row["published_at_text"] is None:
                continue
            parsed = parse_published_at(str(row["published_at_text"]), seen_at=anchor)
            if parsed is not None:
                published.append(parsed)

        window_reference, window_source = self._window_reference(anchors)
        recent = [
            moment
            for moment in published
            if window_reference is not None
            and window_reference - moment <= timedelta(days=RECENT_WINDOW_DAYS)
        ]
        signals["published_dates_parsed"] = len(published)
        signals["published_dates_unparseable"] = len(rows) - len(published)
        signals["published_dates_without_anchor"] = without_anchor
        signals["latest_published_at"] = max(published).date().isoformat() if published else None
        signals["window_reference_at"] = (
            window_reference.isoformat() if window_reference is not None else None
        )
        signals["window_reference_source"] = window_source

        features = {
            "S": feature(raw=float(len(obs_sessions) + len(obs_paths)), normalized=s_value,
                         weight=self.weights.s),
            "Q": feature(raw=float(q_raw), normalized=log_scale(q_raw, CONSENSUS_CAP),
                         weight=self.weights.q),
            "A": (
                feature(raw=float(len(known_authors)),
                        normalized=log_scale(len(known_authors), CONSENSUS_CAP),
                        weight=self.weights.a)
                if known_authors
                else missing_feature(self.weights.a, "作者引用全部缺失，不把每篇当不同作者")
            ),
            "F": (
                feature(raw=float(len(recent)),
                        normalized=round(len(recent) / len(published), 6),
                        weight=self.weights.f)
                if published
                else missing_feature(self.weights.f, "发布时间原文无法可靠解析，F=unknown")
            ),
        }
        if not known_authors:
            unknowns.append("A")
        if not published:
            unknowns.append("F")

        provisional = sample_size < self.quantile_min_sample
        score = WeightedScore.compute(
            features,
            model_version=SCORING_MODEL_VERSION,
            provisional=provisional,
            provisional_reason=QUANTILE_PROVISIONAL_REASON if provisional else None,
        )
        return NeedScore(
            need_id=int(need["id"]),
            stable_key=str(need["stable_key"] or ""),
            statement_kind=StatementKind(str(need["statement_kind"])),
            canonical_text=str(need["canonical_text"]),
            score=score,
            signals=signals,
            unknowns=tuple(unknowns),
        )

    def _no_evidence_score(self, need: dict) -> NeedScore:
        """没有可定位证据的需求行不打分。

        四个分量全缺时 `WeightedScore` 给出 `score=None`，而不是 0：0 分仍然会进排行榜，
        而"没有证据"根本不该有名次。展示层要过滤 `score IS NULL`，这是 P0-29 的硬约束。
        """
        reason = "需求行没有可定位的证据，S/Q/A/F 均无来源"
        features = {
            name: missing_feature(getattr(self.weights, name.lower()), reason)
            for name in ("S", "Q", "A", "F")
        }
        return NeedScore(
            need_id=int(need["id"]),
            stable_key=str(need["stable_key"] or ""),
            statement_kind=StatementKind(str(need["statement_kind"])),
            canonical_text=str(need["canonical_text"]),
            score=WeightedScore.compute(
                features,
                model_version=SCORING_MODEL_VERSION,
                provisional_reason=None,
            ),
            signals={"evidence_rows": 0},
            unknowns=("S", "Q", "A", "F"),
        )

    def _window_reference(self, anchors: list[datetime]) -> tuple[datetime | None, str | None]:
        """近期窗口的参照时刻：与解析锚点分离，可审计（C-12）。

        显式 `reference_at`（批次评分时间）优先；没给就用本簇观察锚点的最大值——它仍然
        来自观察本身，跨分析批次稳定，绝不取 `evidence.created_at`（分析时刻）。
        """
        if self.reference_at is not None:
            return self.reference_at, "batch_reference_at"
        if anchors:
            return max(anchors), "latest_observed_at"
        return None, None

    def assumptions(self) -> dict[str, object]:
        return {
            "consensus_cap": CONSENSUS_CAP,
            "rank_cap": RANK_CAP,
            "recent_window_days": RECENT_WINDOW_DAYS,
            "window_reference_at": (
                self.reference_at.isoformat() if self.reference_at is not None else None
            ),
            "family_method": FAMILY_METHOD,
            "quantile_min_sample": self.quantile_min_sample,
            "likes_are_not_demand": True,
            "posts_are_not_users": True,
        }

    def score(self) -> ScoreOutcome:
        needs = self.needs()
        # 分位数的样本就是本批要打分的簇数：低于门槛时整批标 provisional，
        # 而不是逐条判断——半批精确半批待验证的排行榜没人能复核。
        scored = tuple(self.score_need(need, sample_size=len(needs)) for need in needs)
        return ScoreOutcome(
            run_id=self.run_id,
            analysis_version=self.analysis_version,
            scored=scored,
            families=self.families,
            provisional=len(needs) < self.quantile_min_sample,
            weights=self.weights.as_dict(),
            assumptions=self.assumptions(),
        )


__all__ = [
    "CONSENSUS_CAP",
    "FAMILY_METHOD",
    "FamilyPlan",
    "NeedScore",
    "QUANTILE_PROVISIONAL_REASON",
    "RECENT_WINDOW_DAYS",
    "SCORING_METHOD",
    "SCORING_MODEL_VERSION",
    "ScoreOutcome",
    "DemandScorer",
    "build_families",
    "family_key",
    "log_scale",
    "parse_published_at",
]
