from typing import Any, NotRequired, TypedDict

from ticketmind.agent.proposals import Proposal
from ticketmind.agent.schemas import AgentMessage
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.retrieval.schemas import EvidenceHit


class ExecutionLimits(TypedDict):
    max_search_rounds: int
    max_case_details: int
    max_agent_steps: int
    max_clarification_rounds: int


class ToolCallRecord(TypedDict, total=False):
    tool: str
    parameters: dict[str, Any]
    reason: str
    status: str
    duration_ms: int
    result_source_ids: list[str]
    result_summary: str
    result_hits: list[dict[str, Any]]
    error: str
    retrieval_error: str
    retrieval_mode: str
    collection: str
    candidate_k: int
    top_k: int
    rrf_k: int
    channels: dict[str, Any]


class TicketAgentState(TypedDict):
    subject: str
    messages: list[AgentMessage]
    clarification_rounds: int
    tool_calls: list[ToolCallRecord]

    proposal: NotRequired[Proposal]
    retrieval_query: NotRequired[str]
    retrieval_hits: NotRequired[list[RetrievalHit | EvidenceHit]]
    case_details: NotRequired[dict[str, dict[str, Any]]]
    search_rounds: NotRequired[int]
    agent_steps: NotRequired[int]
    execution_limits: NotRequired[ExecutionLimits]
    guardrail_feedback: NotRequired[dict[str, Any]]


class RetrievalUpdate(TypedDict):
    retrieval_query: str
    retrieval_hits: list[RetrievalHit | EvidenceHit]


def customer_fact_text(state: TicketAgentState) -> str:
    """Canonical deterministic text source for facts explicitly supplied by the customer."""
    parts = [state["subject"]]
    parts.extend(message.content for message in state["messages"] if message.role == "customer")
    return "\n".join(parts)
