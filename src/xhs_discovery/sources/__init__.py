"""数据源适配层：fixture 与 CLI 实现都只依赖这里的抽象契约。"""

from xhs_discovery.sources.base import AccessNotStarted, SearchSource, unsupported_error

__all__ = ["AccessNotStarted", "SearchSource", "unsupported_error"]
