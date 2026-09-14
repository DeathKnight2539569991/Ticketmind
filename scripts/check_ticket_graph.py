import argparse
import json
from pathlib import Path
from tempfile import TemporaryFile

from ticketmind.agent.dev_cache import (
    CachedQueryEmbeddings, CachedUnderstanding, build_single_attempt_embeddings,
)
from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.agent.state import TicketAgentState
from ticketmind.core.config import MilvusSettings, QwenSettings
from ticketmind.retrieval.case_collection import CASE_COLLECTION, EMBEDDING_DIMENSION, VECTOR_INDEX
from ticketmind.retrieval.milvus_client import build_milvus_client


def sample_state() -> TicketAgentState:
    return {
        "subject": "API 调用失败",
        "body": (
            "今天上午调用订单查询接口时多次返回 E_TIMEOUT。"
            "运行环境是 Python 3.12，昨天还可以正常调用。"
        ),
    }

def run_check(*, cache_dir: Path, check_only: bool = False,
              allow_understanding: bool = False, allow_embedding: bool = False) -> None:
    settings = QwenSettings()
    milvus = MilvusSettings()
    if not settings.api_key.get_secret_value().strip() or not settings.workspace_id.strip():
        raise ValueError("请配置非空 DASHSCOPE_API_KEY 和 DASHSCOPE_WORKSPACE_ID；不要输出密钥")
    if settings.embedding_model != "text-embedding-v4":
        raise ValueError("historical_cases_v1 仅用于 text-embedding-v4 / 1024 维")
    initial_state = sample_state()
    query = build_retrieval_query(**initial_state)
    understanding = CachedUnderstanding(cache_dir / "understanding.json", allow_call=allow_understanding)
    embeddings = CachedQueryEmbeddings(
        settings, cache_dir / "query.json", allow_call=allow_embedding,
        factory=lambda: build_single_attempt_embeddings(settings),
    )
    # Validate BOTH caches before opening any external client or spending calls.
    cached_understanding = understanding.read(settings=settings, **initial_state)
    cached_vector = embeddings.read(query)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryFile(dir=cache_dir) as probe:
        probe.write(b"cache write preflight")
        probe.flush()
    print(json.dumps({
        "understanding_cache": "hit" if cached_understanding else "missing",
        "query_cache": "hit" if cached_vector else "missing",
        "cache_writable": True,
    }))
    if check_only:
        print("无外部调用预检通过；未连接模型、Milvus 或 PostgreSQL。缓存缺失不代表链路可回放。")
        return
    if cached_understanding is None and not allow_understanding:
        raise RuntimeError("缺少理解缓存；需已有授权后显式指定 --allow-understanding")
    if cached_vector is None and not allow_embedding:
        raise RuntimeError("缺少查询缓存；需已有授权后显式指定 --allow-embedding")

    client = build_milvus_client(milvus)
    try:
        # Read-only dependency checks must pass before either model is invoked.
        if not client.has_collection(collection_name=CASE_COLLECTION, timeout=milvus.timeout_seconds):
            raise RuntimeError("历史案例集合不存在；先按 README 初始化")
        description = client.describe_collection(collection_name=CASE_COLLECTION, timeout=milvus.timeout_seconds)
        fields = {field["name"]: field for field in description["fields"]}
        if int(fields.get("embedding", {}).get("params", {}).get("dim", 0)) != EMBEDDING_DIMENSION:
            raise RuntimeError("集合向量维度不匹配")
        index = client.describe_index(collection_name=CASE_COLLECTION, index_name=VECTOR_INDEX,
                                      timeout=milvus.timeout_seconds)
        if index.get("metric_type") != "COSINE":
            raise RuntimeError("集合索引度量不是 COSINE")
        rows = client.query(collection_name=CASE_COLLECTION, filter="", output_fields=["count(*)"],
                            consistency_level="Strong", timeout=milvus.timeout_seconds)
        if not rows or rows[0]["count(*)"] < 1:
            raise RuntimeError("历史案例集合为空；先按 README 导入")
        graph = build_ticket_graph(
            settings, embeddings=embeddings, client=client, top_k=3,
            timeout=milvus.timeout_seconds, understanding_fn=understanding,
        )
        final_state = graph.invoke(initial_state)
        validate_result(initial_state, final_state)
        print("图理解与检索检查通过；Milvus 为本次真实查询。")
        print(json.dumps(final_state, ensure_ascii=False, indent=2,
                         default=lambda value: value.model_dump()))
    finally:
        client.close()
        print(json.dumps({
            "understanding_attempts": understanding.calls, "embedding_attempts": embeddings.calls,
            "understanding_cache_hits": understanding.cache_hits, "query_cache_hits": embeddings.cache_hits,
        }))


def validate_result(initial_state: TicketAgentState, final_state: TicketAgentState) -> None:

    assert final_state["subject"] == initial_state["subject"]
    assert final_state["body"] == initial_state["body"]

    understanding = final_state["understanding"]
    assert isinstance(understanding, TicketUnderstanding)
    assert understanding.error_codes == ["E_TIMEOUT"]
    assert any(
        "Python 3.12" in item
        for item in understanding.environment
    )

    assert final_state["retrieval_query"] == build_retrieval_query(**initial_state)
    assert final_state["retrieval_hits"], "未返回检索证据"
    assert all(hit.source_id and hit.text for hit in final_state["retrieval_hits"])


def main() -> None:
    parser = argparse.ArgumentParser(description="验证理解 → 检索；默认仅复用真实模型缓存，查询真实 Milvus")
    parser.add_argument("--check-only", action="store_true", help="仅检查配置、缓存与写入权限，无外部调用")
    parser.add_argument("--allow-understanding", action="store_true", help="授权缓存缺失时一次理解调用尝试")
    parser.add_argument("--allow-embedding", action="store_true", help="授权缓存缺失时一次查询向量调用尝试")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data/cache/graph/api_timeout")
    args = parser.parse_args()
    run_check(**vars(args))


if __name__ == "__main__":
    main()
