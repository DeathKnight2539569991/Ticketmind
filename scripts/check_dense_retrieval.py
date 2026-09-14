from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ticketmind.core.config import MilvusSettings, QwenSettings
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION
from ticketmind.retrieval.dense import search_case_vectors
from ticketmind.retrieval.embeddings import build_embedding_client
from ticketmind.retrieval.milvus_client import build_milvus_client


class QueryCache(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
    )

    query: str
    model: str
    vector: list[float] = Field(
        min_length=EMBEDDING_DIMENSION,
        max_length=EMBEDDING_DIMENSION,
    )
def main() -> None:
    query = (
        "上传 CSV 后，预览把姓名和邮箱挤在同一列。"
        "文件实际用分号分隔，导入选项目前选的是逗号。"
    )

    project_root = Path(__file__).resolve().parents[1]
    cache_path = (
        project_root / "data/cache/queries/csv_separator.json"
    )

    qwen_settings = QwenSettings()
    milvus_settings = MilvusSettings()

    if qwen_settings.embedding_model != "text-embedding-v4":
        raise ValueError("查询必须使用与历史案例一致的模型")

    client = build_milvus_client(milvus_settings)

    try:
        if cache_path.exists():
            cache = QueryCache.model_validate_json(
                cache_path.read_text(encoding="utf-8")
            )
            if (
                cache.query != query
                or cache.model != qwen_settings.embedding_model
            ):
                raise ValueError("查询或模型已变化，请为新样本换一个缓存文件名")
            print("复用查询向量，未调用模型")
        else:
            embeddings = build_embedding_client(qwen_settings)
            cache = QueryCache(
                query=query,
                model=qwen_settings.embedding_model,
                vector=embeddings.embed_query(query),
            )

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_path.with_suffix(".tmp")
            temporary_path.write_text(
                cache.model_dump_json(indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(cache_path)
            print("查询向量已生成并缓存")

        hits = search_case_vectors(
            client,
            cache.vector,
            top_k=3,
            timeout=milvus_settings.timeout_seconds,
        )

        print(f"\n查询：{query}")
        for rank, hit in enumerate(hits, start=1):
            print(
                f"\n第 {rank} 名：{hit.source_id}"
                f" | COSINE={hit.score:.4f}"
            )
            print(hit.text)

        if not hits:
            raise RuntimeError("没有返回候选，请检查集合中的数据")
    finally:
        client.close()


if __name__ == "__main__":
    main()