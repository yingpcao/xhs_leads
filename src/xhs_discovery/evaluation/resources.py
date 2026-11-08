"""只读硬件/资源检视（P2-03 的离线部分）。

边界（DEVELOPMENT_PLAN.md:208、POST_P1_NEXT）：
- 只用标准库与本机可得元数据；不导入、不下载、不加载任何 ML 模型或权重；
- 采不到的信息一律 unknown，不猜测；
- 硬件事实不等于推理耗时：本模块不测任何模型延迟，
  inference_latency_budget 恒为 "unmeasured"，P2-03 在真实基准前保持未完成。
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

REPORT_VERSION = "p2-resource-report-v1"


def _memory_bytes() -> dict[str, Any]:
    """返回 {total_bytes, source}；取不到时 total_bytes=None。"""
    system = platform.system()
    if system == "Windows":

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return {"total_bytes": int(stat.ullTotalPhys), "source": "kernel32.GlobalMemoryStatusEx"}
        except (OSError, AttributeError):
            pass
    elif system == "Linux":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return {"total_bytes": kb * 1024, "source": "/proc/meminfo"}
        except (OSError, ValueError):
            pass
    elif system == "Darwin":
        try:
            raw = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            return {"total_bytes": int(raw), "source": "sysconf"}
        except (ValueError, OSError, AttributeError):
            pass
    return {"total_bytes": None, "source": "unavailable"}


def _gpus() -> list[dict[str, Any]]:
    """无依赖、只读的 GPU 元数据枚举。

    标准库没有可移植的 GPU 枚举；不启动外部探测进程、不加载 CUDA 运行时。
    因此 GPU 一律 unknown，这是事实而不是失败。
    """
    return []


def collect_resource_report() -> dict[str, Any]:
    cpu = {
        "cores_logical": os.cpu_count(),
        "platform_machine": platform.machine(),
        "processor": platform.processor() or None,
        "source": "os.cpu_count/platform (标准库元数据，未做负载测量)",
    }
    mem = _memory_bytes()
    return {
        "version": REPORT_VERSION,
        "read_only": True,
        "model_loaded": False,
        "network_accessed": False,
        "os": {"system": platform.system(), "release": platform.release(), "python": sys.version.split()[0]},
        "cpu": cpu,
        "memory": {
            "total_bytes": mem["total_bytes"],
            "total_bytes_unknown": mem["total_bytes"] is None,
            "source": mem["source"],
        },
        "gpus": _gpus(),
        "gpus_unknown_reason": "标准库无法可移植地枚举 GPU；未启动外部探测、未加载 CUDA",
        "inference_latency_budget": {
            "status": "unmeasured",
            "value_ms": None,
            "reason": "硬件事实不等于推理耗时；Qwen3-Embedding-0.6B 或替代模型的本机基准尚未获准执行",
        },
        "model_capacity_estimate": {
            "status": "unmeasured",
            "note": "0.6B fp32 约需 2.4GB 权重内存（公开参数量的算术估算），仅作预算线索，不构成可运行结论",
        },
        "p2_03_status": "INCOMPLETE：真实模型基准与权重版本记录需要单独授权后进行",
    }


def write_resource_report(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(collect_resource_report(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(json.dumps(collect_resource_report(), ensure_ascii=False, indent=2))
