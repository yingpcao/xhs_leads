"""Human annotation UI, independent of the discovery database and worker."""
from pathlib import Path
import hashlib

import streamlit as st

from xhs_discovery.evaluation import annotation_editor as editor
from xhs_discovery.evaluation import annotation_workflow as wf
from xhs_discovery.evaluation.annotations import EXTRACTION_LABEL_FIELDS

LABELS = dict(zip(EXTRACTION_LABEL_FIELDS, ("显性问题", "隐含问题", "广告营销", "包含否定", "包含地域", "包含年份")))
STATES = {"草稿": "draft", "已复核": "reviewed", "已裁决": "adjudicated"}
VALUES = {"未知": None, "是": True, "否": False}
RELATIONS = {"待判断": "unknown", "相同需求": "same_need", "不同需求": "different_need"}
FILTERS = {"全部": None, "未标注": {"unlabeled"}, "草稿": {"draft"},
           "待完成（未标注和草稿）": {"unlabeled", "draft"}, "已复核或裁决": {"reviewed", "adjudicated"}}


def matching_indices(records, samples, status: str, query: str) -> list[int]:
    query = query.strip().casefold()
    result = []
    for index, record in enumerate(records):
        if FILTERS[status] is not None and record.reviewer_state not in FILTERS[status]:
            continue
        text = record.sample_id + " " + record.text
        if hasattr(record, "right_sample_id"):
            text += " " + record.right_sample_id + " " + samples[record.right_sample_id].text
        if query in text.casefold():
            result.append(index)
    return result


def _clear_form(key: str) -> None:
    for state_key in list(st.session_state):
        if state_key.endswith(f"_{key}"):
            del st.session_state[state_key]


