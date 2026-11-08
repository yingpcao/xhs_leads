"""相对路径校验与归属检查：RawArtifactRef 与 RuntimePaths 共用同一套规则。

原始产物只能落在本次 run 目录内，因此分两层把关：
1. 字符串层：绝对路径、盘符、UNC、根相对路径与任何形式的 `..`（`../`、`..\\`、混合分隔符）都拒绝；
2. 解析层：真正读写前把候选路径与容器都 `resolve()`（展开符号链接/junction）后再确认归属。
第 2 层是本模块存在的理由：词法 `is_relative_to` 看不出指向目录外的链接，
"防符号链接逃逸"必须是解析后的结论，不能只靠字符串规则宣称。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

ABSOLUTE_MESSAGE = "必须是 run 目录内的相对路径，不接受绝对路径、盘符、UNC 或根相对路径"
TRAVERSAL_MESSAGE = "不得包含 .. 目录穿越（含 Windows 反斜杠形式）"


def split_relative(value: str) -> tuple[str, ...]:
    """校验并返回路径片段；Windows 与 Posix 两种解析都要通过。"""
    if not value:
        raise ValueError(ABSOLUTE_MESSAGE)
    parsers = (PureWindowsPath(value), PurePosixPath(value))
    for parsed in parsers:
        if parsed.is_absolute() or parsed.drive or parsed.root:
            raise ValueError(f"{ABSOLUTE_MESSAGE}：{value}")
    for parsed in parsers:
        if ".." in parsed.parts:
            raise ValueError(f"{TRAVERSAL_MESSAGE}：{value}")
    return tuple(PurePosixPath(value.replace("\\", "/")).parts)


def ensure_inside(candidate: Path, container: Path) -> Path:
    """解析真实路径后确认目标仍在容器内，返回解析后的绝对路径。

    两侧都 resolve：只解析候选、容器仍为相对写法时，`is_relative_to` 会因根不同而误判。
    """
    resolved = Path(candidate).resolve()
    root = Path(container).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"解析后路径越出允许目录：{resolved} 不在 {root} 内")
    return resolved
