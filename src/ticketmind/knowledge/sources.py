import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ticketmind.knowledge.corpus import HistoricalCase, build_case_text, load_historical_cases


@dataclass(frozen=True)
class CorpusSnapshot:
    version: str
    cases: dict[str, HistoricalCase]

    def evidence(self, hits) -> list[dict]:
        from ticketmind.retrieval.schemas import EvidenceHit
        evidence = []
        for rank, hit in enumerate(hits, 1):
            case = self.cases.get(hit.source_id)
            if case is None or build_case_text(case) != hit.text:
                raise ValueError("Milvus 来源或文本与本次语料版本不匹配")
            if isinstance(hit, EvidenceHit):
                if hit.corpus_version != self.version or hit.title != case.request.subject:
                    raise ValueError("检索证据版本不匹配")
                evidence.append({**hit.model_dump(), "synthetic": True})
                continue
            evidence.append({**hit.model_dump(), "corpus_version": self.version,
                             "title": case.request.subject, "rank": rank, "dense_score": hit.score,
                             "bm25_score": None, "fusion_score": None, "retrieval_mode": "dense",
                             "synthetic": True})
        return evidence


def load_sources(path: Path) -> CorpusSnapshot:
    cases = {case.source_id: case for case in load_historical_cases(path)}
    content = json.dumps([cases[key].model_dump(mode="json") for key in sorted(cases)],
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return CorpusSnapshot("synthetic-v2-" + hashlib.sha256(content.encode()).hexdigest(), cases)
