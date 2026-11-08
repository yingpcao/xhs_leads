"""发现图读取视图（P0-14）。

`run_keywords` 只保存**首次**归属（父、深度、分支），用于调度与展示的稳定归属；
完整的多父关系在 `discovery_edges`，每条边带平台推荐排名、会话与观察时间。
两者分工在 DATA_MODEL 第 4 节已定，本模块是它唯一的读取入口：
调度、评分与导出都从这里拿，避免各处自己 JOIN 出不同口径。

路径枚举必须封顶：数据集里刻意包含环路（父词被下级联想回指），
不做环保护的递归会在展示"发现路径"时死循环。
"""

from __future__ import annotations

from dataclasses import dataclass

from xhs_discovery.models.enums import CandidateOrigin
from xhs_discovery.storage.repository import Repository

MAX_PATH_DEPTH = 8


@dataclass(frozen=True)
class GraphEdge:
    parent_keyword_id: int
    child_keyword_id: int
    origin: CandidateOrigin
    rank: int | None
    session_id: str | None
    observed_at: str
    operation_id: int | None

    def as_dict(self) -> dict:
        return {
            "parent_keyword_id": self.parent_keyword_id,
            "child_keyword_id": self.child_keyword_id,
            "origin": self.origin.value,
            "rank": self.rank,
            "session_id": self.session_id,
            "observed_at": self.observed_at,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class GraphNode:
    keyword_id: int
    normalized: str
    origin: str
    depth: int
    status: str
    branch_key: str | None
    first_seen_at: str
    last_seen_at: str
    parent_keyword_id: int | None
    revisit_count: int

    def as_dict(self) -> dict:
        return {
            "keyword_id": self.keyword_id,
            "normalized": self.normalized,
            "origin": self.origin,
            "depth": self.depth,
            "status": self.status,
            "branch_key": self.branch_key,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "parent_keyword_id": self.parent_keyword_id,
            "revisit_count": self.revisit_count,
        }


def _edges(rows) -> tuple[GraphEdge, ...]:
    return tuple(
        GraphEdge(
            parent_keyword_id=int(row["parent_keyword_id"]),
            child_keyword_id=int(row["child_keyword_id"]),
            origin=CandidateOrigin(str(row["origin"])),
            rank=None if row["rank"] is None else int(row["rank"]),
            session_id=row["session_id"],
            observed_at=str(row["observed_at"]),
            operation_id=None if row["operation_id"] is None else int(row["operation_id"]),
        )
        for row in rows
    )


class DiscoveryGraph:
    def __init__(self, repo: Repository, run_id: str) -> None:
        self.repo = repo
        self.run_id = run_id
        self.conn = repo.conn

    # ------------------------------------------------------------------ 节点与边

    def node(self, keyword_id: int) -> GraphNode | None:
        row = self.conn.execute(
            "SELECT rk.keyword_id, k.normalized_text, rk.origin, rk.depth, rk.status,"
            " rk.first_seen_at, rk.last_seen_at, rk.parent_keyword_id, rk.revisit_count,"
            " b.branch_key FROM run_keywords rk"
            " JOIN keywords k ON k.id = rk.keyword_id"
            " LEFT JOIN branches b ON b.id = rk.branch_id"
            " WHERE rk.run_id = ? AND rk.keyword_id = ?",
            (self.run_id, keyword_id),
        ).fetchone()
        if row is None:
            return None
        return GraphNode(
            keyword_id=int(row["keyword_id"]),
            normalized=str(row["normalized_text"]),
            origin=str(row["origin"]),
            depth=int(row["depth"]),
            status=str(row["status"]),
            branch_key=row["branch_key"],
            first_seen_at=str(row["first_seen_at"]),
            last_seen_at=str(row["last_seen_at"]),
            parent_keyword_id=None if row["parent_keyword_id"] is None else int(row["parent_keyword_id"]),
            revisit_count=int(row["revisit_count"]),
        )

    def nodes(self, *, status: str | None = None) -> tuple[GraphNode, ...]:
        sql = (
            "SELECT rk.keyword_id, k.normalized_text, rk.origin, rk.depth, rk.status,"
            " rk.first_seen_at, rk.last_seen_at, rk.parent_keyword_id, rk.revisit_count,"
            " b.branch_key FROM run_keywords rk"
            " JOIN keywords k ON k.id = rk.keyword_id"
            " LEFT JOIN branches b ON b.id = rk.branch_id WHERE rk.run_id = ?"
        )
        params: list = [self.run_id]
        if status is not None:
            sql += " AND rk.status = ?"
            params.append(status)
        sql += " ORDER BY rk.depth, rk.keyword_id"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return tuple(
            GraphNode(
                keyword_id=int(row["keyword_id"]),
                normalized=str(row["normalized_text"]),
                origin=str(row["origin"]),
                depth=int(row["depth"]),
                status=str(row["status"]),
                branch_key=row["branch_key"],
                first_seen_at=str(row["first_seen_at"]),
                last_seen_at=str(row["last_seen_at"]),
                parent_keyword_id=(
                    None if row["parent_keyword_id"] is None else int(row["parent_keyword_id"])
                ),
                revisit_count=int(row["revisit_count"]),
            )
            for row in rows
        )

    def edges(self, *, origin: CandidateOrigin | None = None) -> tuple[GraphEdge, ...]:
        sql = (
            "SELECT parent_keyword_id, child_keyword_id, origin, rank, session_id, observed_at,"
            " operation_id FROM discovery_edges WHERE run_id = ?"
        )
        params: list = [self.run_id]
        if origin is not None:
            sql += " AND origin = ?"
            params.append(origin.value)
        sql += " ORDER BY id"
        return _edges(self.conn.execute(sql, tuple(params)).fetchall())

    def parents(self, child_keyword_id: int) -> tuple[GraphEdge, ...]:
        """所有父边：多父词必须完整保留，首个父词只用于稳定归属。"""
        rows = self.conn.execute(
            "SELECT parent_keyword_id, child_keyword_id, origin, rank, session_id, observed_at,"
            " operation_id FROM discovery_edges WHERE run_id = ? AND child_keyword_id = ?"
            " ORDER BY rank IS NULL, rank, id",
            (self.run_id, child_keyword_id),
        ).fetchall()
        return _edges(rows)

    def children(self, parent_keyword_id: int) -> tuple[GraphEdge, ...]:
        rows = self.conn.execute(
            "SELECT parent_keyword_id, child_keyword_id, origin, rank, session_id, observed_at,"
            " operation_id FROM discovery_edges WHERE run_id = ? AND parent_keyword_id = ?"
            " ORDER BY rank IS NULL, rank, id",
            (self.run_id, parent_keyword_id),
        ).fetchall()
        return _edges(rows)

    # ------------------------------------------------------------------ 路径与观察

    def paths_to(self, keyword_id: int, *, max_depth: int = MAX_PATH_DEPTH) -> tuple[tuple[int, ...], ...]:
        """枚举 root→keyword 的全部父路径（id 序列，末尾是自身）。

        环保护：路径上已出现的父节点不再展开；深度上限只限制长度，不裁剪多父。
        被裁剪的环在 `describe()` 里显式报告，避免"看起来只有一条路径"的假象。
        """
        results: list[tuple[int, ...]] = []
        truncated = 0

        def walk(current: int, trail: tuple[int, ...]) -> None:
            nonlocal truncated
            if len(trail) > max_depth:
                truncated += 1
                return
            parents = [edge.parent_keyword_id for edge in self.parents(current)]
            if not parents:
                results.append(tuple(reversed(trail)))
                return
            for parent in parents:
                if parent in trail:
                    # 环路：保留可达前缀并计数，不重复展开
                    truncated += 1
                    results.append(tuple(reversed(trail)))
                    continue
                walk(parent, trail + (parent,))

        node = self.node(keyword_id)
        if node is None:
            return ()
        walk(keyword_id, (keyword_id,))
        unique = sorted(set(results))
        if truncated:
            # 截断信息也要可复核：静默少几条路径等于伪造图
            self.repo.record_event(
                self.run_id,
                "graph_path_truncated",
                detail={"keyword_id": keyword_id, "skipped_branches": truncated, "max_depth": max_depth},
            )
        return tuple(unique)

    def observation_stats(self, keyword_id: int) -> dict:
        """队列去重不得丢失观察：这里统计该词在 run 内被观察到的次数与最好排名。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS seen, MIN(si.rank) AS best_rank, COUNT(DISTINCT so.session_id)"
            " AS sessions FROM suggestion_items si"
            " JOIN suggestion_observations so ON so.id = si.observation_id"
            " WHERE so.run_id = ? AND si.keyword_id = ?",
            (self.run_id, keyword_id),
        ).fetchone()
        return {
            "observed_times": int(row["seen"]) if row else 0,
            "best_rank": None if row is None or row["best_rank"] is None else int(row["best_rank"]),
            "distinct_sessions": int(row["sessions"]) if row else 0,
        }

    def independent_parent_count(self, keyword_id: int) -> int:
        """C 特征分子：不同父词才算独立共识；同一会话重复返回不增加。"""
        rows = self.conn.execute(
            "SELECT DISTINCT parent_keyword_id FROM discovery_edges"
            " WHERE run_id = ? AND child_keyword_id = ? AND origin = 'observed'",
            (self.run_id, keyword_id),
        ).fetchall()
        return len(rows)

    def describe(self, keyword_id: int) -> dict:
        node = self.node(keyword_id)
        if node is None:
            raise ValueError(f"关键词 {keyword_id} 不在 run {self.run_id} 的发现图里")
        return {
            **node.as_dict(),
            "aliases": self.repo.keyword_aliases(keyword_id),
            "parents": [edge.as_dict() for edge in self.parents(keyword_id)],
            "paths_to_root": [" > ".join(str(item) for item in path) for path in self.paths_to(keyword_id)],
            "observations": self.observation_stats(keyword_id),
            "independent_parents": self.independent_parent_count(keyword_id),
        }


__all__ = ["DiscoveryGraph", "GraphEdge", "GraphNode", "MAX_PATH_DEPTH"]
