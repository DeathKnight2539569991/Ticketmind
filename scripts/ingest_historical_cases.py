import argparse
from pathlib import Path

from ticketmind.core.config import MilvusSettings, QwenSettings
from ticketmind.knowledge.corpus import load_historical_cases
from ticketmind.knowledge.vector_cache import load_or_build_records
from ticketmind.retrieval.case_collection import CASE_COLLECTION
from ticketmind.retrieval.milvus_client import build_milvus_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-embedding",
        action="store_true",
        help="允许缺少缓存时调用 Embedding 模型",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    cases = load_historical_cases(
        project_root / "data/synthetic/v2/historical_cases.jsonl"
    )

    if len(cases) != 12:
        raise ValueError("本轮验证预期为 12 条历史案例")

    milvus_settings = MilvusSettings()
    qwen_settings = QwenSettings()

    if qwen_settings.embedding_model != "text-embedding-v4":
        raise ValueError("当前集合仅用于 text-embedding-v4 向量")

    client = build_milvus_client(milvus_settings)

    try:
        if not client.has_collection(
            collection_name=CASE_COLLECTION,
            timeout=milvus_settings.timeout_seconds,
        ):
            raise RuntimeError("集合不存在，请先运行集合初始化脚本")

        records = load_or_build_records(
            cases,
            qwen_settings,
            project_root / "data/cache/embeddings",
            allow_embedding=args.allow_embedding,
        )

        result = client.upsert(
            collection_name=CASE_COLLECTION,
            data=[record.model_dump() for record in records],
            timeout=milvus_settings.timeout_seconds,
        )
        print(f"写入结果：{result}")

        stored = client.get(
            collection_name=CASE_COLLECTION,
            ids=[record.source_id for record in records],
            output_fields=["source_id", "text"],
            consistency_level="Strong",
            timeout=milvus_settings.timeout_seconds,
        )

        expected = {
            record.source_id: record.text
            for record in records
        }
        actual = {
            row["source_id"]: row["text"]
            for row in stored
        }
        if len(stored) != len(records) or actual != expected:
            raise RuntimeError("读回的案例 ID 或文本与本次导入不一致")

        counts = client.query(
            collection_name=CASE_COLLECTION,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
            timeout=milvus_settings.timeout_seconds,
        )
        total = counts[0]["count(*)"]
        if total != len(records):
            raise RuntimeError(
                f"集合共有 {total} 条记录，预期 {len(records)} 条；"
                "请检查是否存在旧数据，脚本不会自动删除"
            )

        print(f"入库验证通过：{total} 条，ID 与文本一致")
    finally:
        client.close()


if __name__ == "__main__":
    main()