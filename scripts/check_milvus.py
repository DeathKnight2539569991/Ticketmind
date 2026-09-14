from ticketmind.core.config import MilvusSettings
from ticketmind.retrieval.milvus_client import build_milvus_client


def main() -> None:
    settings = MilvusSettings()
    client = build_milvus_client(settings)

    try:
        collections = client.list_collections(
            timeout=settings.timeout_seconds,
        )
        print(f"当前集合：{collections}")
        print("Milvus Python 连接验证通过")
    finally:
        client.close()


if __name__ == "__main__":
    main()