"""本地假 xhs-search CLI：按"计划文件"应答，供 live 流水线端到端测试（绝不联网、不读凭据）。

与 `fake_cli.py`（单用例协议核验）的分工：那份只验证协议解释的严格性，本份要支撑**一整条
run**——引擎会为不同关键词、不同 attempt 反复调用，所以这里必须按 (command, seed) 分发、
并且能表达"同一个键第 1 次失败、第 2 次成功"这类重试剧本。

调用方式（与真实适配器一致，多余参数一律忽略）：

    XHS_FAKE_PLAN=<plan.json> XHS_FAKE_STATE=<state.json> XHS_FAKE_LOG=<calls.jsonl> \
        python fake_pipeline_cli.py \
        suggest --seed 上海中考 --profile default --browser msedge --timeout 60000 --out <dir>

`XHS_FAKE_LOG` 每行记一次真实调用（墙钟时间戳、command/seed、命中的步骤序号、收到的 argv、
启动本进程的解释器 `interpreter` 与 `invoked_as`），用于核对 live 的退避是否真的等过、
重放是否真的没再访问、发出去的是不是快照里固化的那份工具。

计划文件：

    {"steps": [
        {"command": "suggest", "seed": "上海中考", "times": 1, "returncode": 5,
         "status": "failed", "word_error": {"code": "NETWORK_ERROR", "message": "网络"}},
        {"command": "suggest", "returncode": 0, "suggestions": ["上海中考 分数线"]}
    ]}

- `match` 只比 command 与（可选的）seed；`times` 缺省=不限次，用尽后自动落到后面的同类步骤；
- 未匹配到任何步骤时以 99 退出并写 stderr：这是测试计划写错，不让它伪装成平台返回；
- 步骤可选 `sleep_seconds`（触发适配器总超时）、`rows`（直接给出逐词结果，用于多词/错词剧本）、
  `no_rows`（省略 data.results）、`raw_stdout`（原样输出的字符串，用于损坏 JSON/空 stdout）、
  `tool_files`（额外写到 --out 的工具产物名与内容）。
"""
import json
import os
import sys
import time
from pathlib import Path


def parse_args(argv):
    command = argv[0] if argv else None
    out = None
    seed = None
    limit = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
        elif tok == "--seed" and i + 1 < len(argv):
            seed = argv[i + 1]
        elif tok == "--limit" and i + 1 < len(argv):
            limit = argv[i + 1]
        i += 1
    return command, seed, out, limit


def _write(stream, text: str) -> None:
    stream.buffer.write(text.encode("utf-8"))
    stream.flush()


def _load_state(path: str | None) -> dict:
    if not path:
        return {}
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _log_call(command: str | None, seed: str | None, step_index: int, argv: list[str]) -> None:
    """每次真实调用记一行时间戳与 argv：live 的退避与"重放不再访问"必须能按墙钟核对。

    `argv` 是本进程收到的参数（不含解释器与脚本路径）；`interpreter`/`invoked_as` 另记启动
    用的可执行文件与脚本绝对路径——固化那套工具路径是否真的被用上，只能这样核对。
    """
    path = os.environ.get("XHS_FAKE_LOG")
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "at": time.time(),
                    "command": command,
                    "seed": seed,
                    "step": step_index,
                    "pid": os.getpid(),
                    "argv": argv,
                    "interpreter": sys.executable,
                    "invoked_as": sys.argv[:1],
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _save_state(path: str | None, state: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _match(step: dict, command: str | None, seed: str | None) -> bool:
    if step.get("command") not in (None, command):
        return False
    declared = step.get("seed")
    return declared in (None, seed)


def _pick(steps: list[dict], state: dict, command: str | None, seed: str | None):
    for index, step in enumerate(steps):
        if not _match(step, command, seed):
            continue
        times = step.get("times")
        used = int(state.get(str(index), 0))
        if times is None or used < int(times):
            state[str(index)] = used + 1
            return index, step
    return None


def _row(step: dict, seed: str, command: str) -> dict:
    suggestions = list(step.get("suggestions") or [])
    notes = list(step.get("notes") or [])
    status = step.get("status") or ("completed" if (suggestions or notes or step.get("explicit_empty")) else "failed")
    return {
        "group": None,
        "seed": step.get("row_seed", seed),
        "collectedAt": step.get("collectedAt") or "2026-10-28T08:21:33.358Z",
        "status": status,
        "suggestions": suggestions,
        "notes": notes,
        "stopReason": step.get("stopReason"),
        "warnings": list(step.get("warnings") or []),
        "error": step.get("word_error"),
    }


def _envelope(step: dict, command: str, seed: str, out: str | None) -> str:
    rc = int(step.get("returncode", 0))
    if "rows" in step:
        rows = list(step["rows"] or [])
    elif step.get("no_rows"):
        rows = []
    else:
        rows = [_row(step, seed, command)]
    data = {"results": rows, "outputDir": out}
    if step.get("no_data"):
        data = None
    return json.dumps(
        {
            "schemaVersion": step.get("schemaVersion", "1.0"),
            "cliVersion": step.get("cliVersion", "2.0.1"),
            "command": step.get("envelope_command", command),
            "ok": step.get("ok", rc == 0),
            "exitCode": step.get("envelope_exit_code", rc),
            "startedAt": step.get("startedAt") or "2026-10-28T08:21:30.111Z",
            "data": data,
            "error": step.get("top_error"),
        },
        ensure_ascii=False,
    )


def main() -> int:
    argv = sys.argv[1:]
    command, seed, out, limit = parse_args(argv)
    plan_path = os.environ.get("XHS_FAKE_PLAN")
    state_path = os.environ.get("XHS_FAKE_STATE")
    if not plan_path:
        _write(sys.stderr, "缺少 XHS_FAKE_PLAN：测试计划未提供，不接受任何默认回答\n")
        return 99
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    state = _load_state(state_path)
    picked = _pick(list(plan.get("steps") or []), state, command, seed)
    if picked is None:
        _write(sys.stderr, f"计划里没有 {command}/{seed} 的可用步骤：这是测试计划缺失，不伪装平台回答\n")
        return 99
    step_index, step = picked
    _save_state(state_path, state)
    _log_call(command, seed, step_index, argv)

    if step.get("sleep_seconds"):
        time.sleep(float(step["sleep_seconds"]))

    if out:
        outdir = Path(out)
        outdir.mkdir(parents=True, exist_ok=True)
        files = dict(step.get("tool_files") or {})
        if "response.json" not in files and not step.get("no_tool_files"):
            files["response.json"] = json.dumps(
                {"command": command, "seed": seed, "limit": limit,
                 "results": step.get("rows") or [ _row(step, seed or "", command)]},
                ensure_ascii=False,
            )
        for name, text in files.items():
            (outdir / name).write_text(str(text), encoding="utf-8")

    stdout = step["raw_stdout"] if "raw_stdout" in step else _envelope(step, command or "", seed or "", out)
    if stdout:
        _write(sys.stdout, stdout + "\n")
    _write(sys.stderr, str(step.get("stderr", "progress\n")))
    return int(step.get("returncode", 0))


if __name__ == "__main__":
    raise SystemExit(main())
