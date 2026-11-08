"""真实路径解析与 run 归属（P0-A 复核遗留要求，P0-09 落盘读写的前置）。

字符串层拒绝 `..`/绝对路径/盘符/UNC 只挡住"写出来的路径"；指向目录外的符号链接或 junction
在词法上完全合法，必须靠解析后的归属检查拦住。无法创建链接的机器用 skip 明确说明，
不宣称已验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xhs_discovery.models.enums import OperationType
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.relpath import ensure_inside, split_relative


def test_ensure_inside_returns_resolved_path_and_accepts_inside_targets(tmp_path: Path):
    root = tmp_path / "runs" / "run-a"
    root.mkdir(parents=True)
    inside = root / "operations" / "k1" / "stdout.json"
    resolved = ensure_inside(root / ".." / "run-a" / "operations" / "k1" / "stdout.json", tmp_path)
    assert resolved == inside.resolve()
    assert ensure_inside(inside, root) == inside.resolve()


def test_ensure_inside_resolves_a_relative_container(tmp_path: Path, monkeypatch):
    """容器写成相对路径时也要能判定归属：只解析候选会因根不同而误判。"""
    (tmp_path / "runs" / "run-a").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    resolved = ensure_inside(Path("runs/run-a/x.json"), Path("runs/run-a"))
    assert resolved == (tmp_path / "runs" / "run-a" / "x.json").resolve()
    with pytest.raises(ValueError, match="解析后路径越出"):
        ensure_inside(Path("runs/run-a/../../escape.json"), Path("runs/run-a"))


def test_split_relative_still_rejects_lexical_escapes():
    for value in ("../a", r"..\\a", "/a", r"\a", "C:/a", "//srv/a"):
        with pytest.raises(ValueError):
            split_relative(value)


def test_run_confinement_blocks_link_pointing_outside_the_run(tmp_path: Path, make_dir_link):
    paths = RuntimePaths(tmp_path)
    run_dir = paths.run_dir("run-a")
    run_dir.mkdir(parents=True)
    outside = tmp_path / "other-place"
    outside.mkdir()
    (outside / "secret.json").write_text("{}", encoding="utf-8")

    link = run_dir / "leak"
    mechanism = make_dir_link(outside, link)
    if mechanism is None:
        pytest.skip("本机无权创建符号链接或 junction：链接逃逸路径未验证")

    rel = "runs/run-a/leak/secret.json"
    # 词法检查会放行：路径本身就在 run 目录里
    assert (run_dir / "leak" / "secret.json").is_relative_to(run_dir)
    # 解析后指向 run 目录外，必须拒绝
    with pytest.raises(ValueError, match="解析后路径越出"):
        paths.resolve_within_run(rel, "run-a")
    # 同一缺陷也不能从 runs 根校验中漏掉
    with pytest.raises(ValueError, match="解析后路径越出"):
        paths.resolve_artifact(rel)


def test_run_confinement_allows_targets_inside_the_same_run(tmp_path: Path):
    paths = RuntimePaths(tmp_path)
    target = paths.attempt_dir("run-a", 7, OperationType.suggest, 0, 1) / "stdout.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    rel = paths.relative(target)
    assert rel == "runs/run-a/operations/k7/suggest/r0/a1/stdout.json"
    assert paths.resolve_within_run(rel, "run-a") == target.resolve()


def test_artifacts_of_another_run_are_rejected_even_with_valid_syntax(tmp_path: Path):
    paths = RuntimePaths(tmp_path)
    other = paths.attempt_dir("run-b", 7, OperationType.suggest, 0, 1) / "stdout.json"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("{}", encoding="utf-8")
    rel = paths.relative(other)
    assert paths.resolve_within_run(rel, "run-b") == other.resolve()
    with pytest.raises(ValueError, match="解析后路径越出"):
        paths.resolve_within_run(rel, "run-a")


def test_relative_root_and_absolute_root_agree(tmp_path: Path, monkeypatch):
    """root 写成相对路径时，解析结果与绝对写法一致，归属判定不受 cwd 影响。"""
    monkeypatch.chdir(tmp_path)
    abs_paths = RuntimePaths(tmp_path / "data")
    rel_paths = RuntimePaths(Path("data"))
    target = abs_paths.attempt_dir("run-a", 1, OperationType.suggest, 0, 1) / "stderr.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    rel = abs_paths.relative(target)
    assert rel_paths.resolve_within_run(rel, "run-a") == abs_paths.resolve_within_run(rel, "run-a")
    assert rel_paths.resolve_within_run(rel, "run-a") == target.resolve()
