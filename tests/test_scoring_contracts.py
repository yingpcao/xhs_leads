"""P0-04：评分特征分解。缺失不等于零，未采样用中性先验并标出来。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xhs_discovery.models.scoring import FeatureValue, WeightedScore, feature, missing_feature, prior_feature


def demand_features(**overrides) -> dict[str, FeatureValue]:
    base = {
        "S": feature(3.2, 0.7, 0.35),
        "Q": feature(5.0, 0.6, 0.30),
        "A": feature(4.0, 0.5, 0.20),
        "F": feature(1.0, 0.4, 0.15),
    }
    base.update(overrides)
    return base


def test_full_evidence_score_is_the_weighted_sum():
    score = WeightedScore.compute(demand_features(), model_version="p0-rule-baseline.1")
    assert score.score == pytest.approx(0.7 * 0.35 + 0.6 * 0.30 + 0.5 * 0.20 + 0.4 * 0.15)
    assert score.effective_weight_total == pytest.approx(1.0)
    assert score.all_missing is False


def test_missing_component_renormalizes_instead_of_counting_zero():
    partial = WeightedScore.compute(
        demand_features(F=missing_feature(0.15, "发布时间原文无法解析")),
        model_version="p0-rule-baseline.1",
    )
    full = WeightedScore.compute(demand_features(), model_version="v")
    expected = (0.7 * 0.35 + 0.6 * 0.30 + 0.5 * 0.20) / 0.85
    assert partial.score == pytest.approx(expected)
    assert partial.score != full.score
    assert partial.effective_weight_total == pytest.approx(0.85)
    assert partial.features["F"].available is False
    assert partial.features["F"].normalized is None


def test_unknown_author_source_keeps_evidence_completeness_visible():
    score = WeightedScore.compute(
        demand_features(A=missing_feature(0.20, "作者引用缺失，不能把每篇当不同作者")),
        model_version="v",
    )
    assert score.effective_weight_total == pytest.approx(0.80)
    assert "作者引用缺失" in score.features["A"].missing_reason


def test_prior_feature_is_labeled_as_prior_not_as_observed_evidence():
    score = WeightedScore.compute(
        demand_features(Q=prior_feature(None, 0.5, 0.30, "该词尚未内容采样")),
        model_version="v",
    )
    q = score.features["Q"]
    assert q.is_prior is True and q.prior_value == 0.5 and q.normalized == 0.5
    assert q.missing_reason == "该词尚未内容采样"


def test_unsampled_feature_cannot_claim_real_evidence():
    with pytest.raises(ValidationError, match="必须记录 prior_value"):
        FeatureValue(normalized=0.5, baseline_weight=0.3, is_prior=True)
    with pytest.raises(ValidationError, match="prior_value 只在"):
        FeatureValue(normalized=0.5, baseline_weight=0.3, prior_value=0.5)


def test_available_feature_requires_a_normalized_value():
    with pytest.raises(ValidationError, match="必须有归一化值"):
        FeatureValue(raw=3.0, baseline_weight=0.35, available=True)
    with pytest.raises(ValidationError, match="不得有归一化值"):
        FeatureValue(baseline_weight=0.35, available=False, normalized=0.2, missing_reason="x")
    with pytest.raises(ValidationError, match="必须写明 missing_reason"):
        FeatureValue(baseline_weight=0.35, available=False)


def test_all_missing_yields_no_score_rather_than_zero():
    score = WeightedScore.compute(
        {k: missing_feature(w, "无可用观测") for k, w in (("S", 0.35), ("Q", 0.30), ("A", 0.20), ("F", 0.15))},
        model_version="v",
    )
    assert score.score is None
    assert score.all_missing is True
    assert score.provisional is True
    assert score.effective_weight_total == 0.0


def test_priority_features_keep_raw_and_normalized_values():
    priority = WeightedScore.compute(
        {
            "R": feature(0.42, 0.62, 0.25),
            "N": feature(0.81, 0.81, 0.20),
            "I": feature(2.0, 0.5, 0.20),
            "E": prior_feature(None, 0.5, 0.15, "未采样"),
            "C": feature(1.0, 0.2, 0.10),
            "U": feature(1.3, 0.9, 0.10),
        },
        model_version="p0-rule-baseline.1",
    )
    assert set(priority.features) == {"R", "N", "I", "E", "C", "U"}
    assert priority.features["R"].raw == 0.42
    assert priority.score == pytest.approx(0.62 * 0.25 + 0.81 * 0.20 + 0.5 * 0.20 + 0.5 * 0.15 + 0.2 * 0.10 + 0.9 * 0.10)


def test_provisional_flag_requires_explanation():
    with pytest.raises(ValidationError, match="必须说明原因"):
        WeightedScore(
            features=demand_features(),
            score=0.5,
            effective_weight_total=1.0,
            provisional=True,
            model_version="v",
        )
    ok = WeightedScore(
        features=demand_features(),
        score=0.5,
        effective_weight_total=1.0,
        provisional=True,
        provisional_reason="样本量 7 < quantile_min_sample",
        model_version="v",
    )
    assert ok.provisional is True


def test_normalized_values_are_bounded():
    with pytest.raises(ValidationError):
        feature(1.0, 1.4, 0.3)
    with pytest.raises(ValidationError):
        feature(1.0, -0.1, 0.3)
    with pytest.raises(ValidationError):
        feature(1.0, 0.5, 0.0)
