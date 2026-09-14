from langchain_core.embeddings import Embeddings
from collections.abc import Callable
from pymilvus import MilvusClient
from ticketmind.agent.state import RetrievalUpdate,TicketAgentState
from ticketmind.retrieval.dense import search_case_vectors
def build_retrieval_query(*,subject:str,body:str)->str:
     return f"标题：{subject}\n\n问题描述：{body}"
def retrieve_ticket(
        state:TicketAgentState,
        *,
        embeddings:Embeddings,
        client:MilvusClient,
        top_k:int,
        timeout:float | Callable[[], float],
)->RetrievalUpdate:
    query=build_retrieval_query(
        subject=state["subject"],body=state["body"]
        )
    query_vector=embeddings.embed_query(query)
    hits=search_case_vectors(client=client,query_vectors=query_vector,top_k=top_k,
                            timeout=timeout() if callable(timeout) else timeout)
    return {
        "retrieval_query":query,
        "retrieval_hits":hits
    }
