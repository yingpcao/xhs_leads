"""P2-C-DASHSCOPE-EMBEDDING smoke benchmark（可审查、单次访问、持久化原子 claim）。

授权边界：
- 唯一一次授权已在 r1 消耗（1 个 HTTP 请求，见 data/embeddings/dashscope_smoke_claim.json
  中的登记与 docs/coordination/automation/P2_DASHSCOPE_RESULT.json）；本脚本在联网前
  先原子占用持久化 claim 文件（O_CREAT|O_EXCL，独占创建），claim 已存在即拒绝联网，
  失败/崩溃后 claim 依然保留，不会自动重试，也没有 force 绕过；
- 最多 1 个 HTTP 请求：4 条预先写死的非敏感中文构造短句（每条 <=80 字，
  总计 <=320 字符），timeout=30s、retries=0；
- 只访问官方北京 endpoint https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings，
  只用模型 qwen3.7-text-embedding、维度 1024；
- 不发送项目/帖子/人工标签文本，不执行真实检索，不上传业务数据；
- 密钥只经 DASHSCOPE_API_KEY 环境变量在占用 claim 之后、调用函数内部读取；
  live 路径 claim-first：发行 claim 前不读取、不依赖任何密钥；密钥缺失即 BLOCKED，
  claim 已占用即失效（失败不重占、不重试）；
- 输出仅含安全字段：请求ID（若响应提供）、返回模型名、维度、usage、耗时、
  向量校验/哈希、成本估计；绝不输出密钥、header、原始错误 body；
- 失败即停止：不重试、不换地域/模型/账户、不降级合成。

用法（全部命令请加 .venv/Scripts/python.exe 前缀）：
    .venv/Scripts/python.exe scripts/p2_dashscope_smoke.py --check-only   # 零网络预检，不触碰 claim
    .venv/Scripts/python.exe scripts/p2_dashscope_smoke.py                # 尝试联网（claim 已消耗时拒绝）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from xhs_discovery.embeddings.cache import VectorCache, cache_key  # noqa: E402
from xhs_discovery.embeddings.dashscope import (  # noqa: E402
    DashScopeEmbeddingProvider,
    DashScopeProviderError,
    DOCUMENTED_TASK_TYPES,
)

# 4 条预先写死的非敏感中文构造短句（合成 benchmark 输入，不是 human gold、不是业务数据）。
SMOKE_TEXTS = [
    "周末想在上海苏州河边找一家安静的咖啡馆看书。",
    "新手学摄影应该先买便宜的定焦镜头，还是先用好手机？",
    "北京秋天适合带孩子去植物园认识哪些本地植物？",
    "求推荐适合通勤背的双肩包，希望轻便又防水。",
]

CACHE_PATH = PROJECT_ROOT / "data" / "embeddings" / "dashscope_benchmark_cache.sqlite3"
# 持久化单次 claim：联网前独占创建；r1 已消耗后此文件登记历史事实，之后任何运行都拒绝联网。
CLAIM_FILE = PROJECT_ROOT / "data" / "embeddings" / "dashscope_smoke_claim.json"
TASK_TYPE = DOCUMENTED_TASK_TYPES[0]
INSTRUCTION = ""
NORMALIZE = True


def _check_bounds() -> None:
    for text in SMOKE_TEXTS:
        if len(text) > 80:
            raise SystemExit("smoke 文本超出 80 字上限")
    if sum(len(t) for t in SMOKE_TEXTS) > 320:
        raise SystemExit("smoke 文本总量超出 320 字符上限")
    if len(SMOKE_TEXTS) > 4:
        raise SystemExit("smoke 输入条数超出授权")


def claim_single_use(claim_file: Path, record: dict) -> bool:
    """原子一次性占用：O_CREAT|O_EXCL 独占创建，已存在即返回 False（不覆盖、不重试）。

    并发进程同时抢占时恰有一个成功；创建后进程崩溃/失败，文件仍在，
    后续运行一律视为已消耗——不存在 force 绕过。"""
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(claim_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    return True


def _append_claim_note(claim_file: Path, note: dict) -> None:
    """claim 之后的补记（仅安全字段）；补记失败不影响 fail-closed 语义。"""
    try:
        data = json.loads(claim_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data.update(note)
        tmp = claim_file.parent / (claim_file.name + ".note.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(claim_file))
    except (OSError, ValueError):
        pass


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return "<external>"


def _run_live() -> int:
    """唯一一次真实调用路径：至多 1 个 HTTP 请求，失败即停止。"""
    provider = DashScopeEmbeddingProvider(dimension=1024, timeout_seconds=30.0)
    try:
        vectors, receipt = provider.embed_detailed(
            SMOKE_TEXTS, task_type=TASK_TYPE, instruction=INSTRUCTION, normalize=NORMALIZE
        )
    except DashScopeProviderError as exc:
        # 仅安全分类与 HTTP 状态；无 body/header/密钥；失败即停止（claim 已占用，不会重试）。
        print(json.dumps({
            "status": "FAILED",
            "category": exc.category,
            "http_status": exc.http_status,
            "attempts": provider.call_count,
            "retries": 0,
        }, ensure_ascii=False))
        return 1

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    identity = provider.cache_identity()
    with VectorCache(path=CACHE_PATH, expected_dim=provider.dimension) as cache:
        for text, vector in zip(SMOKE_TEXTS, vectors):
            key = cache_key(
                content=text,
                model_name=provider.model_name,
                model_version=provider.model_version,
                preprocess_version=provider.preprocess_version,
                task_type=TASK_TYPE,
                instruction=INSTRUCTION,
                normalize=NORMALIZE,
                synthetic=False,
                **identity,
            )
            cache.put(key, vector, model_name=provider.model_name,
                      model_version=provider.model_version, synthetic=False)
        cache_stats = cache.stats()

    print(json.dumps({
        "status": "OK",
        "attempts": provider.call_count,
        "retries": 0,
        "request_id": receipt.request_id,
        "returned_model": receipt.returned_model,
        "dimension": receipt.dimension,
        "usage": receipt.usage,
        "latency_seconds": receipt.latency_seconds,
        "estimated_cost_cny": receipt.estimated_cost_cny,
        "cost_note": "按公布标价 0.0005 元/千输入 token 估计，非账单",
        "vector_sha256": receipt.vector_sha256,
        "vectors_validated": "model/count/index/dimension/finite/nonzero-norm/stable-unit-norm(normalize=True) 全部通过",
        "security": {"key_logged": False, "headers_logged": False, "raw_body_logged": False},
        "input_provenance": "synthetic_constructed_chinese_sentences（非 human gold、非业务数据）",
        "cache": {"path": _display_path(CACHE_PATH), "stats": cache_stats,
                  "benchmark_only": True},
    }, ensure_ascii=False, indent=2))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    claim_file: Path | None = None,
    live_call: Callable[[], int] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="P2 DashScope 单次授权 smoke（持久化原子 claim）")
    parser.add_argument("--check-only", action="store_true",
                        help="只做安全预检（密钥存在性、输入边界），零网络，不触碰 claim")
    args = parser.parse_args(argv)

    claim = claim_file if claim_file is not None else CLAIM_FILE
    live = live_call if live_call is not None else _run_live

    _check_bounds()

    if args.check_only:
        provider = DashScopeEmbeddingProvider(dimension=1024, timeout_seconds=30.0)
        check = provider.security_check()
        print(json.dumps({"mode": "check-only", "network_used": False,
                          "claim_file": _display_path(claim),
                          "claim_consumed": claim.exists(), **check}, ensure_ascii=False))
        return 0 if check["api_key_present"] else 2

    # live 路径：claim-first——占用之前不读取、不依赖任何密钥，也不可能有网络动作。
    claimed = claim_single_use(claim, {
        "phase": "claimed_before_network",
        "authorization": "exactly 1 HTTP request, model qwen3.7-text-embedding, dim 1024, 4 synthetic texts",
    })
    if not claimed:
        print(json.dumps({
            "status": "ALREADY_CONSUMED",
            "detail": "单次授权已被占用（claim 文件存在）；不覆盖、不绕过、不再联网",
            "network_used": False,
            "claim_file": _display_path(claim),
        }, ensure_ascii=False))
        return 3

    provider = DashScopeEmbeddingProvider(dimension=1024, timeout_seconds=30.0)
    check = provider.security_check()
    if not check["api_key_present"]:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "missing_api_key",
            "detail": "DASHSCOPE_API_KEY 不存在；不寻找其他密钥来源，不执行任何网络请求；"
                      "claim 已占用，失败不重占、不重试",
            "network_used": False,
        }, ensure_ascii=False))
        _append_claim_note(claim, {"outcome": "blocked_missing_api_key", "network_used": False})
        return 2

    exit_code = live()
    _append_claim_note(claim, {"outcome": "ok" if exit_code == 0 else "failed_closed",
                               "exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
