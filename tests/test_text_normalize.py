"""P0-13：关键词标准化——只折叠全半角与空白，结构属性一律原样保留。

这里守住的是一条会静默毁掉归并的边界：地域/年份/年级/数字/否定/比较对象是
"问题是不是同一个"的判定材料，不是噪声。改写它们会让"上海2025中考"与"上海中考"
被当成同一个词，也会让用户输入的原文再也找不回来。
"""

from __future__ import annotations

import pytest

from xhs_discovery.text.normalize import KeywordNormalizer, fold, normalize_text


def test_normalize_only_folds_width_and_whitespace():
    assert normalize_text("上海　中考   志愿") == "上海 中考 志愿"
    assert normalize_text("  2025\t中考\n冲稳保  ") == "2025 中考 冲稳保"
    # NFKC 把全角字母数字与标点折成半角
    assert normalize_text("上中 ＡＢＣ ６４") == "上中 ABC 64"
    assert normalize_text("上中 ＡＢＣ ６４") == normalize_text("上中 ABC 64")


def test_normalization_is_idempotent_and_stable():
    for text in ("上海2026中考建档线", "上海中考 志愿怎么填不踩坑", "初三 名额分配 到校"):
        once = normalize_text(text)
        assert normalize_text(once) == once


def test_fold_is_available_for_raw_comparison():
    assert fold("ＦＵＬＬ") == "FULL"
    assert fold("上海") == "上海"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("上海中考名额分配到区2025", "上海中考名额分配到区2025"),
        ("上海2026中考建档线", "上海2026中考建档线"),
        ("上海中考志愿怎么填不踩坑", "上海中考志愿怎么填不踩坑"),
        ("浦东新区初三模拟考", "浦东新区初三模拟考"),
    ],
)
def test_structural_words_survive_normalization(text: str, expected: str):
    """地域、年份、年级、否定词都在规范化文本里逐字保留——它们必须参与后续兼容判定。"""
    assert normalize_text(text) == expected


def test_blank_keyword_is_rejected_not_normalized_to_empty():
    normalizer = KeywordNormalizer()
    with pytest.raises(ValueError, match="关键词不得为空"):
        normalizer.normalize("   ")
    with pytest.raises(ValueError, match="关键词不得为空"):
        normalizer.normalize("")


def test_structural_attributes_extracted_without_rewriting_text():
    normalizer = KeywordNormalizer(locations=("上海", "浦东", "杨浦"))
    result = normalizer.normalize("上海浦东 2026 初三 名额分配 405 分 不要自招")
    assert result.normalized == "上海浦东 2026 初三 名额分配 405 分 不要自招"
    assert result.raw == result.normalized
    assert result.locations == ("上海", "浦东")
    assert result.years == ("2026",)
    assert result.grades == ("初三",)
    assert result.digits == ("2026", "405")
    assert result.has_negation is True


def test_structural_dict_reports_missing_as_missing_not_wildcard():
    normalizer = KeywordNormalizer(locations=("上海",))
    result = normalizer.normalize("上海中考建档线")
    assert result.structural == {
        "locations": ["上海"],
        "years": [],
        "grades": [],
        "negation": False,
        "digits": [],
    }


def test_years_and_grades_patterns_cover_two_digit_and_chinese_forms():
    normalizer = KeywordNormalizer()
    assert normalizer.normalize("25年中考").years == ("25年",)
    assert normalizer.normalize("2025年中考").years == ("2025",)
    assert normalizer.normalize("九年级 高三 初二").grades == ("九年级", "初二", "高三")
    # 普通数字不是年份：四以下位数的数字留在 digits 里
    plain = normalizer.normalize("405 分")
    assert plain.years == () and plain.digits == ("405",)


def test_negation_terms_are_detected_but_text_untouched():
    normalizer = KeywordNormalizer()
    for text in ("上海中考不要自招", "无标题的建档线", "没能考上普高怎么办", "别报中职"):
        result = normalizer.normalize(text)
        assert result.has_negation is True, text
        assert result.normalized == text


def test_ambiguous_seed_is_flagged_not_rewritten():
    normalizer = KeywordNormalizer(ambiguous_seeds=("上海", "复旦"))
    result = normalizer.normalize("上海")
    assert result.is_ambiguous_seed is True
    assert result.raw == "上海" and result.original == "上海"
    child = normalizer.normalize("上海中考")
    assert child.is_ambiguous_seed is False
    assert child.original == "上海中考"


def test_ambiguous_seed_declaration_is_itself_normalized():
    """词表写全角或带多余空格也必须能命中：否则"只标记"这条规则会因写法差异失效。"""
    normalizer = KeywordNormalizer(ambiguous_seeds=(" ＦＵＬＬ ",))
    assert normalizer.is_ambiguous_seed("FULL") is True
    assert normalizer.normalize("FULL").is_ambiguous_seed is True


def test_location_vocabulary_matches_substrings_longest_first():
    normalizer = KeywordNormalizer(locations=("浦东", "上海浦东新区"))
    result = normalizer.normalize("上海浦东新区中考")
    assert set(result.locations) == {"浦东", "上海浦东新区"}


def test_as_keyword_text_keeps_raw_and_normalized_pair():
    normalizer = KeywordNormalizer()
    result = normalizer.normalize(" 上海　２０２５中考  ")
    assert result.original == " 上海　２０２５中考  "
    # raw 只剥首尾空白，内部写法必须原样保留（证据要能指回用户/平台当时看到的字）
    assert result.raw == "上海　２０２５中考"
    assert result.normalized == "上海 2025中考"
    keyword = result.as_keyword_text()
    assert (keyword.raw, keyword.normalized) == ("上海　２０２５中考", "上海 2025中考")
