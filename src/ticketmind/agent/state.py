from typing import Any, NotRequired, TypedDict
import math

from ticketmind.agent.proposals import Proposal, decision_adapter, proposal_adapter
from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.semantic_judge import validate_judgment
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.retrieval.schemas import DocEvidenceHit, EvidenceHit, KnowledgeEvidenceHit


class ExecutionLimits(TypedDict):
    max_search_rounds: int
    max_docs_search_rounds: int
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
    docs_hits: NotRequired[list[DocEvidenceHit]]
    docs_search_rounds: NotRequired[int]
    search_rounds: NotRequired[int]
    agent_steps: NotRequired[int]
    decision_rounds: NotRequired[int]
    step_limit_reached: NotRequired[bool]
    execution_limits: NotRequired[ExecutionLimits]
    guardrail_feedback: NotRequired[dict[str, Any]]
    seen_queries: NotRequired[list[str]]
    seen_docs_queries: NotRequired[list[str]]
    repair_attempt: NotRequired[int]
    decision_result: NotRequired[dict[str, Any]]
    candidate_proposal: NotRequired[dict[str, Any]]
    judge_result: NotRequired[dict[str, Any]]
    usage: NotRequired[dict[str, Any]]
    evidence: NotRequired[list[dict[str, Any]]]
    compute_elapsed_seconds: NotRequired[float]
    state_version: NotRequired[int]


class RetrievalUpdate(TypedDict):
    retrieval_query: str
    retrieval_hits: list[RetrievalHit | EvidenceHit]


class DurableAgentState(TypedDict, total=False):
    """Versioned JSON snapshot; typed models are temporary capability inputs only."""
    state_version: int
    subject: str
    messages: list[dict[str, Any]]
    clarification_rounds: int
    tool_calls: list[dict[str, Any]]
    retrieval_query: str
    retrieval_hits: list[dict[str, Any]]
    docs_hits: list[dict[str, Any]]
    docs_search_rounds: int
    search_rounds: int
    agent_steps: int
    decision_rounds: int
    step_limit_reached: bool
    execution_limits: ExecutionLimits
    seen_queries: list[str]
    seen_docs_queries: list[str]
    repair_attempt: int
    decision_result: dict[str, Any]
    candidate_proposal: dict[str, Any]
    judge_result: dict[str, Any]
    guardrail_feedback: dict[str, Any]
    proposal: dict[str, Any]
    evidence: list[dict[str, Any]]
    usage: dict[str, Any]
    compute_elapsed_seconds: float


def customer_fact_text(state: TicketAgentState) -> str:
    """Canonical deterministic text source for facts explicitly supplied by the customer."""
    parts = [state["subject"]]
    parts.extend(message.content for message in state["messages"] if message.role == "customer")
    return "\n".join(parts)


def copy_data(value: Any) -> Any:
    """Copy JSON data, rejecting runtime resources and non-finite numbers."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [copy_data(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: copy_data(item) for key, item in value.items()}
    raise ValueError(f"Agent durable state 不支持 {type(value).__name__}")


def durable_state(state: dict) -> DurableAgentState:
    """Serialize only known typed fields; everything else must already be JSON data.

    This is a replace snapshot, not an additive reducer. Neither direction shares
    mutable values with its caller. Runtime clients/timers never enter this boundary.
    """
    values = dict(state)
    values.setdefault("state_version", 2)
    if type(values["state_version"]) is not int or values["state_version"] != 2:
        raise ValueError("不支持的 Agent state version")
    for key in ("messages", "retrieval_hits", "docs_hits"):
        if key in values:
            values[key] = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                           for item in values[key]]
    for key in ("proposal", "decision_result", "candidate_proposal", "judge_result"):
        if key in values and hasattr(values[key], "model_dump"):
            values[key] = values[key].model_dump(mode="json")
    data = copy_data(values)
    # Validate typed contracts before treating a snapshot as durable data.
    typed_state(data)
    return data


def typed_state(data: dict) -> TicketAgentState:
    """Create a temporary validated projection, preserving knowledge hit extensions."""
    values = copy_data(data)
    if {"case_details", "detail_ids"} & set(values):
        raise ValueError("旧详情状态不属于 Agent v2")
    if type(values.get("state_version", 2)) is not int or values.get("state_version", 2) != 2:
        raise ValueError("不支持的 Agent state version")
    for key in ("clarification_rounds", "search_rounds", "docs_search_rounds", "decision_rounds", "agent_steps", "repair_attempt"):
        if key in values and (type(values[key]) is not int or values[key] < 0):
            raise ValueError(f"{key} 必须是非负整数")
    if "repair_attempt" in values and values["repair_attempt"] > 1:
        raise ValueError("最多允许一次 repair")
    if "compute_elapsed_seconds" in values:
        elapsed = values["compute_elapsed_seconds"]
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("累计计算耗时必须是非负有限数值")
        for key in ("compute_observed_seconds", "compute_estimated_seconds"):
            amount = values.get(key, 0)
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                raise ValueError("预算分类必须是非负有限数值")
        if "compute_estimated_seconds" in values and not math.isclose(elapsed,
                values.get("compute_observed_seconds", 0) + values["compute_estimated_seconds"]):
            raise ValueError("预算分类与累计耗时不一致")
    for key in ("seen_queries", "seen_docs_queries"):
        if key in values and (type(values[key]) is not list or
                              any(type(item) is not str for item in values[key]) or
                              len(values[key]) != len(set(values[key]))):
            raise ValueError(f"{key} 必须是无重复的字符串列表")
    values["messages"] = [AgentMessage.model_validate(item) for item in values["messages"]]
    if "retrieval_hits" in values:
        values["retrieval_hits"] = [
            (KnowledgeEvidenceHit if "knowledge_revision" in item else
             EvidenceHit if "retrieval_mode" in item else RetrievalHit).model_validate(item)
            for item in values["retrieval_hits"]
        ]
    if "docs_hits" in values:
        values["docs_hits"] = [DocEvidenceHit.model_validate(item) for item in values["docs_hits"]]
        if any(hit.source_id != "docs:" + hit.chunk_id for hit in values["docs_hits"]):
            raise ValueError("Docs 来源与 chunk 不一致")
    if "proposal" in values:
        values["proposal"] = proposal_adapter.validate_python(values["proposal"])
    if "candidate_proposal" in values:
        proposal_adapter.validate_python(values["candidate_proposal"])
    if "decision_result" in values:
        decision_adapter.validate_python(values["decision_result"])
    if "judge_result" in values:
        if "candidate_proposal" not in values:
            raise ValueError("Judge result 缺少对应提案")
        validate_judgment(values["judge_result"],
                          proposal_adapter.validate_python(values["candidate_proposal"]))
    return values