def render() -> None:
    st.subheader("人工标注")
    st.caption("逐条阅读原文后填写标签。保存草稿不会自动复核；样本来源保持原样，合成样本不计入真实人工评估集。")
    with st.expander("从本地样本清单创建标注包"):
        with st.form("annotation_prepare"):
            source = st.text_input("样本清单 JSONL")
            pairs = st.text_input("候选对清单 JSONL（可选）")
            destination = st.text_input("新标注包目录（必须为空）")
            prepare = st.form_submit_button("创建标注包")
        if prepare:
            try:
                if not source.strip() or not destination.strip():
                    raise ValueError("请填写样本清单和输出目录")
                wf.prepare_packet(Path(source.strip()), Path(destination.strip()), Path(pairs.strip()) if pairs.strip() else None)
                st.success(f"已创建：{destination}")
            except (ValueError, OSError) as exc:
                st.error(str(exc))
    packet_text = st.text_input("标注包目录", key="annotation_packet")
    if not packet_text.strip():
        st.info("填写已有标注包目录，或先从本地清单创建。")
        return
    packet = Path(packet_text.strip()).resolve()
    kind = st.radio("标注任务", ["抽取标签", "需求对比"], horizontal=True)
    kind = "extraction" if kind == "抽取标签" else "pair"
    try:
        manifest, samples, records, digest = editor.load_sheet(packet, kind)
    except (ValueError, OSError) as exc:
        st.error(f"无法加载标注包：{exc}")
        return
    token = hashlib.sha256(f"{packet}:{manifest.packet_fingerprint}:{kind}".encode()).hexdigest()[:16]
    flash = st.session_state.pop(f"notice_{token}", None)
    if flash:
        st.success(flash)
    st.caption(f"共 {len(records)} 条；已复核/裁决 {sum(r.reviewer_state in ('reviewed', 'adjudicated') for r in records)} 条。来源分布：{manifest.origin_counts}")
    status = st.selectbox("按标注状态筛选", list(FILTERS), key=f"filter_{token}")
    query = st.text_input("搜索原文或样本编号", key=f"search_{token}")
    visible = matching_indices(records, samples, status, query)
    st.caption(f"当前显示 {len(visible)} 条。切换记录、筛选或搜索前请先保存填写内容。")
    selection_key = f"record_{token}"
    pending = st.session_state.pop(f"next_{token}", None)
    if pending in visible:
        st.session_state[selection_key] = pending
    if visible and st.session_state.get(selection_key) not in visible:
        st.session_state[selection_key] = visible[0]
    if visible:
        index = st.selectbox("选择记录", visible, format_func=lambda i: f"{i + 1}. {records[i].sample_id}" + (f" ↔ {records[i].right_sample_id}" if kind == "pair" else ""), key=selection_key)
        record = records[index]
        st.text(record.text)
        st.caption(f"来源：{samples[record.sample_id].origin} ｜ 当前状态：{record.reviewer_state}")
        if kind == "pair":
            other = samples[record.right_sample_id]
            st.text(other.text)
            st.caption(f"对端来源：{other.origin}")
        key = f"{token}_{index}"
        # Returning to a row is a fresh read, not an old form with a stale digest.
        if st.session_state.get("annotation_active_form") != key or st.session_state.pop(f"reset_{key}", False):
            _clear_form(key)
        st.session_state["annotation_active_form"] = key
        # Keep the digest of the displayed form until explicit save/reload, not the rerun's latest file.
        version_key = f"version_{key}"
        st.session_state.setdefault(version_key, digest)
        if st.button("重新加载已保存内容", key=f"reload_{key}"):
            _clear_form(key)
            st.rerun()
        with st.form(f"edit_{key}"):
            producer = st.text_input("标注人", value=record.provenance.producer_id if record.provenance.producer_id not in wf.PLACEHOLDER_SET else "", key=f"producer_{key}")
            dataset = st.text_input("数据集标识", value=record.provenance.dataset_id if record.provenance.dataset_id not in wf.PLACEHOLDER_SET else "", key=f"dataset_{key}")
            reviewer = st.text_input("复核人（复核/裁决时必填）", value=record.reviewer_id or "", key=f"reviewer_{key}")
            # Every editing action defaults to draft, never silently carries a previous review forward.
            state = st.selectbox("保存状态", list(STATES), key=f"state_{key}")
            label = None
            relation = "unknown"
            if kind == "extraction":
                label = {}
                for field, title in LABELS.items():
                    current = (record.label or {}).get(field)
                    choice = st.selectbox(title, list(VALUES), index=list(VALUES.values()).index(current), key=f"{field}_{key}")
                    label[field] = VALUES[choice]
            else:
                choice = st.selectbox("两段原文是否表达相同需求", list(RELATIONS), index=list(RELATIONS.values()).index(record.pair_relation), key=f"relation_{key}")
                relation = RELATIONS[choice]
            save = st.form_submit_button("保存本条标注")
            save_next = st.form_submit_button("保存并转到下一条待完成记录")
        if save or save_next:
            try:
                saved_digest = editor.save_annotation(packet, kind, index, st.session_state[version_key], producer=producer, dataset=dataset, reviewer=reviewer, state=STATES[state], label=label, relation=relation)
                st.session_state[version_key] = saved_digest
                st.session_state[f"reset_{key}"] = True
                notice = "已保存；原版本已备份。"
                if save_next:
                    updated = list(records)
                    updated[index] = record.model_copy(update={"reviewer_state": STATES[state]})
                    candidates = [i for i in matching_indices(updated, samples, status, query)
                                  if i != index and updated[i].reviewer_state in ("unlabeled", "draft")]
                    following = [i for i in candidates if i > index] or candidates
                    if following:
                        st.session_state[f"next_{token}"] = following[0]
                    else:
                        notice += " 当前筛选下没有其他待完成记录。"
                st.session_state[f"notice_{token}"] = notice
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(f"未保存：{exc}")
    else:
        st.session_state.pop("annotation_active_form", None)
        st.info("没有符合当前筛选条件的记录。可调整筛选或搜索，也可在下方校验整份标注包。")
    with st.form(f"validate_{token}"):
        output = st.text_input("校验结果目录（必须为空）", key=f"output_{token}")
        validate = st.form_submit_button("校验并导出标注")
    if validate:
        try:
            if not output.strip():
                raise ValueError("请填写校验结果目录")
            report = editor.validate_packet(packet, Path(output.strip()))
            st.info(f"数据准备状态：{report['ready']}；此结果不等于模型质量验收通过。")
            st.json(report)
        except (ValueError, OSError) as exc:
            st.error(str(exc))
