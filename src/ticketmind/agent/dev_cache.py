"""Opt-in adapters for repeated local verification, never production defaults."""

from collections.abc import Callable
from pathlib import Path

from langchain_core.embeddings import Embeddings

from ticketmind.agent.run_cache import QueryVectorCache, load_cache, query_fingerprint, save_cache
from ticketmind.core.config import QwenSettings
from ticketmind.retrieval.case_collection import EMBEDDING_DIMENSION
from ticketmind.retrieval.embeddings import build_embedding_client
from ticketmind.retrieval.transport import SingleRequestSession


def build_single_attempt_embeddings(settings: QwenSettings) -> Embeddings:
    embeddings = build_embedding_client(settings)
    sdk_client = embeddings.client

    class SingleAttemptClient:
        @staticmethod
        def call(**kwargs):
            with SingleRequestSession() as session:
                return sdk_client.call(
                    **kwargs,
                    api_key=settings.api_key.get_secret_value(),
                    dimension=EMBEDDING_DIMENSION,
                    request_timeout=30,
                    session=session,
                )

    embeddings.client = SingleAttemptClient
    return embeddings


class CachedQueryEmbeddings(Embeddings):
    def __init__(
        self,
        settings: QwenSettings,
        path: Path,
        *,
        factory: Callable[[], Embeddings],
        allow_call: bool = False,
    ):
        self.settings = settings
        self.path = path
        self.factory = factory
        self.allow_call = allow_call
        self.calls = 0
        self.cache_hits = 0

    def read(self, query: str) -> QueryVectorCache | None:
        cache = load_cache(
            self.path,
            QueryVectorCache,
            expected_fingerprint=query_fingerprint(settings=self.settings, query=query),
        )
        if cache is not None and (cache.query, cache.model) != (
            query,
            self.settings.embedding_model,
        ):
            raise ValueError("查询缓存元数据与请求不匹配")
        return cache

    def embed_query(self, text: str) -> list[float]:
        cache = self.read(text)
        if cache is not None:
            self.cache_hits += 1
            return cache.vector
        if not self.allow_call or self.calls >= 1:
            raise RuntimeError("查询缓存缺失；需明确授权 --allow-embedding，每次运行最多一次尝试")
        self.calls += 1
        vector = self.factory().embed_query(text)
        cache = QueryVectorCache(
            query=text,
            model=self.settings.embedding_model,
            request_fingerprint=query_fingerprint(settings=self.settings, query=text),
            vector=vector,
        )
        save_cache(self.path, cache)
        return cache.vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("开发查询缓存不用于文档入库")
