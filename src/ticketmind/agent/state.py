from typing import NotRequired,TypedDict
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.retrieval.schemas import EvidenceHit
from ticketmind.agent.proposals import Proposal
class TicketAgentState(TypedDict):
    proposal: NotRequired[Proposal]
    understanding: NotRequired[TicketUnderstanding]
    subject:str
    body:str
    retrieval_query:NotRequired[str]
    retrieval_hits:NotRequired[list[RetrievalHit | EvidenceHit]]
class UnderstandingUpdate(TypedDict):
    understanding: TicketUnderstanding
class RetrievalUpdate(TypedDict):
    retrieval_query:str
    retrieval_hits:list[RetrievalHit | EvidenceHit]
