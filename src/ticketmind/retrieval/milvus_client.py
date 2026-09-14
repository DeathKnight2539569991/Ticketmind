from pymilvus import MilvusClient
from ticketmind.core.config import MilvusSettings
def build_milvus_client(settings: MilvusSettings) -> MilvusClient:
    """Build a Milvus client."""
    return MilvusClient(
        uri=settings.uri,
        timeout=settings.timeout_seconds
    )