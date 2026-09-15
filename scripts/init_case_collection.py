from pprint import pprint
import argparse
from ticketmind.core.config import ProcessingSettings
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.versioned_collection import create_versioned_collection

from ticketmind.core.config import MilvusSettings
from ticketmind.retrieval.case_collection import (
    CASE_COLLECTION,
    VECTOR_INDEX,
    create_case_collection,
)
from ticketmind.retrieval.milvus_client import build_milvus_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--versioned", action="store_true", help="创建 M3 内容版本化集合，保留旧集合")
    args = parser.parse_args()
    settings = MilvusSettings()
    client = build_milvus_client(settings)

    try:
        if args.versioned:
            name = create_versioned_collection(client, load_sources(ProcessingSettings().corpus_path),
                                              timeout=settings.timeout_seconds)
            print(f"M3 集合已就绪（尚需导入核对）：{name}")
            return
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
