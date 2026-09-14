import dashscope
from langchain_community.embeddings import DashScopeEmbeddings
from ticketmind.core.config import QwenSettings
def build_embedding_client(
        settings: QwenSettings
):
    dashscope.base_http_api_url=(
        f"https://{settings.workspace_id}.cn-beijing.maas.aliyuncs.com"
        "/api/v1"
    )
    return DashScopeEmbeddings(
        model=settings.embedding_model,
        dashscope_api_key=settings.api_key.get_secret_value(),
        max_retries=1)