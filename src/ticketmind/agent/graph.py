from langchain_core.embeddings import Embeddings
from pymilvus import MilvusClient
from ticketmind.agent.state import RetrievalUpdate, TicketAgentState,UnderstandingUpdate
from ticketmind.agent.retrieve import retrieve_ticket
from ticketmind.core.config import QwenSettings
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from ticketmind.agent.understand import understand_ticket
from pathlib import Path
from ticketmind.agent.run_cache import UnderstandingCache, understanding_fingerprint,load_cache,save_cache
def build_ticket_graph(
        settings:QwenSettings,
        *,
        embeddings:Embeddings,
        client:MilvusClient,
        top_k:int,
        timeout:float,
        understanding_cache_path:Path,
        allow_understanding:bool=False
) ->CompiledStateGraph:
    def understand_node(
            state:TicketAgentState
    ):
        fingerprint = understanding_fingerprint(
            settings=settings, subject=state["subject"], body=state["body"],
        )
        cache = load_cache(
            understanding_cache_path,
            UnderstandingCache,
            expected_fingerprint=fingerprint,
        )
        if cache is not None:
            return {"understanding": cache.understanding}
        if not allow_understanding:
            raise RuntimeError("理解缓存不存在，当前运行未开启理解模型调用")
        understanding=understand_ticket(
            settings=settings,
            subject=state["subject"],
            body=state["body"],
        )
        return {"understanding":understanding}
    def retrieve_node(state:TicketAgentState):
        retrieval=retrieve_ticket(
            state=state,
            embeddings=embeddings,
            client=client,
            top_k=top_k,
            timeout=timeout
        )
        return retrieval
    builder=StateGraph(TicketAgentState)
    builder.add_node( "understand",understand_node)
    builder.add_node("retrieve",retrieve_node)
    builder.add_edge(START,"understand")
    builder.add_edge("understand","retrieve")
    builder.add_edge("retrieve",END)
    return builder.compile()
    