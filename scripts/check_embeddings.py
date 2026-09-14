import math
from pathlib import Path

from ticketmind.core.config import QwenSettings
from ticketmind.knowledge.corpus import (
    build_case_text,
    load_historical_cases,
)
from ticketmind.retrieval.embeddings import build_embedding_client


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    corpus_path = (
        project_root
        / "data"
        / "synthetic"
        / "v2"
        / "historical_cases.jsonl"
    )

    cases = load_historical_cases(corpus_path)
    texts = [build_case_text(case) for case in cases]

    embeddings = build_embedding_client(QwenSettings())

    document_vectors = embeddings.embed_documents(texts)
    query_vector = embeddings.embed_query(
        "CSV 文件使用分号分隔，但导入预览选择逗号，所有内容挤在一列。"
    )

    assert len(document_vectors) == len(cases)
    assert len(query_vector) == 1024

    for vector in [*document_vectors, query_vector]:
        assert len(vector) == 1024
        assert all(math.isfinite(value) for value in vector)
        assert any(value != 0 for value in vector)

    for case, vector in zip(cases, document_vectors, strict=True):
        print(f"{case.source_id} → {len(vector)} 维")

    print(f"查询向量：{len(query_vector)} 维")
    print("向量化验证通过")


if __name__ == "__main__":
    main()