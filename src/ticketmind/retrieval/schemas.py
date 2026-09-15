from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RetrievalMode = Literal["dense", "bm25", "hybrid"]


class EvidenceHit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_id: str = Field(min_length=1)
    corpus_version: str
    title: str
    text: str = Field(min_length=1)
    rank: int = Field(ge=1)
    dense_score: float | None = None
    bm25_score: float | None = None
    fusion_score: float | None = None
    dense_rank: int | None = None
    bm25_rank: int | None = None
    retrieval_mode: RetrievalMode


class RetrievalError(RuntimeError):
    """Stable, non-secret error identifier for persisted retrieval diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
