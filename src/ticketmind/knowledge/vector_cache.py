import hashlib
import json
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field,field_validator
from ticketmind.core.config import QwenSettings
from ticketmind.knowledge.corpus import HistoricalCase,build_case_text
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION,SOURCE_ID_MAX_BYTES,TEXT_MAX_BYTES
from ticketmind.retrieval.embeddings import build_embedding_client
class VectorRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
    )

    source_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    embedding: list[float] = Field(
        min_length=EMBEDDING_DIMENSION,
        max_length=EMBEDDING_DIMENSION,
    )

    @field_validator("embedding")
    @classmethod
    def reject_zero_vector(cls, value: list[float]) -> list[float]:
        if not any(number != 0 for number in value):
            raise ValueError("向量不能全为零")
        return value
class VectorCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    records: list[VectorRecord]
def load_or_build_records(
    cases: list[HistoricalCase],
    settings: QwenSettings,
    cache_dir: Path,
    *,
    allow_embedding: bool = False,
) -> list[VectorRecord]:
    documents = [
        {"source_id": case.source_id, "text": build_case_text(case)}
        for case in cases
    ]

    for document in documents:
        if len(document["source_id"].encode("utf-8")) > SOURCE_ID_MAX_BYTES:
            raise ValueError("source_id 超出集合的字节长度限制")
        if len(document["text"].encode("utf-8")) > TEXT_MAX_BYTES:
            raise ValueError(f"{document['source_id']} 的文本过长")

    identity = {
        "cache_version": 1,
        "provider": "dashscope",
        "region": "cn-beijing",
        "workspace_id": settings.workspace_id,
        "model": settings.embedding_model,
        "dimension": EMBEDDING_DIMENSION,
        "text_type": "document",
        "documents": documents,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    cache_path = cache_dir / f"{fingerprint}.json"

    if cache_path.exists():
        cache = VectorCache.model_validate_json(
            cache_path.read_text(encoding="utf-8")
        )
        cached_documents = [
            {"source_id": record.source_id, "text": record.text}
            for record in cache.records
        ]
        if cache.fingerprint != fingerprint or cached_documents != documents:
            raise ValueError("缓存与当前语料不一致，停止导入")
        print(f"命中缓存：{len(cache.records)} 条，未调用模型")
        return cache.records

    if not allow_embedding:
        raise RuntimeError(
            "没有匹配的向量缓存。确认需要生成后，使用 --allow-embedding"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)

    print("没有匹配缓存，开始生成历史案例文档向量")
    client = build_embedding_client(settings)
    vectors = client.embed_documents(
        [document["text"] for document in documents]
    )

    records = [
        VectorRecord(**document, embedding=vector)
        for document, vector in zip(documents, vectors, strict=True)
    ]
    cache = VectorCache(
        fingerprint=fingerprint,
        records=records,
    )

    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(
        cache.model_dump_json(indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)

    print(f"向量缓存已保存：{cache_path}")
    return records