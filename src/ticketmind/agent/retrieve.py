from collections.abc import Callable

from langchain_core.embeddings import Embeddings
from pymilvus import MilvusClient

from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.state import RetrievalUpdate, TicketAgentState
from ticketmind.retrieval.dense import search_case_vectors


def build_retrieval_query(*, subject: str, messages: list[AgentMessage]) -> str:
    customer_messages = "\n\n".join(
        message.content for message in messages if message.role == "customer"
    )
    return f"标题：{subject}\n\n客户问题：{customer_messages}"


def retrieve_ticket(
    state: TicketAgentState,
    *,
    embeddings: Embeddings,
    client: MilvusClient,
    top_k: int,
    timeout: float | Callable[[], float],
) -> RetrievalUpdate:
    query = build_retrieval_query(subject=state["subject"], messages=state["messages"])
    query_vector = embeddings.embed_query(query)
    hits = search_case_vectors(
        client=client,
        query_vectors=query_vector,
        top_k=top_k,
        timeout=timeout() if callable(timeout) else timeout,
    )
    return {
        "retrieval_query": query,
        "retrieval_hits": hits,
    }
