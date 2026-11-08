"""P0-01 验收证据：隔离环境、依赖版本与"关闭模型仍可导入核心契约"。"""

from __future__ import annotations

import json
import subprocess
import sys

from importlib import metadata

HEAVY_MODULES = (
    "jieba",
    "sklearn",
    "streamlit",
    "torch",
    "transformers",
    "sentence_transformers",
    "plotly",
    "pyvis",
)

IMPORT_SNIPPET = """
import json, sys
import xhs_discovery
import xhs_discovery.config
import xhs_discovery.models
import xhs_discovery.sources.base
import xhs_discovery.paths
heavy = {m for m in sys.modules if m.split('.')[0] in %(heavy)r}
print(json.dumps(sorted(heavy)))
""" % {"heavy": HEAVY_MODULES}


def test_core_contracts_import_without_heavy_or_model_dependencies():
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_SNIPPET], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == [], "核心契约不得导入分词/TF-IDF/UI/模型依赖"


# P0-B/P0-C 全链路：存储、文本、fixture 与探索闭环都要能在没装模型依赖时也导入得起来。
OFFLINE_STACK_MODULES = (
    "storage.db", "storage.migrations", "storage.repository", "storage.ledger",
    "storage.artifacts", "storage.worker",
    "text.normalize", "text.intent", "text.similarity",
    "sources.fixture",
    "discovery.graph", "discovery.ingest", "discovery.operators", "discovery.priority",
    "discovery.scheduler", "discovery.merge", "discovery.extract", "discovery.stopping",
    "discovery.calibration", "discovery.cost", "discovery.engine",
)

OFFLINE_STACK_SNIPPET = """
import json, sys
for name in %(mods)r:
    __import__("xhs_discovery." + name)
heavy = {m for m in sys.modules if m.split('.')[0] in %(heavy)r}
print(json.dumps(sorted(heavy)))
""" % {"mods": OFFLINE_STACK_MODULES, "heavy": HEAVY_MODULES}


def test_offline_discovery_stack_imports_without_model_dependencies():
    """P0-C 的探索闭环不依赖分词/向量库/UI：装了它们也不能被导入。"""
    result = subprocess.run(
        [sys.executable, "-c", OFFLINE_STACK_SNIPPET], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == [], "离线探索栈不得导入模型、分词或 UI 依赖"


def test_documented_dependencies_are_installed_in_the_isolated_env():
    for name in ("pydantic", "PyYAML", "networkx", "streamlit", "jieba", "scikit-learn", "pytest"):
        version = metadata.version(name)
        assert version, f"{name} 未安装"


def test_pyproject_dependency_policy_has_no_model_extras_by_default():
    pyproject = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "sentence-transformers" in pyproject
    base, _, extras = pyproject.partition("[project.optional-dependencies]")
    assert "torch" not in base, "默认依赖不得包含模型依赖"
    assert "streamlit" in base
