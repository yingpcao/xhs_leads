"""SQLite 连接与事务纪律（P0-06）。

`docs/DATA_MODEL.md` 第 6.6 条要求：外键开启、WAL、busy_timeout=5000，写事务保持短小。
这里把这三件事固定下来，避免各调用方自己拼 PRAGMA。
"""

from __future__ import annotations

import itertools
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from xhs_discovery.storage import migrations

PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

_SEQUENCE = itertools.count()


def _sequence() -> int:
    return next(_SEQUENCE)


def connect(path: Path | str, *, migrate: bool = True) -> sqlite3.Connection:
    """打开（并按需建库迁移）数据库；`:memory:` 不做 WAL（内存库不支持）。"""
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        if target == ":memory:" and "journal_mode" in pragma:
            continue
        conn.execute(pragma)
    if migrate:
        migrations.apply_migrations(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """唯一的写事务入口：事务所有权归最外层调用者。

    默认隔离级别的 `with conn:` 会无条件提交，包在别人的事务里就会把外层一起提交掉，
    批次原子性（观察/证据/预算要么全写、要么全不回）就没了。判定不需要额外状态：
    - 进入时 `conn.in_transaction` 为假 → 本层是最外层，BEGIN + COMMIT/ROLLBACK；
    - 进入时已经在事务里（外层是本函数的嵌套调用，或调用方自己 BEGIN/写过 DML）→ 只开 SAVEPOINT，
      失败回滚到保存点后继续上抛，绝不替外层提交，也不整体回滚别人的事务。
    """
    if conn.in_transaction:
        name = f"xhs_{_sequence()}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def pragma_value(conn: sqlite3.Connection, name: str) -> str:
    return str(conn.execute(f"PRAGMA {name}").fetchone()[0])
