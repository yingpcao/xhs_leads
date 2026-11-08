"""特征与加权评分契约。

关键规则：缺失分量不直接当零。WeightedScore 只对可用分量按原始权重重新归一化，
同时输出有效权重与证据完整度；小样本分位数不稳定时标 provisional。
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, StrictFloat, model_validator

from xhs_discovery.models.common import StrictModel

MissingReason = str


class FeatureValue(StrictModel):
    """单特征：同时保留原值与归一化值，供分解与复核。"""

    raw: StrictFloat | None = None
    normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_weight: float = Field(gt=0.0, le=1.0)
    available: bool = True
    missing_reason: MissingReason | None = None
    is_prior: bool = False
    prior_value: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> "FeatureValue":
        if self.available:
            if self.normalized is None:
                raise ValueError("available 特征必须有归一化值")
            if self.missing_reason is not None and not self.is_prior:
                raise ValueError("只有标注为中性先验的特征才能同时携带 missing_reason")
        else:
            if self.normalized is not None:
                raise ValueError("不可用特征不得有归一化值，必须走重归一化")
            if self.missing_reason is None:
                raise ValueError("不可用特征必须写明 missing_reason")
        if self.is_prior and self.prior_value is None:
            raise ValueError("使用中性先验的特征必须记录 prior_value，不能冒充实际证据")
        if self.prior_value is not None and not self.is_prior:
            raise ValueError("prior_value 只在 is_prior=True 时有意义")
        return self


class WeightedScore(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    features: dict[str, FeatureValue]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    effective_weight_total: float = Field(default=0.0, ge=0.0, le=1.0)
    all_missing: bool = False
    provisional: bool = False
    provisional_reason: str | None = None
    model_version: str = Field(min_length=1)

    @classmethod
    def compute(
        cls,
        features: dict[str, FeatureValue],
        *,
        model_version: str,
        provisional: bool = False,
        provisional_reason: str | None = None,
    ) -> "WeightedScore":
        total = sum(f.baseline_weight for f in features.values())
        available = {k: v for k, v in features.items() if v.available and v.normalized is not None}
        if not available:
            return cls(
                features=features,
                score=None,
                effective_weight_total=0.0,
                all_missing=True,
                provisional=True,
                provisional_reason=provisional_reason or "所有分量均缺失，无可计算得分",
                model_version=model_version,
            )
        effective = sum(f.baseline_weight for f in available.values()) / total
        weighted = sum(f.baseline_weight * float(f.normalized or 0.0) for f in available.values())
        score = weighted / (effective * total)
        return cls(
            features=features,
            score=round(min(max(score, 0.0), 1.0), 6),
            effective_weight_total=round(effective, 6),
            provisional=provisional,
            provisional_reason=provisional_reason,
            model_version=model_version,
        )

    @model_validator(mode="after")
    def _check(self) -> "WeightedScore":
        if self.all_missing and self.score is not None:
            raise ValueError("all_missing 时不得有 score")
        if not self.all_missing and self.score is None:
            raise ValueError("非 all_missing 必须给出 score")
        if self.provisional and not self.provisional_reason:
            raise ValueError("provisional 分数必须说明原因（如小样本分位数不稳定）")
        return self


def feature(raw: float | None, normalized: float | None, weight: float) -> FeatureValue:
    return FeatureValue(raw=raw, normalized=normalized, baseline_weight=weight, available=True)


def missing_feature(weight: float, reason: str) -> FeatureValue:
    return FeatureValue(
        baseline_weight=weight, available=False, missing_reason=reason
    )


def prior_feature(raw: float | None, prior: float, weight: float, reason: str) -> FeatureValue:
    """未采样等未知证据用中性先验，并明确标记为先验而非实际证据。"""
    return FeatureValue(
        raw=raw,
        normalized=prior,
        baseline_weight=weight,
        available=True,
        is_prior=True,
        prior_value=prior,
        missing_reason=reason,
    )


__all__ = ["FeatureValue", "WeightedScore", "feature", "missing_feature", "prior_feature"]
