from langchain_core.embeddings import Embeddings
from pymilvus import MilvusClient
from ticketmind.agent.state import RetrievalUpdate, TicketAgentState,UnderstandingUpdate
from ticketmind.agent.retrieve import retrieve_ticket
from ticketmind.core.config import QwenSettings
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from ticketmind.agent.understand import understand_ticket
from collections.abc import Callable
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.agent.proposals import Proposal
def build_ticket_graph(
        settings:QwenSettings,
        *,
        embeddings:Embeddings,
        client:MilvusClient,
        top_k:int,
        timeout:float,
        understanding_fn:Callable[..., TicketUnderstanding]=understand_ticket,
        decision_fn:Callable[[TicketAgentState], Proposal] | None=None,
        retrieval_timeout_fn:Callable[[], float] | None=None,
) ->CompiledStateGraph:
    """业务图默认直接理解新工单；开发缓存通过可选适配器注入，客户端由调用方管理。"""
    def understand_node(
            state:TicketAgentState
    )->UnderstandingUpdate:
        understanding=understanding_fn(
            settings=settings,
            subject=state["subject"],
            body=state["body"],
        )
        return {"understanding":understanding}
    def retrieve_node(state:TicketAgentState)->RetrievalUpdate:
        retrieval=retrieve_ticket(
            state=state,
            embeddings=embeddings,
            client=client,
            top_k=top_k,
            timeout=retrieval_timeout_fn if retrieval_timeout_fn else timeout
        )
        return retrieval
    builder=StateGraph(TicketAgentState)
    builder.add_node( "understand",understand_node)
    builder.add_node("retrieve",retrieve_node)
    builder.add_edge(START,"understand")
    builder.add_edge("understand","retrieve")
    if decision_fn is None:
        builder.add_edge("retrieve",END)
    else:
        builder.add_node("decide", lambda state: {"proposal": decision_fn(state)})
        builder.add_edge("retrieve", "decide")
        builder.add_edge("decide", END)
    return builder.compile()
