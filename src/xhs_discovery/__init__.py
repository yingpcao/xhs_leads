"""小红书长尾需求发现引擎 —— P0 离线闭环。

核心契约（config/models/sources）不得在导入时依赖 jieba、scikit-learn、
streamlit 或任何模型重型依赖；这些只在对应能力开启时延迟导入。
"""

from xhs_discovery._version import CODE_VERSION, PROTOCOL_VERSION

__all__ = ["CODE_VERSION", "PROTOCOL_VERSION"]
