"""资源检视测试：只读、无模型、未测项如实 unknown/unmeasured。"""

from __future__ import annotations

import json

from xhs_discovery.evaluation.resources import collect_resource_report, write_resource_report


def test_report_is_serializable_and_readonly():
    report = collect_resource_report()
    json.dumps(report, ensure_ascii=False)
    assert report["read_only"] is True
    assert report["model_loaded"] is False
    assert report["network_accessed"] is False


def test_unmeasured_fields_stay_unknown():
    report = collect_resource_report()
    assert report["inference_latency_budget"]["status"] == "unmeasured"
    assert report["inference_latency_budget"]["value_ms"] is None
    assert report["model_capacity_estimate"]["status"] == "unmeasured"
    assert report["p2_03_status"].startswith("INCOMPLETE")


def test_gpu_unknown_declared_not_guessed():
    report = collect_resource_report()
    assert report["gpus"] == [] or all(isinstance(g, dict) for g in report["gpus"])
    assert "gpus_unknown_reason" in report


def test_cpu_memory_facts_present_or_unknown():
    report = collect_resource_report()
    assert report["cpu"]["cores_logical"] is None or report["cpu"]["cores_logical"] >= 1
    assert report["memory"]["total_bytes_unknown"] == (report["memory"]["total_bytes"] is None)


def test_write_roundtrip(tmp_path):
    path = write_resource_report(tmp_path / "resource-report.json")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["version"] == "p2-resource-report-v1"
