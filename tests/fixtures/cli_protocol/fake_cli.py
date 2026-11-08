"""本地假 xhs-search CLI（供 SubprocessRunner 真实端到端调用，绝不联网、不读凭据）。

用法：XHS_FAKE_CASE=<case> python fake_cli.py <command> --seed <词> [--out <dir>] ...
按用例向 stdout 打印单个 JSON、向 stderr 打印进度、以对应码退出，并在 --out 目录写出工具产物。
大输出用例用于验证 stdout/stderr 并发读取不会阻塞管道；sleep 用例用于验证总超时只回收本次子进程。
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIOS = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))


def parse_args(argv):
    out = None
    command = None
    seed = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if i == 0:
            command = tok
        if tok == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
        if tok == "--seed" and i + 1 < len(argv):
            seed = argv[i + 1]
        i += 1
    return command, seed, out


def _out(text: str) -> None:
    import sys as _s
    _s.stdout.buffer.write(text.encode("utf-8"))
    _s.stdout.flush()


def _err(text: str) -> None:
    import sys as _s
    _s.stderr.buffer.write(text.encode("utf-8"))
    _s.stderr.flush()


def main() -> int:
    case = os.environ.get("XHS_FAKE_CASE", "suggest_ok")
    argv = sys.argv[1:]
    command, seed, out = parse_args(argv)

    if case == "sleep_forever":
        # 只在被终止时结束；测试用很短的 total timeout 触发回收，验证只杀自己启动的子进程
        _err("starting long operation\n")
        time.sleep(30)
        return 0

    if case == "echo_argv":
        # 把收到的参数原样吐回 stdout，验证 shell 字符/空格/引号仍是单个参数（不被拆分）
        payload = {"schemaVersion": "1.0", "cliVersion": "2.0.1", "command": command,
                   "ok": True, "exitCode": 0, "startedAt": "2026-10-28T08:21:30.111Z",
                   "data": {"argv": argv, "results": []}, "error": None}
        _out(json.dumps(payload, ensure_ascii=False))
        return 0

    if case == "big":
        many = [f"上海中考建议{i}" for i in range(2000)]  # 大 stdout
        env = {"schemaVersion": "1.0", "cliVersion": "2.0.1", "command": "suggest", "ok": True,
               "exitCode": 0, "startedAt": "2026-10-28T08:21:30.111Z",
               "data": {"results": [{"group": None, "seed": seed or "上海中考",
               "collectedAt": "2026-10-28T08:21:33.358Z", "status": "completed",
               "suggestions": many, "notes": [], "stopReason": None, "warnings": [],
               "error": None}], "outputDir": out}, "error": None}
        _err(("progress " * 4000) + "\n")  # 大 stderr
        _out(json.dumps(env, ensure_ascii=False) + "\n")
        return 0

    scenario = next((s for s in SCENARIOS["scenarios"] if s["name"] == case), None)
    if scenario is None:
        _err(f"unknown case {case}\n")
        return 99

    if out:
        outdir = Path(out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "response.json").write_text(scenario["stdout"], encoding="utf-8")
        (outdir / "results.json").write_text("[]", encoding="utf-8")

    _out(scenario["stdout"] + "\n")
    _err(scenario.get("stderr", ""))
    return int(scenario["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
