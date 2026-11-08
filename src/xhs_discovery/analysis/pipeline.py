"""P0-D 分析编排：把库里的观察事实一次跑成规则基线分析并导出五类产物。

编排层只做三件事：按依赖顺序调用各层、用同一个时间戳盖住整批、把各层结果收进一个可打印的
`PipelineOutcome`。它不直接写业务行（写库全在 `store`），也不碰预算账本——分析是纯本地计算，
没有任何外部访问，把它记成一笔花费会把"跑过一次分析"和"花掉一格平台额度"混进同一个口径。

顺序是硬约束，不是风格：`phrase_mining` 读语料文本，`extraction` 把证据与问题落库，
`clustering` 只认库里的 `problems` 行，`scoring` 只认库里的 `needs` 与 `evidence` 行，
`graph` 与 `export` 再从落库结果重建。跳步或把内存对象直接喂给后一层，导出内容就和数据库
分成两套真相，反读校验也就无从对照。

版本纪律（DEVELOPMENT_PLAN 第 5.2 节末尾）：本层只往**新的** `analysis_version` 写行，
并且拒绝 P0-C 在线层自己那两份常量。往 `p0-preliminary.1` 里补写离线行，等于在已复核通过的
在线结论上盖一层它从未见过的成员，依赖它的 `branch_stats` 历史奖励也就跟着被改了口径。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from xhs_discovery._version import ANALYSIS_VERSION, EXTRACTOR_VERSION
from xhs_discovery.analysis.clustering import ClusterOutcome, ConservativeMerger
from xhs_discovery.analysis.corpus import Corpus, load_corpus
from xhs_discovery.analysis.export import ExportResult, export_run
from xhs_discovery.analysis.extraction import ExtractionOutcome, ProblemExtractor
from xhs_discovery.analysis.graph import Graph, GraphIntegrity, build_run_graph
from xhs_discovery.analysis.lexicon import DomainLexicon, load_lexicon
from xhs_discovery.analysis.phrase_mining import PhraseMiner, PhraseSet
from xhs_discovery.analysis.scoring import DemandScorer, ScoreOutcome
from xhs_discovery.analysis.store import (
    ClusterWrite,
    ExtractionWrite,
    ScoringWrite,
    record_clustering,
    record_extraction,
    record_phrase_run,
    record_scoring,
)
from xhs_discovery.config import Settings, load_intent_rules
from xhs_discovery.discovery.extract import EXTRACTOR_VERSION as ONLINE_EXTRACTOR_VERSION
from xhs_discovery.discovery.merge import PRELIMINARY_ANALYSIS_VERSION
from xhs_discovery.models.common import utc_now
from xhs_discovery.paths import RuntimePaths
from xhs_discovery.storage.repository import Repository
from xhs_discovery.text.intent import IntentMatcher
from xhs_discovery.text.normalize import KeywordNormalizer


class AnalysisRules:
    """从配置装配分析层的规则件：词典、意图规则、短语参数。

    路径来自 `Settings.nlp`，缺文件直接抛（`load_lexicon` 的口径），不静默退化成逐字符——
    那样短语发现照样"跑通"，只是每次切分都不一样，而结果里看不出区别。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        lexicon: DomainLexicon | None = None,
        normalizer: KeywordNormalizer | None = None,
    ) -> None:
        self.settings = settings
        self.lexicon = lexicon if lexicon is not None else load_lexicon(settings)
        self.matcher = IntentMatcher(load_intent_rules(Path(settings.nlp.intent_rules_path)))
        self.normalizer = normalizer if normalizer is not None else KeywordNormalizer()

    def miner(self, *, extractor_version: str) -> PhraseMiner:
        nlp = self.settings.nlp
        return PhraseMiner(
            self.lexicon,
            char_ngram_min=nlp.char_ngram_min,
            char_ngram_max=nlp.char_ngram_max,
            min_phrase_freq=nlp.min_phrase_freq,
            min_pmi=nlp.min_pmi,
            extractor_version=extractor_version,
        )

    def extractor(self, *, analysis_version: str, extractor_version: str) -> ProblemExtractor:
        return ProblemExtractor(
            matcher=self.matcher,
            normalizer=self.normalizer,
            lexicon=self.lexicon,
            analysis_version=analysis_version,
            extractor_version=extractor_version,
        )


