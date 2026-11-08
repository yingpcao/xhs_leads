"""持久化层（P0-B）：连接、迁移、Repository、预算账本、产物落盘与恢复、单 worker 控制。

表结构以 `docs/DATA_MODEL.md` 为唯一输入，枚举 CHECK 子句直接取自 `models.enums`，
避免 schema 与契约漂移。
"""
