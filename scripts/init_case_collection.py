from pprint import pprint

from ticketmind.core.config import MilvusSettings
from ticketmind.retrieval.case_collection import (
    CASE_COLLECTION,
    VECTOR_INDEX,
    create_case_collection,
)
from ticketmind.retrieval.milvus_client import build_milvus_client


def main() -> None:
    settings = MilvusSettings()
    client = build_milvus_client(settings)

    try:
        exists = client.has_collection(
            collection_name=CASE_COLLECTION,
            timeout=settings.timeout_seconds,
        )

        if exists:
            print("集合已存在，保留现有结构与数据")
        else:
            create_case_collection(
                client,
                timeout=settings.timeout_seconds,
            )
            print("集合创建完成")

        print("集合结构：")
        pprint(
            client.describe_collection(
                collection_name=CASE_COLLECTION,
                timeout=settings.timeout_seconds,
            )
        )

        print("向量索引：")
        pprint(
            client.describe_index(
                collection_name=CASE_COLLECTION,
                index_name=VECTOR_INDEX,
                timeout=settings.timeout_seconds,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()