@dataclass(frozen=True)
class PipelineOutcome:
    """一次分析的完整结果：各层的计划、落库计数、两张图与导出凭证。

    字段分两类是有意为之——`*_outcome` 是**决策**（可复算的中间物，撤销与复核要看它），
    `*_write` 是**落库计数**。只给后者的话，"这次一条都没归上"和"归上了但库里早有行"
    在调用方眼里长得一样。
    """

    run_id: str
    analysis_version: str
    extractor_version: str
    at: datetime
    corpus: Corpus
    phrase_set: PhraseSet
    phrases: tuple[int, int]
    extraction: ExtractionOutcome
    extraction_write: ExtractionWrite
    clustering: ClusterOutcome
    clustering_write: ClusterWrite
    scoring: ScoreOutcome
    scoring_write: ScoringWrite
    discovery_graph: Graph
    demand_graph: Graph
    integrity: GraphIntegrity
    export: ExportResult

    def summary(self) -> dict[str, object]:
        inserted, reused = self.phrases
        return {
            "run_id": self.run_id,
            "analysis_version": self.analysis_version,
            "extractor_version": self.extractor_version,
            "at": self.at.isoformat(),
            "corpus": self.corpus.summary(),
            "phrases": {
                "inserted": inserted,
                "reused": reused,
                "discovered": len(self.phrase_set.phrases),
                "skipped": len(self.phrase_set.skipped),
                "doc_count": self.phrase_set.doc_count,
            },
            "extraction": {**self.extraction.summary(), **self.extraction_write.as_event()},
            "clustering": {**self.clustering.summary(), **self.clustering_write.as_event()},
            "scoring": {
                "scored": len(self.scoring.scored),
                "families": len(self.scoring.families),
                "provisional": self.scoring.provisional,
                "weights": self.scoring.weights,
                **self.scoring_write.as_event(),
            },
            "graph": {
                "discovery": self.discovery_graph.summary(),
                "demand": self.demand_graph.summary(),
                "integrity": {"ok": self.integrity.ok, "codes": list(self.integrity.codes())},
            },
            "export": {
                "directory": self.export.export_dir,
                "files": [item.filename for item in self.export.files],
                "verification_ok": None
                if self.export.verification is None
                else self.export.verification.ok,
            },
        }


def run_analysis(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    run_id: str,
    settings: Settings,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
    at: datetime | None = None,
    snapshot: str | None = None,
    verify: bool = True,
    rules: AnalysisRules | None = None,
) -> PipelineOutcome:
    """跑完整离线分析并导出五类产物；所有写入都带 `analysis_version`，历史行一字不动。

    规则件缺省时从 `settings` 装配。`AnalysisRules.normalizer` 的歧义种子与地域词表是**数据集**
    声明的（在线层从 fixture 载荷读），库里没有留副本，所以要跑数据集专属的规范化口径，
    由调用方把装配好的 `rules` 传进来——不给就用空词表：抽取仍然可用，只是抽不出地域属性，
    这会在 `report.json` 的属性缺口统计里露出来，比悄悄换一套词表诚实。
    """
    if analysis_version == PRELIMINARY_ANALYSIS_VERSION:
        raise ValueError(
            f"analysis_version={analysis_version} 是 P0-C 在线初步归并的地盘，"
            "离线分析写进去会改写已复核过的在线簇与依赖它的历史奖励"
        )
    if extractor_version == ONLINE_EXTRACTOR_VERSION:
        raise ValueError(
            f"extractor_version={extractor_version} 属于在线抽取器，离线分析必须用自己的版本，"
            "否则需求图里的证据分不清是哪一层写的"
        )
    stamp = at if at is not None else utc_now()
    repo = Repository(conn, migrate=False)
    repo.require_run(run_id)
    parts = rules if rules is not None else AnalysisRules(settings)

    corpus = load_corpus(conn, run_id)
    phrase_set = parts.miner(extractor_version=extractor_version).mine(corpus.texts())
    phrases = record_phrase_run(
        repo, run_id, phrase_set, analysis_version=analysis_version, at=stamp
    )
    extraction = parts.extractor(
        analysis_version=analysis_version, extractor_version=extractor_version
    ).extract(corpus.units)
    extraction_write = record_extraction(repo, run_id, extraction, at=stamp)
    clustering = ConservativeMerger(
        repo,
        run_id,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
        matcher=parts.matcher,
    ).plan(corpus)
    clustering_write = record_clustering(repo, run_id, clustering, at=stamp)
    scoring = DemandScorer(
        repo,
        run_id,
        analysis_version=analysis_version,
        weights=settings.scoring.weights,
        quantile_min_sample=settings.scoring.quantile_min_sample,
        # 近期窗口参照 = 本批评分时间（C-12）：只影响窗口，不参与发布日期解析，
        # 逐条分数里带着 window_reference_at/source 可审计。
        reference_at=stamp,
    ).score()
    scoring_write = record_scoring(repo, run_id, scoring, at=stamp)
    discovery, demand, integrity = build_run_graph(
        repo,
        run_id,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
    )
    export = export_run(
        conn,
        paths,
        run_id,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
        at=stamp,
        snapshot=snapshot,
        verify=verify,
    )
    return PipelineOutcome(
        run_id=run_id,
        analysis_version=analysis_version,
        extractor_version=extractor_version,
        at=stamp,
        corpus=corpus,
        phrase_set=phrase_set,
        phrases=phrases,
        extraction=extraction,
        extraction_write=extraction_write,
        clustering=clustering,
        clustering_write=clustering_write,
        scoring=scoring,
        scoring_write=scoring_write,
        discovery_graph=discovery,
        demand_graph=demand,
        integrity=integrity,
        export=export,
    )


__all__ = [
    "AnalysisRules",
    "PipelineOutcome",
    "run_analysis",
]
