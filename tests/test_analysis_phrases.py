"""P0-22：领域分词、字符 n-gram、TF-IDF、PMI 与低频降级。

验收口径（PROJECT_CHECKLIST P0-22）："可运行"不等于"跑出一份词表"：
每条被降级的候选都要说明**为什么**降级，批次统计要带着自己的边界（语料条数、向量器版本），
换 `analysis_version` 重跑要写新行而不是覆盖旧结论。全程离线、纯 Python、无模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xhs_discovery.analysis.lexicon import DomainLexicon, load_lexicon
from xhs_discovery.analysis.phrase_mining import (
    SCORE_BASIS_DOC_RATIO,
    SCORE_BASIS_NGRAM_IDF,
    SCORE_BASIS_PMI,
    SKIP_BELOW_FREQ,
    SKIP_CORPUS_SMALL,
    SKIP_IN_DOMAIN_TERM,
    SKIP_PMI_BELOW,
    PhraseMiner,
)
from xhs_discovery.analysis.store import record_phrase_run
from xhs_discovery.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
AT = datetime(2026, 10, 26, 15, 0, 0, tzinfo=timezone.utc)
VERSION = "p0-rule-baseline.2"

LEXICON_TERMS = ("上海中考", "中考", "志愿填报", "志愿", "分数线", "名额分配")

# 前三条共享"上海中考志愿填报怎么"，第四条只贡献"志愿"与"怎么"：
# 于是 `考志`（df=3）比 `怎么`（df=4）更稀奇，`上海中考`（df=3）与 `志愿`（df=4）频次可比。
CORPUS = [
    "上海中考志愿填报怎么改",
    "上海中考志愿填报怎么查",
    "上海中考志愿填报怎么办",
    "志愿到底怎么排",
]


@pytest.fixture
def lexicon() -> DomainLexicon:
    return DomainLexicon(LEXICON_TERMS)


def miner(lexicon: DomainLexicon, **overrides) -> PhraseMiner:
    payload = dict(char_ngram_min=2, char_ngram_max=3, min_phrase_freq=3, min_pmi=1.0)
    payload.update(overrides)
    return PhraseMiner(lexicon, **payload)


def phrase_texts(result) -> set[str]:
    return {phrase.text for phrase in result.phrases}


def skipped(result, reason: str) -> list[str]:
    return [item.text for item in result.skipped if item.reason == reason]


# ---------------------------------------------------------------------- 分词


def test_longest_match_wins_and_nothing_is_rewritten(lexicon: DomainLexicon):
    segment = lexicon.segment("上海中考志愿填报")
    assert [token.text for token in segment.tokens] == ["上海中考", "志愿填报"]
    assert segment.terms == ("上海中考", "志愿填报")
    assert segment.domain_coverage == 1.0
    # 位置指向原文：分词只用于统计，关键词身份仍然由 normalize 负责
    assert (segment.tokens[0].start, segment.tokens[0].end) == (0, 4)


def test_out_of_dictionary_text_stays_as_characters(lexicon: DomainLexicon):
    segment = lexicon.segment("中考到底怎么排")
    assert [token.text for token in segment.tokens] == ["中考", "到", "底", "怎", "么", "排"]
    assert segment.domain_coverage == pytest.approx(2 / 7)


def test_digits_latin_and_full_width_are_kept_not_cleaned(lexicon: DomainLexicon):
    segment = lexicon.segment("２０２５上海中考 don't 放弃")
    kinds = [(token.text, token.kind) for token in segment.tokens]
    assert ("2025", "digit") in kinds, "年份成串但仍留在原文里，不作为噪声删除"
    assert ("上海中考", "domain") in kinds
    assert ("don", "latin") in kinds
    assert segment.text.startswith("2025上海中考"), "NFKC 只折叠全半角，不删词"


def test_whitespace_is_the_only_thing_removed(lexicon: DomainLexicon):
    assert lexicon.segment("中考 志愿").characters == "中考志愿"


def test_empty_lexicon_is_refused():
    with pytest.raises(ValueError, match="领域词典为空"):
        DomainLexicon(["  ", "　"])


def test_load_lexicon_reads_the_configured_dictionary_file():
    settings = load_settings(ROOT / "config" / "default.yaml")
    lexicon = load_lexicon(settings)
    assert lexicon.is_term("上海中考"), "配置声明的词典必须真的进到分词器里"
    assert not lexicon.is_term("不存在的词")


# ---------------------------------------------------------------------- n-gram / 文档频次


def test_document_frequency_counts_documents_not_repetitions(lexicon: DomainLexicon):
    result = miner(lexicon).mine(["志愿志愿志愿", "志愿啊", "志愿呢"])
    word = next(item for item in result.by_kind("word") if item.text == "志愿")
    assert word.doc_freq == 3, "同一篇里复读三遍不等于三个独立来源"
    fragment = next(
        item for item in result.skipped if item.kind == "ngram" and item.text == "志愿"
    )
    assert fragment.doc_freq == 3, "n-gram 分支同样按条计，出现 5 次也只算 3 条"


def test_below_min_phrase_freq_is_reported_as_a_skip(lexicon: DomainLexicon):
    result = miner(lexicon).mine(CORPUS)
    assert "名额分配" in skipped(result, SKIP_BELOW_FREQ)
    assert "分数线" in skipped(result, SKIP_BELOW_FREQ)
    assert "么改" in skipped(result, SKIP_BELOW_FREQ)
    entry = next(item for item in result.skipped if item.text == "名额分配")
    assert entry.kind == "word" and entry.doc_freq == 0
    assert "min_phrase_freq" in entry.detail, "降级明细必须点名是哪条阈值拦住的"
    assert "名额分配" not in phrase_texts(result)


def test_fragments_inside_one_dictionary_term_are_not_re_discovered(lexicon: DomainLexicon):
    result = miner(lexicon).mine(CORPUS)
    ngrams = {item.text for item in result.by_kind("ngram")}
    covered = skipped(result, SKIP_IN_DOMAIN_TERM)
    assert "愿填" not in ngrams and "愿填" in covered, "志愿填报内部的碎片不该再算一个新短语"
    assert "上海中" in covered and "海中考" in covered, "上海中考内部的三 gram 同理"
    assert "分数线" in skipped(result, SKIP_BELOW_FREQ), "本批语料没有它，先报频次不足"


def test_cross_term_boundary_is_kept_as_a_candidate(lexicon: DomainLexicon):
    """`考志` 的两个字分属两个词典词：mask 口径会把它误判成已覆盖，spans 口径不会。"""
    result = miner(lexicon).mine(CORPUS)
    assert "考志" in {item.text for item in result.by_kind("ngram")}
    assert "考志" not in skipped(result, SKIP_IN_DOMAIN_TERM)


# ---------------------------------------------------------------------- TF-IDF


def test_idf_prefers_rarer_documents_and_is_deterministic(lexicon: DomainLexicon):
    result = miner(lexicon).mine(CORPUS)
    by_text = {item.text: item.score for item in result.by_kind("ngram")}
    assert by_text["考志"] > by_text["怎么"], "df=3 的串必须比 df=4 的串更稀奇"
    assert result == miner(lexicon).mine(CORPUS), "同一批语料 + 同一版本常量 → 逐字段相同"
    assert all(item.score_basis == SCORE_BASIS_NGRAM_IDF for item in result.by_kind("ngram"))


def test_vectorizer_version_and_corpus_size_travel_with_the_result(lexicon: DomainLexicon):
    result = miner(lexicon).mine(CORPUS)
    assert result.doc_count == 4
    assert result.vectorizer_version.startswith("char-ngram-")
    summary = result.summary()
    assert summary["doc_count"] == 4
    assert summary["extractor_version"] == result.extractor_version
    assert summary["score_basis"]["ngram"] == SCORE_BASIS_NGRAM_IDF
    assert summary["score_basis"]["word"] == SCORE_BASIS_DOC_RATIO
    assert summary["score_basis"]["pmi_phrase"] == SCORE_BASIS_PMI


def test_word_score_is_a_document_ratio_not_a_weight(lexicon: DomainLexicon):
    result = miner(lexicon).mine(CORPUS)
    words = {item.text: item for item in result.by_kind("word")}
    assert words["志愿"].doc_freq == 4 and words["志愿"].score == pytest.approx(1.0)
    assert words["上海中考"].score == pytest.approx(0.75)
    assert all(0.0 <= item.score <= 1.0 for item in result.by_kind("word"))


# ---------------------------------------------------------------------- PMI 与阈值


def test_pmi_is_reported_with_its_own_scale(lexicon: DomainLexicon):
    result = miner(lexicon, min_pmi=0.0).mine(CORPUS)
    phrases = result.by_kind("pmi_phrase")
    assert phrases, "阈值放到 0 时，过了频次的候选应当给出粘合度结论"
    for item in phrases:
        assert item.pmi == item.score and item.score_basis == SCORE_BASIS_PMI
        assert item.doc_freq >= 3


def test_raising_the_pmi_threshold_degrades_instead_of_quietly_shrinking(lexicon: DomainLexicon):
    loose = miner(lexicon, min_pmi=0.0).mine(CORPUS)
    tight = miner(lexicon, min_pmi=50.0).mine(CORPUS)
    assert tight.by_kind("pmi_phrase") == ()
    dropped = {item.text for item in tight.skipped if item.reason == SKIP_PMI_BELOW}
    assert dropped == {item.text for item in loose.by_kind("pmi_phrase")}, (
        "阈值拦下的每一个候选都要留下名字与理由，不能凭空少掉"
    )
    detail = next(item for item in tight.skipped if item.reason == SKIP_PMI_BELOW).detail
    assert "min_pmi" in detail and "50.0" in detail
    assert loose.by_kind("ngram") == tight.by_kind("ngram"), "PMI 阈值只管粘合度结论，不动频次结论"
    assert loose.by_kind("word") == tight.by_kind("word")


def test_one_document_corpus_refuses_to_mine_pmi(lexicon: DomainLexicon):
    result = miner(lexicon).mine(["上海中考志愿填报怎么改"])
    assert result.by_kind("pmi_phrase") == ()
    assert result.corpus_below_min_docs is True
    small = [item for item in result.skipped if item.reason == SKIP_CORPUS_SMALL]
    assert len(small) == 1, "整批只报一次'样本不足'，不给每个候选都挂一遍同样的理由"
    assert "自我确认" in small[0].detail


def test_empty_corpus_reports_missing_rather_than_zero_phrases(lexicon: DomainLexicon):
    result = miner(lexicon).mine(["   ", ""])
    assert result.phrases == ()
    assert result.doc_count == 0
    assert [item.reason for item in result.skipped] == [SKIP_CORPUS_SMALL]
    assert result.corpus_below_min_docs is True


def test_thresholds_come_from_configuration():
    settings = load_settings(ROOT / "config" / "default.yaml")
    built = PhraseMiner.from_settings(settings, DomainLexicon(LEXICON_TERMS))
    assert built.min_n == settings.nlp.char_ngram_min
    assert built.max_n == settings.nlp.char_ngram_max
    assert built.min_phrase_freq == settings.nlp.min_phrase_freq
    assert built.min_pmi == settings.nlp.min_pmi == 1.0


def test_incoherent_or_magic_thresholds_are_rejected(lexicon: DomainLexicon):
    with pytest.raises(ValueError, match="char_ngram_max"):
        PhraseMiner(lexicon, char_ngram_min=4, char_ngram_max=2)
    with pytest.raises(ValueError, match="char_ngram_min"):
        PhraseMiner(lexicon, char_ngram_min=0)
    with pytest.raises(ValueError, match="min_phrase_freq"):
        PhraseMiner(lexicon, min_phrase_freq=0)


def test_document_order_does_not_change_the_batch_result(lexicon: DomainLexicon):
    forward = miner(lexicon).mine(CORPUS)
    backward = miner(lexicon).mine(list(reversed(CORPUS)))
    assert phrase_texts(forward) == phrase_texts(backward)
    assert forward.phrases == backward.phrases, "排序由 kind/分数/文本决定，与语料顺序无关"


# ---------------------------------------------------------------------- 入库


def test_record_phrase_run_writes_rows_and_the_events_that_explain_them(storage):
    result = miner(DomainLexicon(LEXICON_TERMS), min_pmi=0.0).mine(CORPUS)
    inserted, reused = record_phrase_run(
        storage.repo, storage.run_id, result, analysis_version=VERSION, at=AT
    )
    assert (inserted, reused) == (len(result.phrases), 0)
    rows = storage.conn.execute(
        "SELECT text, kind, score, freq, extractor_version, analysis_version, source_version_id"
        " FROM phrases WHERE run_id = ? ORDER BY kind, text",
        (storage.run_id,),
    ).fetchall()
    assert len(rows) == len(result.phrases)
    assert {row["analysis_version"] for row in rows} == {VERSION}
    assert {row["source_version_id"] for row in rows} == {None}, "批次级短语不冒充某一篇的来源"
    by_key = {(row["text"], row["kind"]): row for row in rows}
    for candidate in result.phrases:
        row = by_key[(candidate.text, candidate.kind)]
        assert row["score"] == candidate.score and row["freq"] == candidate.doc_freq
        assert row["extractor_version"] == result.extractor_version

    events = storage.repo.events(storage.run_id)
    kinds = [event["kind"] for event in events]
    assert kinds.count("phrase_mining") == 1
    assert kinds.count("phrase_skipped") == len(result.skipped), "每条降级都要有一条事件"
    mining = json.loads(storage.repo.events(storage.run_id, kind="phrase_mining")[0]["detail_json"])
    assert mining["analysis_version"] == VERSION
    assert mining["counts"]["word"] == len(result.by_kind("word"))
    assert mining["reused"] == 0
    first_skip = json.loads(storage.repo.events(storage.run_id, kind="phrase_skipped")[0]["detail_json"])
    assert first_skip["analysis_version"] == VERSION and first_skip["reason"] in {
        SKIP_BELOW_FREQ,
        SKIP_IN_DOMAIN_TERM,
        SKIP_PMI_BELOW,
        SKIP_CORPUS_SMALL,
    }


def test_replaying_the_same_batch_writes_no_second_row(storage):
    result = miner(DomainLexicon(LEXICON_TERMS)).mine(CORPUS)
    record_phrase_run(storage.repo, storage.run_id, result, analysis_version=VERSION, at=AT)
    before = storage.conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0]
    inserted, reused = record_phrase_run(
        storage.repo, storage.run_id, result, analysis_version=VERSION, at=AT
    )
    assert inserted == 0 and reused == len(result.phrases)
    assert storage.conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0] == before, (
        "表级 UNIQUE 里的 NULL 互不相等，重复行只能靠写入前的自然键查询拦住"
    )


def test_a_new_analysis_version_adds_rows_without_touching_history(storage):
    result = miner(DomainLexicon(LEXICON_TERMS)).mine(CORPUS)
    record_phrase_run(storage.repo, storage.run_id, result, analysis_version=VERSION, at=AT)
    history = [
        dict(row)
        for row in storage.conn.execute(
            "SELECT text, kind, score, freq, analysis_version FROM phrases ORDER BY kind, text"
        )
    ]

    changed = miner(DomainLexicon(LEXICON_TERMS), min_pmi=0.0).mine(CORPUS)
    record_phrase_run(
        storage.repo, storage.run_id, changed, analysis_version="p0-rule-baseline.3", at=AT
    )

    kept = [
        dict(row)
        for row in storage.conn.execute(
            "SELECT text, kind, score, freq, analysis_version FROM phrases"
            " WHERE analysis_version = ? ORDER BY kind, text",
            (VERSION,),
        )
    ]
    assert kept == history, "旧版本行一字不动：分析只追加，不改写历史结论"
    total = storage.conn.execute("SELECT COUNT(*) FROM phrases").fetchone()[0]
    assert total == len(result.phrases) + len(changed.phrases)
