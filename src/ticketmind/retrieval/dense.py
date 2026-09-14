import math
from pydantic import BaseModel, ConfigDict, Field
from pymilvus import MilvusClient
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION, CASE_COLLECTION
class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid",allow_inf_nan=False)

    source_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    score: float

def search_case_vectors(
        client: MilvusClient,
        query_vectors: list[float],
        *,
        top_k: int,
        timeout:float
)-> list[RetrievalHit]:
    if not 1<=top_k<=100:
        raise ValueError("top_k 必须在 1 到 100 之间")
    if len(query_vectors) != EMBEDDING_DIMENSION:
        raise ValueError(f"查询向量的维度必须为 {EMBEDDING_DIMENSION}")
    if not all(math.isfinite(x) for x in query_vectors):
        raise ValueError("查询向量中包含非有限值")
    if not any(value != 0 for value in query_vectors):
        raise ValueError("查询向量不能全为零")
    results=client.search(
        collection_name=CASE_COLLECTION,
        data=[query_vectors],
        anns_field="embedding",
        search_params={"metric_type": "COSINE"},
        output_fields=["source_id", "text"],
        limit=top_k,
        timeout=timeout,
        consistency_level="Strong"
    )
    if len(results) != 1:
        raise RuntimeError("单条查询未返回对应的一组结果")

    return [
        RetrievalHit(
            source_id=hit["entity"]["source_id"],
            text=hit["entity"]["text"],
            score=hit["distance"],
        )
        for hit in results[0]
    ]