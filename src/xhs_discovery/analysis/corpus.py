"""规则分析的语料读取（P0-24 的证据来源）。

分析层只从库里重算，不接受调用方递进来的内存对象——这样"分析看到的事实"和"采集落盘
的事实"是同一份，换进程、换时序都不会得到不同结论。三种文本来源各有一条必须遵守的口径：

- **标题**：复用 `discovery.ingest.replayed_posts`，取"该次观察当时可见"的标题版本。
  恢复重放与分析必须走同一条选择规则，否则同一篇帖子的证据会在两条路径上指向不同版本。
- **正文**：只认来源**自洽**的观察行（C-09/C-15）——`bo.run_id` 与操作同 run、操作类型确为
  body、attempt 真属于该操作。帖子身份与内容版本是全局的，"我读到过这篇正文"是按 run 发生的
  事；来源缺失或不自洽（借来别的 run 的操作、挂错操作的尝试）的旧行一律不认领。
- **联想**：用 `suggestion_versions`（观察当时的原文与哈希）而不是 `keywords` 表，
  因为联想文本会变，而证据必须钉在"当时看到的那一行"上。

每条 `TextUnit` 都自带能重建 `EvidenceProvenance` 的全部来源字段：分析产出的证据因此
不需要采集时的对象，只靠库里的 operations/attempts/observations 就能回溯到操作与尝试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from xhs_discovery.discovery.ingest import replayed_posts
from xhs_discovery.models.enums import OperationType, ResultState, SourceField
from xhs_discovery.models.source import KeywordText, OperationContext
from xhs_discovery.models.evidence import EvidenceProvenance

SUCCESSFUL_STATES = (ResultState.completed.value, ResultState.partial.value)

# 语料固定按 标题 → 正文 → 联想 排序，同一库两次读取给出同一个序列。
_SOURCE_ORDER = {
    SourceField.title: 0,
    SourceField.body: 1,
    SourceField.suggestion: 2,
}


@dataclass(frozen=True)
class TextUnit:
    """一个可定位的文本单元：容器原文 + 它落在哪张版本表 + 来源操作。"""

    source_field: SourceField
    run_id: str
    text: str
    content_version_id: int | None
    suggestion_version_id: int | None
    observation_id: int
    operation_id: int
    operation_type: OperationType
    revisit_seq: int
    attempt_no: int
    session_id: str
    keyword_id: int
    keyword: KeywordText
    note_id: str | None
    post_id: int | None
    result_rank: int | None
    published_at_text: str | None
    likes_text: str | None
    observed_at: datetime
    synthetic: bool
    # 发布时间文本所属的卡片观察与它的观察时刻（C-12）：解析相对日期只认这个锚点，
    # 不借正文抓取时刻，也不借分析批次时间。联想没有卡片，恒为 None。
    card_observation_id: int | None = None
    card_observed_at: datetime | None = None

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        return (
            _SOURCE_ORDER[self.source_field],
            self.observation_id,
            self.operation_id,
            self.result_rank if self.result_rank is not None else 0,
        )

    def operation_context(self) -> OperationContext:
        return OperationContext(
            run_id=self.run_id,
            keyword_id=self.keyword_id,
            keyword=self.keyword,
            operation_type=self.operation_type,
            revisit_seq=self.revisit_seq,
            attempt_no=self.attempt_no,
        )

    def provenance(self, run_id: str) -> EvidenceProvenance:
        return EvidenceProvenance(
            run_id=run_id,
            operation=self.operation_context(),
            session_id=self.session_id,
            keyword=self.keyword,
            note_id=self.note_id,
            suggestion_rank=self.result_rank if self.source_field is SourceField.suggestion else None,
        )

    def source_ref(self) -> dict[str, object]:
        """写进事件的可审计引用：不只有 id，还带能人工核对的文本定位信息。"""
        return {
            "source_field": self.source_field.value,
            "observation_id": self.observation_id,
            "operation_id": self.operation_id,
            "attempt_no": self.attempt_no,
            "keyword_id": self.keyword_id,
            "content_version_id": self.content_version_id,
            "suggestion_version_id": self.suggestion_version_id,
            "card_observation_id": self.card_observation_id,
            "note_id": self.note_id,
            "result_rank": self.result_rank,
        }


@dataclass(frozen=True)
class Corpus:
    run_id: str
    units: tuple[TextUnit, ...]

    def by_source(self, source_field: SourceField) -> tuple[TextUnit, ...]:
        return tuple(unit for unit in self.units if unit.source_field is source_field)

    def summary(self) -> dict[str, object]:
        counts = {
            field.value: len(self.by_source(field))
            for field in (SourceField.title, SourceField.body, SourceField.suggestion)
        }
        return {
            "run_id": self.run_id,
            "units": len(self.units),
            **counts,
            "keywords": len({unit.keyword_id for unit in self.units}),
            "posts": len({unit.post_id for unit in self.units if unit.post_id is not None}),
            "synthetic": any(unit.synthetic for unit in self.units),
        }

    def texts(self) -> tuple[str, ...]:
        """短语发现的输入：保持语料顺序，只给文本。"""
        return tuple(unit.text for unit in self.units)


def _keyword_texts(conn) -> dict[int, KeywordText]:
    """关键词身份：规范化文本进 `keywords`，原始写法进 `keyword_aliases`。

    取首次登记的别名作 raw——同一个规范化词在不同 run 可能有不同写法，证据引用要稳定，
    不能随机挑一个。没有别名时退回规范化文本（种子词直接入库就只有这一种写法）。
    """
    rows = conn.execute("SELECT id, normalized_text FROM keywords").fetchall()
    alias_rows = conn.execute(
        "SELECT keyword_id, raw_text FROM keyword_aliases ORDER BY keyword_id, id"
    ).fetchall()
    first_alias: dict[int, str] = {}
    for row in alias_rows:
        first_alias.setdefault(int(row["keyword_id"]), str(row["raw_text"]))
    return {
        int(row["id"]): KeywordText(
            raw=first_alias.get(int(row["id"]), str(row["normalized_text"])),
            normalized=str(row["normalized_text"]),
        )
        for row in rows
    }


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp))


def title_units(conn, run_id: str, keywords: dict[int, KeywordText]) -> list[TextUnit]:
    """卡片标题：走重放同一套可见版本规则，缺失标题不成语料（不是"空文本问题"）。"""
    headers = conn.execute(
        "SELECT o.id, o.operation_id, o.keyword_id, o.session_id, o.observed_at, o.state,"
        " o.synthetic, op.revisit_seq, op.operation_type, att.attempt_no"
        " FROM post_observations o JOIN operations op ON op.id = o.operation_id"
        " JOIN operation_attempts att ON att.id = o.attempt_id"
        f" WHERE o.run_id = ? AND o.state IN {SUCCESSFUL_STATES}"
        " ORDER BY o.id",
        (run_id,),
    ).fetchall()
    units: list[TextUnit] = []
    for header in headers:
        observation_id = int(header["id"])
        observed_at = str(header["observed_at"])
        keyword_id = int(header["keyword_id"])
        for post in replayed_posts(conn, observation_id, observed_at):
            if not post.title or post.title_version_id is None:
                continue
            units.append(
                TextUnit(
                    source_field=SourceField.title,
                    text=post.title,
                    content_version_id=post.title_version_id,
                    suggestion_version_id=None,
                    observation_id=observation_id,
                    operation_id=int(header["operation_id"]),
                    operation_type=OperationType(str(header["operation_type"])),
                    revisit_seq=int(header["revisit_seq"]),
                    attempt_no=int(header["attempt_no"]),
                    session_id=str(header["session_id"]),
                    keyword_id=keyword_id,
                    keyword=keywords[keyword_id],
                    note_id=post.note_id,
                    post_id=post.post_id,
                    result_rank=post.result_rank,
                    published_at_text=post.published_at_text,
                    likes_text=post.likes_text,
                    observed_at=_parse(observed_at),
                    synthetic=bool(header["synthetic"]),
                    run_id=run_id,
                    card_observation_id=observation_id,
                    card_observed_at=_parse(observed_at),
                )
            )
    return units


def body_units(conn, run_id: str, keywords: dict[int, KeywordText]) -> list[TextUnit]:
    """正文：只认领来源三列齐全的观察行；空正文与无版本的观察不是语料。"""
    rows = conn.execute(
        "SELECT bo.id, bo.operation_id, bo.session_id, bo.captured_at, bo.content_version_id,"
        " bo.post_id, p.note_id, op.keyword_id, op.operation_type, op.revisit_seq,"
        " att.attempt_no, r.synthetic_fixture"
        " FROM body_observations bo"
        " JOIN operations op ON op.id = bo.operation_id"
        "  AND op.run_id = bo.run_id AND op.operation_type = 'body'"
        " JOIN operation_attempts att ON att.id = bo.attempt_id"
        "  AND att.operation_id = bo.operation_id"
        " JOIN posts p ON p.id = bo.post_id"
        " JOIN runs r ON r.id = bo.run_id"
        " WHERE bo.run_id = ? AND bo.state = 'completed'"
        " AND bo.content_version_id IS NOT NULL"
        " ORDER BY bo.id",
        (run_id,),
    ).fetchall()
    units: list[TextUnit] = []
    for row in rows:
        text_row = conn.execute(
            "SELECT text FROM post_content_versions WHERE id = ? AND field = 'body'"
            " AND is_missing = 0",
            (int(row["content_version_id"]),),
        ).fetchone()
        if text_row is None or not str(text_row["text"]).strip():
            continue
        keyword_id = int(row["keyword_id"])
        observed_at = _parse(str(row["captured_at"]))
        # 正文所属的卡片观察决定 result_rank 与可见字段；查不到就承认不知道（NULL），
        # 不拿一个假的排名补位——排名会进评分与展示顺序。发布时间文本属于**这张卡片
        # 观察**，它的观察时刻要一并保留（card_* 字段）：相对日期按它解析，不能借
        # 正文 captured_at 或分析批次时间（C-12）。
        card = conn.execute(
            "SELECT pq.result_rank, pq.published_at_text, pq.likes_text, po.synthetic,"
            " po.id, po.observed_at"
            " FROM post_queries pq"
            " JOIN post_observations po ON po.id = pq.post_observation_id"
            " WHERE pq.run_id = ? AND pq.post_id = ? AND po.state IN (?, ?)"
            " ORDER BY po.id, pq.result_rank LIMIT 1",
            (run_id, int(row["post_id"]), *SUCCESSFUL_STATES),
        ).fetchone()
        units.append(
            TextUnit(
                source_field=SourceField.body,
                text=str(text_row["text"]),
                content_version_id=int(row["content_version_id"]),
                suggestion_version_id=None,
                observation_id=int(row["id"]),
                operation_id=int(row["operation_id"]),
                operation_type=OperationType(str(row["operation_type"])),
                revisit_seq=int(row["revisit_seq"]),
                attempt_no=int(row["attempt_no"]),
                session_id=str(row["session_id"]),
                keyword_id=keyword_id,
                keyword=keywords[keyword_id],
                note_id=str(row["note_id"]),
                post_id=int(row["post_id"]),
                result_rank=None if card is None else int(card["result_rank"]),
                published_at_text=None if card is None else card["published_at_text"],
                likes_text=None if card is None else card["likes_text"],
                observed_at=observed_at,
                synthetic=bool(card["synthetic"]) if card is not None else bool(row["synthetic_fixture"]),
                run_id=run_id,
                card_observation_id=None if card is None else int(card["id"]),
                card_observed_at=None if card is None else _parse(str(card["observed_at"])),
            )
        )
    return units


def suggestion_units(conn, run_id: str, keywords: dict[int, KeywordText]) -> list[TextUnit]:
    """联想原文：按 rank 取观察当时的版本行，rank 同时是证据的 suggestion_rank。"""
    headers = conn.execute(
        "SELECT o.id, o.operation_id, o.keyword_id, o.session_id, o.observed_at, o.synthetic,"
        " op.revisit_seq, att.attempt_no FROM suggestion_observations o"
        " JOIN operations op ON op.id = o.operation_id"
        " JOIN operation_attempts att ON att.id = o.attempt_id"
        f" WHERE o.run_id = ? AND o.state IN {SUCCESSFUL_STATES} ORDER BY o.id",
        (run_id,),
    ).fetchall()
    units: list[TextUnit] = []
    for header in headers:
        observation_id = int(header["id"])
        rows = conn.execute(
            "SELECT v.id, v.rank, v.text FROM suggestion_versions v"
            " WHERE v.observation_id = ? ORDER BY v.rank",
            (observation_id,),
        ).fetchall()
        for row in rows:
            if not str(row["text"]).strip():
                continue
            keyword_id = int(header["keyword_id"])
            units.append(
                TextUnit(
                    source_field=SourceField.suggestion,
                    text=str(row["text"]),
                    content_version_id=None,
                    suggestion_version_id=int(row["id"]),
                    observation_id=observation_id,
                    operation_id=int(header["operation_id"]),
                    operation_type=OperationType.suggest,
                    revisit_seq=int(header["revisit_seq"]),
                    attempt_no=int(header["attempt_no"]),
                    session_id=str(header["session_id"]),
                    keyword_id=keyword_id,
                    keyword=keywords[keyword_id],
                    note_id=None,
                    post_id=None,
                    result_rank=int(row["rank"]),
                    published_at_text=None,
                    likes_text=None,
                    observed_at=_parse(str(header["observed_at"])),
                    synthetic=bool(header["synthetic"]),
                    run_id=run_id,
                )
            )
    return units


def load_corpus(conn, run_id: str) -> Corpus:
    """一次 run 的全部可分析文本单元，顺序确定、可重放。"""
    if not run_id:
        raise ValueError("分析语料必须按 run 读取：跨 run 混语料会让同一句话在不同任务里算出不同分数")
    keywords = _keyword_texts(conn)
    units = [
        *title_units(conn, run_id, keywords),
        *body_units(conn, run_id, keywords),
        *suggestion_units(conn, run_id, keywords),
    ]
    return Corpus(run_id=run_id, units=tuple(sorted(units, key=lambda u: u.sort_key)))


__all__ = ["Corpus", "SUCCESSFUL_STATES", "TextUnit", "load_corpus"]
