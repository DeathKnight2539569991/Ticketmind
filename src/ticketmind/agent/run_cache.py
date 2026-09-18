"""Local development caches; these helpers never call a model."""

import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ticketmind.core.config import QwenSettings
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION


class QueryVectorCache(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    query: str
    model: str
    request_fingerprint: str
    dimension: int = Field(
        default=EMBEDDING_DIMENSION,
        ge=EMBEDDING_DIMENSION,
        le=EMBEDDING_DIMENSION,
    )
    vector: list[float] = Field(
        min_length=EMBEDDING_DIMENSION,
        max_length=EMBEDDING_DIMENSION,
    )

    @field_validator("vector")
    @classmethod
    def validate_vector(cls, vector: list[float]) -> list[float]:
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("查询向量必须全部为有限数值")
        if not any(value != 0 for value in vector):
            raise ValueError("查询向量不能全为零")
        return vector


def calculate_request_fingerprint(request: dict[str, object]) -> str:
    serialized = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def query_fingerprint(*, settings: QwenSettings, query: str) -> str:
    return calculate_request_fingerprint(
        {
            "cache_version": 1,
            "endpoint": f"https://{settings.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1",
            "model": settings.embedding_model,
            "query": query,
            "dimension": EMBEDDING_DIMENSION,
            "text_type": "query",
        }
    )


def save_cache(path: Path, cache: BaseModel) -> None:
    """Write fully before replacing the destination; propagate storage errors."""
    serialized = cache.model_dump_json(indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


CacheType = TypeVar("CacheType", bound=BaseModel)


def load_cache(
    path: Path,
    cache_type: type[CacheType],
    *,
    expected_fingerprint: str,
) -> CacheType | None:
    """Only absence returns None; corrupt or mismatched caches must stop the run."""
    if not path.exists():
        return None
    cache = cache_type.model_validate_json(path.read_text(encoding="utf-8"))
    if cache.request_fingerprint != expected_fingerprint:
        raise ValueError(f"缓存与当前请求不匹配，请使用对应的新缓存路径：{path.name}")
    return cache
