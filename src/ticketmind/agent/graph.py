from collections.abc import Callable
from time import monotonic

from langchain_core.embeddings import Embeddings
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pymilvus import MilvusClient

from ticketmind.agent.proposals import Proposal
from ticketmind.agent.retrieve import build_retrieval_query, retrieve_ticket
from ticketmind.agent.state import RetrievalUpdate, TicketAgentState


def build_ticket_graph(
    *,
    embeddings: Embeddings | None,
    client: MilvusClient,
    top_k: int,
    timeout: float,
    decision_fn: Callable[[TicketAgentState], Proposal] | None = None,
    retrieval_timeout_fn: Callable[[], float] | None = None,
    retrieval_audit: list | None = None,
    retrieval_fn: Callable | None = None,
) -> CompiledStateGraph:
    """Minimal business graph: retrieve evidence, then run the bounded decision loop."""

    def retrieve_node(state: TicketAgentState) -> RetrievalUpdate:
        query = build_retrieval_query(subject=state["subject"], messages=state["messages"])
        record = {
            "tool": "search_cases",
            "parameters": {"query": query},
            "reason": "首次检索客户明确提供的工单事实",
            "status": "failed",
            "result_source_ids": [],
        }
        started = monotonic()
        if retrieval_audit is not None:
            retrieval_audit.append(record)
        try:
            if retrieval_fn:
                retrieval = retrieval_fn(state, record)
            else:
                if embeddings is None:
                    raise ValueError("Dense retrieval requires embeddings")
                retrieval = retrieve_ticket(
                    state=state,
                    embeddings=embeddings,
                    client=client,
                    top_k=top_k,
                    timeout=retrieval_timeout_fn if retrieval_timeout_fn else timeout,
                )
            record.update(
                status="succeeded",
                result_source_ids=[hit.source_id for hit in retrieval["retrieval_hits"]],
                result_summary=f"返回 {len(retrieval['retrieval_hits'])} 条候选",
            )
            return retrieval
        except Exception as exc:
            record["error"] = "tool_execution_failed"
            if hasattr(exc, "code"):
                record["retrieval_error"] = exc.code
            raise
        finally:
            record["duration_ms"] = round((monotonic() - started) * 1000)

    builder = StateGraph(TicketAgentState)
    builder.add_node("retrieve", retrieve_node)
    builder.add_edge(START, "retrieve")
    if decision_fn is None:
        builder.add_edge("retrieve", END)
    else:
        builder.add_node("decide", lambda state: {"proposal": decision_fn(state)})
        builder.add_edge("retrieve", "decide")
        builder.add_edge("decide", END)
    return builder.compile()
