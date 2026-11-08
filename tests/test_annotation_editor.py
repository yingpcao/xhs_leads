"""All identities and reviews here are test data, never actual human annotations."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from xhs_discovery.evaluation import annotation_editor as editor
from xhs_discovery.evaluation import annotation_workflow as wf
from xhs_discovery.evaluation.annotations import EXTRACTION_LABEL_FIELDS
from xhs_discovery.storage.worker import OsGuard

FIXTURE = Path(__file__).parent / "fixtures/p2/workflow_demo"


@pytest.fixture
def packet(tmp_path):
    target = tmp_path / "packet"
    wf.prepare_packet(FIXTURE / "samples_manifest.jsonl", target, FIXTURE / "candidate_pairs.jsonl")
    return target


def save(packet, index=0, **changes):
    args = dict(producer="unit-annotator", dataset="unit-dataset", reviewer="unit-reviewer",
                state="draft", label=dict.fromkeys(EXTRACTION_LABEL_FIELDS, None))
    args.update(changes)
    editor.save_annotation(packet, "extraction", index, editor.load_sheet(packet, "extraction")[3], **args)


def test_save_preserves_original_and_imports(packet, tmp_path):
    before = (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes()
    frozen = (packet / wf.SAMPLES_NAME).read_bytes()
    labels = dict.fromkeys(EXTRACTION_LABEL_FIELDS, False)
    labels["explicit_problem"] = True
    save(packet, state="reviewed", label=labels)
    rows = editor.load_sheet(packet, "extraction")[2]
    assert rows[0].label == labels
    assert rows[1].reviewer_state == "unlabeled"
    assert (packet / wf.SAMPLES_NAME).read_bytes() == frozen
    assert next((packet / ".annotation-history").glob("*.bak")).read_bytes() == before
    report = editor.validate_packet(packet, tmp_path / "result")
    assert report["ready"] == "NOT_READY"
    assert report["counts"]["rejected_rows"] == 0
    with pytest.raises(wf.PacketExistsError):
        editor.validate_packet(packet, tmp_path / "result")


def test_stale_form_cannot_overwrite(packet):
    digest = editor.load_sheet(packet, "extraction")[3]
    save(packet)
    with pytest.raises(wf.WorkflowError, match="其他窗口"):
        editor.save_annotation(packet, "extraction", 0, digest, producer="unit", dataset="unit",
                               reviewer="", state="draft", label=dict.fromkeys(EXTRACTION_LABEL_FIELDS, True))


def test_lock_blocks_concurrent_save(packet):
    guard = OsGuard(packet / ".annotation-editor.guard")
    assert guard.try_acquire()
    try:
        with pytest.raises(wf.WorkflowError, match="另一窗口"):
            save(packet)
    finally:
        guard.release()
    save(packet)


@pytest.mark.parametrize("changes", [{"producer": " "}, {"dataset": "<标注人>"},
    {"state": "reviewed", "reviewer": " "}, {"state": "reviewed"}, {"state": "unlabeled"}])
def test_invalid_review_does_not_write(packet, changes):
    before = (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes()
    with pytest.raises(ValueError):
        save(packet, **changes)
    assert (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes() == before


def test_non_actual_sample_remains_quarantined(packet, tmp_path):
    _, samples, records, _ = editor.load_sheet(packet, "extraction")
    index = next(i for i, r in enumerate(records) if samples[r.sample_id].origin != "actual_source")
    save(packet, index, state="reviewed", label=dict.fromkeys(EXTRACTION_LABEL_FIELDS, True))
    report = editor.validate_packet(packet, tmp_path / "result")
    assert report["counts"]["quarantined_records"] == 1
    assert report["human_dataset_status"]["human_reviewed_count"] == 0


def test_pair_save_and_tampered_packet(packet):
    digest = editor.load_sheet(packet, "pair")[3]
    editor.save_annotation(packet, "pair", 0, digest, producer="unit", dataset="unit",
                           reviewer="unit-review", state="reviewed", relation="different_need")
    assert editor.load_sheet(packet, "pair")[2][0].pair_relation == "different_need"
    with (packet / wf.SAMPLES_NAME).open("a", encoding="utf-8") as out:
        out.write("\n")
    with pytest.raises(wf.WorkflowError, match="哈希"):
        save(packet)


def element(elements, label):
    return next(e for e in elements if e.label == label)


def test_real_ui_save_reload_and_validate(packet, tmp_path):
    at = AppTest.from_string("from xhs_discovery.ui.annotations import render\nrender()", default_timeout=15).run()
    element(at.text_input, "标注包目录").set_value(str(packet)).run()
    assert not at.exception
    element(at.text_input, "标注人").set_value("unit-annotator")
    element(at.text_input, "数据集标识").set_value("unit-data")
    element(at.selectbox, "显性问题").select("是")
    element(at.button, "保存本条标注").click().run()
    assert not at.exception and at.success
    assert editor.load_sheet(packet, "extraction")[2][0].label["explicit_problem"] is True
    element(at.button, "重新加载已保存内容").click().run()
    assert not at.exception
    assert element(at.selectbox, "显性问题").value == "是"
    element(at.text_input, "校验结果目录（必须为空）").set_value(str(tmp_path / "validated"))
    element(at.button, "校验并导出标注").click().run()
    assert not at.exception
    report = json.loads((tmp_path / "validated" / wf.IMPORT_REPORT_NAME).read_text("utf-8"))
    assert report["ready"] == "NOT_READY"


def test_ui_detects_external_edit(packet):
    at = AppTest.from_string("from xhs_discovery.ui.annotations import render\nrender()").run()
    element(at.text_input, "标注包目录").set_value(str(packet)).run()
    save(packet)
    element(at.text_input, "标注人").set_value("other-unit")
    element(at.text_input, "数据集标识").set_value("other-unit")
    element(at.button, "保存本条标注").click().run()
    assert not at.exception
    assert any("其他窗口" in e.value for e in at.error)
    assert editor.load_sheet(packet, "extraction")[2][0].provenance.producer_id == "unit-annotator"


def test_main_navigation_works_without_discovery_config(packet, tmp_path, monkeypatch):
    app = Path(__file__).resolve().parents[1] / "src/xhs_discovery/ui/app.py"
    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(str(app), default_timeout=15).run()
    element(at.radio, "页面").set_value("人工标注").run()
    element(at.text_input, "标注包目录").set_value(str(packet)).run()
    assert not at.exception and not at.error
    assert not (tmp_path / "data").exists()


def test_ui_pair_and_packet_switch_do_not_reuse_labels(packet, tmp_path):
    second = tmp_path / "second"
    wf.prepare_packet(FIXTURE / "samples_manifest.jsonl", second, FIXTURE / "candidate_pairs.jsonl")
    at = AppTest.from_string("from xhs_discovery.ui.annotations import render\nrender()").run()
    element(at.text_input, "标注包目录").set_value(str(packet)).run()
    element(at.radio, "标注任务").set_value("需求对比").run()
    assert any("demo-2" == r.sample_id for r in editor.load_sheet(packet, "extraction")[2])
    assert len(at.text) == 2
    element(at.text_input, "标注人").set_value("unit-pair-author")
    element(at.text_input, "数据集标识").set_value("unit-pairs")
    element(at.selectbox, "两段原文是否表达相同需求").select("不同需求")
    element(at.button, "保存本条标注").click().run()
    assert not at.exception and at.success
    element(at.text_input, "标注包目录").set_value(str(second)).run()
    assert element(at.text_input, "标注人").value == ""
    assert element(at.selectbox, "两段原文是否表达相同需求").value == "待判断"


def test_atomic_failure_keeps_worksheet(packet, monkeypatch):
    before = (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes()
    def fail(*args):
        raise OSError("unit simulated disk failure")
    monkeypatch.setattr(editor.os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        save(packet)
    assert (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes() == before
    assert not list(packet.glob(".annotation-*.tmp"))


def open_ui(packet):
    at = AppTest.from_string("from xhs_discovery.ui.annotations import render\nrender()", default_timeout=15).run()
    element(at.text_input, "标注包目录").set_value(str(packet)).run()
    return at


def fill_draft(at, author="unit-author"):
    element(at.text_input, "标注人").set_value(author)
    element(at.text_input, "数据集标识").set_value("unit-dataset")
    element(at.selectbox, "显性问题").select("是")


def test_save_next_skips_reviewed_and_keeps_labels_separate(packet):
    save(packet, 1, state="reviewed", label=dict.fromkeys(EXTRACTION_LABEL_FIELDS, True))
    at = open_ui(packet)
    fill_draft(at)
    element(at.button, "保存并转到下一条待完成记录").click().run()
    assert not at.exception and at.success
    assert element(at.selectbox, "选择记录").value == 2
    assert element(at.selectbox, "显性问题").value == "未知"
    assert element(at.text_input, "标注人").value == ""
    assert editor.load_sheet(packet, "extraction")[2][0].label["explicit_problem"] is True


def test_return_to_previous_row_uses_fresh_digest(packet):
    at = open_ui(packet)
    fill_draft(at)
    element(at.button, "保存并转到下一条待完成记录").click().run()
    fill_draft(at, "unit-author-2")
    element(at.button, "保存本条标注").click().run()
    element(at.selectbox, "选择记录").set_value(0).run()
    element(at.selectbox, "显性问题").select("否")
    element(at.button, "保存本条标注").click().run()
    assert not at.exception and not at.error
    rows = editor.load_sheet(packet, "extraction")[2]
    assert rows[0].label["explicit_problem"] is False
    assert rows[1].provenance.producer_id == "unit-author-2"


def test_filters_search_and_no_matches_do_not_write(packet):
    save(packet, 1)
    before = (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes()
    at = open_ui(packet)
    element(at.selectbox, "按标注状态筛选").select("草稿").run()
    assert element(at.selectbox, "选择记录").value == 1
    element(at.text_input, "搜索原文或样本编号").set_value("no-such-unit-record").run()
    assert not any(e.label == "选择记录" for e in at.selectbox)
    assert any("没有符合" in e.value for e in at.info)
    assert any(e.label == "校验并导出标注" for e in at.button)
    assert (packet / wf.EXTRACTION_WORKSHEET_NAME).read_bytes() == before


def test_pair_search_matches_right_text(packet):
    at = open_ui(packet)
    element(at.radio, "标注任务").set_value("需求对比").run()
    _, samples, rows, _ = editor.load_sheet(packet, "pair")
    element(at.text_input, "搜索原文或样本编号").set_value(samples[rows[1].right_sample_id].text).run()
    assert not at.exception
    assert element(at.selectbox, "选择记录").value == 1


def test_save_next_last_match_and_validation_failure(packet):
    at = open_ui(packet)
    element(at.text_input, "搜索原文或样本编号").set_value("demo-1").run()
    element(at.button, "保存并转到下一条待完成记录").click().run()
    assert at.error
    assert editor.load_sheet(packet, "extraction")[2][0].reviewer_state == "unlabeled"
    fill_draft(at)
    element(at.button, "保存并转到下一条待完成记录").click().run()
    assert not at.exception
    assert any("没有其他待完成" in e.value for e in at.success)
    assert element(at.selectbox, "选择记录").value == 0


def test_saved_row_leaves_unlabeled_filter(packet):
    at = open_ui(packet)
    element(at.selectbox, "按标注状态筛选").select("未标注").run()
    fill_draft(at)
    element(at.button, "保存并转到下一条待完成记录").click().run()
    assert not at.exception
    assert element(at.selectbox, "选择记录").value == 1
    assert len(element(at.selectbox, "选择记录").options) == 4
