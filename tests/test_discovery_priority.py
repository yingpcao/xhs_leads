"""P0-16：R/N/I/E/C/U 可解释评分。

这里最容易被"顺手简化"破坏的三点：
- 未采样的 E 是中性先验并且必须标注，不能冒充实际证据；
- generated 没有平台共识时 C 是"不可用"，走权重重归一化，而不是记 0 分；
- 分数必须能由 `features` 分解原样重算出来，否则"为什么选这个词"无法复核。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from xhs_discovery.config import PriorityWeights, load_intent_rules
from xhs_discovery.discovery.priority import (
    EVIDENCE_FULL_COUNT,
    PARENT_CONSENSUS_CAP,
    PriorityBatch,
    PriorityInput,
    build_features,
    evaluate_batch,
    with_branch_ucb,
)
from xhs_discovery.models.enums import CandidateOrigin
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.similarity import CharTfidf

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = PriorityWeights()
MATCHER = IntentMatcher(load_intent_rules(ROOT / "config" / "intent_rules.yaml"))


def make_batch(anchors=("上海中考",), explored=(), **overrides) -> PriorityBatch:
    payload = dict(
        anchors=tuple(anchors),
        explored=tuple(explored),
        weights=WEIGHTS,
        matcher=MATCHER,
        unknown_evidence_prior=0.5,
    )
    payload.update(overrides)
    return PriorityBatch(**payload)


def entry(text="上海中考志愿填报", **overrides) -> PriorityInput:
    payload = dict(text=text, origin=CandidateOrigin.observed)
    payload.update(overrides)
    return PriorityInput(**payload)


def score_of(batch: PriorityBatch, candidate: PriorityInput):
    return evaluate_batch(batch, (candidate,))[0][1]


def recompute(features) -> float | None:
    """从分解重算加权分：可用分量按原权重归一，缺失分量不参与。"""
    available = [f for f in features.values() if f.available and f.normalized is not None]
    if not available:
        return None
    total = sum(f.baseline_weight for f in features.values())
    effective = sum(f.baseline_weight for f in available) / total
    weighted = sum(f.baseline_weight * float(f.normalized or 0.0) for f in available)
    return round(min(max(weighted / (effective * total), 0.0), 1.0), 6)


def test_all_six_features_are_present_and_named():
    features = build_features(make_batch(explored=("上海中考志愿填报",)), CharTfidf(["上海中考", "上海中考志愿填报"]), entry())
    assert set(features) == {"R", "N", "I", "E", "C", "U"}
    assert math.isclose(sum(f.baseline_weight for f in features.values()), 1.0, rel_tol=1e-9)


def test_unsampled_candidate_uses_flagged_neutral_prior_for_evidence():
    features = build_features(make_batch(), CharTfidf(["上海中考"]), entry(sampled=False))
    evidence = features["E"]
    assert evidence.available is True
    assert evidence.is_prior is True
    assert evidence.prior_value == 0.5
    assert evidence.raw is None
    assert evidence.normalized == 0.5
    assert "中性先验" in evidence.missing_reason


def test_sampled_evidence_uses_actual_quote_count_and_saturates():
    batch = make_batch()
    vectorizer = CharTfidf(["上海中考"])
    low = build_features(batch, vectorizer, entry(sampled=True, explicit_evidence_count=1))["E"]
    full = build_features(batch, vectorizer, entry(sampled=True, explicit_evidence_count=EVIDENCE_FULL_COUNT))["E"]
    over = build_features(batch, vectorizer, entry(sampled=True, explicit_evidence_count=9))["E"]
    assert (low.raw, low.normalized) == (1.0, round(1 / EVIDENCE_FULL_COUNT, 6))
    assert (full.raw, full.normalized) == (3.0, 1.0)
    assert over.normalized == 1.0 and over.raw == 9.0
    for feature_value in (low, full, over):
        assert feature_value.is_prior is False and feature_value.missing_reason is None


def test_generated_candidate_marked_missing_consensus_not_zero():
    features = build_features(make_batch(), CharTfidf(["上海中考"]), entry(origin=CandidateOrigin.generated))
    consensus = features["C"]
    assert consensus.available is False
    assert consensus.normalized is None
    assert consensus.raw is None
    assert "generated" in consensus.missing_reason


def test_observed_but_unreturned_candidate_explains_a_different_reason():
    features = build_features(
        make_batch(), CharTfidf(["上海中考"]), entry(origin=CandidateOrigin.observed, observed_parents=0)
    )
    assert "尚未被平台联想返回" in features["C"].missing_reason


def test_parent_consensus_is_log_scaled_and_capped():
    batch, vectorizer = make_batch(), CharTfidf(["上海中考"])
    values = [
        build_features(batch, vectorizer, entry(observed_parents=count))["C"].normalized
        for count in (1, 2, 3, 4, 8)
    ]
    assert values == sorted(values)
    assert values[-1] == 1.0
    assert values.index(1.0) == PARENT_CONSENSUS_CAP - 1
    # 同一会话重复返回不增加共识：只有独立父路径才计数
    assert build_features(batch, vectorizer, entry(observed_parents=1))["C"].raw == 1.0


def test_score_is_recomputable_from_the_persisted_breakdown():
    batch = make_batch(explored=("上海中考自招", "上海中考分数线"))
    for candidate in (
        entry("上海中考志愿填报", observed_parents=2, best_suggestion_rank=1, branch_ucb=0.4),
        entry("上海中考建档线 怎么选", origin=CandidateOrigin.generated, branch_ucb=None),
        entry("上海中考中职", sampled=True, explicit_evidence_count=2, observed_parents=3),
    ):
        result = score_of(batch, candidate)
        assert result.score == recompute(result.features)
        payload = result.model_dump()
        assert set(payload["features"]) == {"R", "N", "I", "E", "C", "U"}
        # 分解里每个分量都要能同时看到原值与归一值，否则复核只能相信结论
        for name, item in payload["features"].items():
            assert item["baseline_weight"] > 0, name
            assert bool(item["available"]) == (item["normalized"] is not None), name


def test_missing_components_renormalize_instead_of_diluting_the_score():
    """把不可用分量当 0 会把"还没观察"偷换成"观察过但不被认可"。"""
    batch = make_batch(explored=("上海中考志愿填报",))
    plain = entry("上海中考志愿填报", observed_parents=2, best_suggestion_rank=1, branch_ucb=0.5)
    generated = entry("上海中考志愿填报", origin=CandidateOrigin.generated, branch_ucb=0.5)

    observed_score = score_of(batch, plain)
    generated_score = score_of(batch, generated)
    assert observed_score.effective_weight_total == 1.0
    assert generated_score.effective_weight_total == pytest.approx(0.9, abs=1e-6)
    # C 缺失后其余分量按原权重放大补齐，而不是被 0 拉低
    weighted_available = sum(
        f.baseline_weight * f.normalized for f in generated_score.features.values() if f.available
    )
    assert generated_score.score == round(weighted_available / 0.9, 6)
    assert generated_score.score > weighted_available


def test_novelty_is_undefined_without_explored_words_not_full_marks():
    batch = make_batch()
    features = build_features(batch, CharTfidf(["上海中考"]), entry())
    assert features["N"].available is False
    assert "尚无已探索词" in features["N"].missing_reason

    explored = make_batch(explored=("上海中考志愿填报",))
    same = build_features(explored, CharTfidf(["上海中考", "上海中考志愿填报"]), entry("上海中考志愿填报"))
    other = build_features(explored, CharTfidf(["上海中考", "上海中考志愿填报"]), entry("上海中考建档线"))
    assert same["N"].normalized <= other["N"].normalized
    assert other["N"].available is True


def test_relevance_prefers_text_close_to_the_anchor():
    batch = make_batch(anchors=("上海中考",), explored=("上海中考自招",))
    near = build_features(batch, CharTfidf(list(batch.corpus(("上海中考志愿填报",)))), entry("上海中考志愿填报"))
    far = build_features(batch, CharTfidf(list(batch.corpus(("手机维修店推荐",)))), entry("手机维修店推荐"))
    assert near["R"].normalized > far["R"].normalized
    assert near["R"].available and far["R"].available


def test_relevance_is_missing_when_the_batch_has_no_anchor():
    batch = make_batch(anchors=(), explored=("上海中考",))
    features = build_features(batch, CharTfidf(["上海中考"]), entry())
    assert features["R"].available is False
    assert "没有可比锚词" in features["R"].missing_reason


def test_intent_feature_hits_rules_and_zeroes_without_match():
    batch = make_batch()
    vectorizer = CharTfidf(["上海中考"])
    demand = build_features(batch, vectorizer, entry("上海中考建档线不够怎么办"))["I"]
    neutral = build_features(batch, vectorizer, entry("上海"))["I"]
    assert demand.normalized > 0 and demand.raw == demand.normalized
    assert neutral.normalized == 0.0 and neutral.available is True


def test_branch_ucb_component_records_note_when_unavailable():
    batch = make_batch()
    vectorizer = CharTfidf(["上海中考"])
    missing = build_features(batch, vectorizer, entry(branch_ucb=None))["U"]
    assert missing.available is False and "分支统计不可用" in missing.missing_reason
    with_note = build_features(
        batch, vectorizer, with_branch_ucb(entry(), None, note="分支 b-x 无任何试验记录")
    )["U"]
    assert with_note.missing_reason == "分支 b-x 无任何试验记录"
    clamped = build_features(batch, vectorizer, entry(branch_ucb=2.5))["U"]
    assert clamped.normalized == 1.0 and clamped.raw == 2.5


def test_with_branch_ucb_returns_a_new_entry():
    original = entry()
    updated = with_branch_ucb(original, 0.75, note="x")
    assert original.branch_ucb is None
    assert (updated.branch_ucb, updated.branch_ucb_note) == (0.75, "x")
    assert updated.text == original.text


def test_platform_signal_follows_suggestion_rank():
    assert entry(best_suggestion_rank=None).platform_signal == 0.0
    assert entry(best_suggestion_rank=1).platform_signal == 1.0
    assert entry(best_suggestion_rank=4).platform_signal == 0.25


def test_single_candidate_batch_is_marked_provisional():
    batch = make_batch(explored=("上海中考",))
    alone = evaluate_batch(batch, (entry("上海中考志愿填报"),))
    assert alone[0][1].provisional is True
    assert "不足 2 个" in alone[0][1].provisional_reason
    pair = evaluate_batch(batch, (entry("上海中考志愿填报"), entry("上海中考建档线")))
    assert all(score.provisional is False for _, score in pair)


def test_only_truly_unknown_components_are_missing_prior_and_hit_are_real_values():
    """E 的先验与 I 的"确实没命中"都算可用分量，所以永远不会退化成"全缺"。"""
    batch = PriorityBatch(anchors=(), explored=(), weights=WEIGHTS, matcher=MATCHER)
    result = score_of(batch, entry(origin=CandidateOrigin.generated, branch_ucb=None))
    features = result.features
    assert [name for name, f in features.items() if not f.available] == ["R", "N", "C", "U"]
    assert features["E"].is_prior is True and features["I"].normalized == 0.0
    assert result.score is not None
    assert result.effective_weight_total == pytest.approx(0.35, abs=1e-6)


def test_model_version_binds_the_batch_corpus():
    batch = make_batch(anchors=("上海中考",), explored=("上海中考自招",))
    first = score_of(batch, entry("上海中考志愿填报"))
    same = score_of(batch, entry("上海中考志愿填报"))
    other_batch = make_batch(anchors=("上海中考",), explored=("上海中考自招", "上海中考建档线"))
    different = score_of(other_batch, entry("上海中考志愿填报"))
    assert first.model_version == same.model_version
    assert first.model_version != different.model_version
    assert first.model_version.startswith("priority-heuristic.")


def test_scoring_is_deterministic_for_identical_inputs():
    batch = make_batch(explored=("上海中考自招", "上海中考分数线"))
    candidates = (
        entry("上海中考志愿填报", observed_parents=2, best_suggestion_rank=1),
        entry("上海中考名额分配", origin=CandidateOrigin.generated),
    )
    first = [(e.text, s.score, s.model_version) for e, s in evaluate_batch(batch, candidates)]
    second = [(e.text, s.score, s.model_version) for e, s in evaluate_batch(batch, candidates)]
    assert first == second


def test_weights_from_config_are_used_verbatim():
    batch = make_batch(weights=PriorityWeights(r=0.4, n=0.2, i=0.2, e=0.1, c=0.05, u=0.05))
    features = build_features(batch, CharTfidf(["上海中考"]), entry())
    assert features["R"].baseline_weight == 0.4
    assert features["U"].baseline_weight == 0.05
    with pytest.raises(ValueError, match="之和必须为 1.0"):
        PriorityWeights(r=0.5, n=0.5, i=0.2, e=0.1, c=0.1, u=0.1)
