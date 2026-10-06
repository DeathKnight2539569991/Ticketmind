"""Only two read-only tools. No dynamic names, expressions, paths or write tools."""
from time import monotonic

from ticketmind.agent.policy import escalation, validate_query
from ticketmind.agent.proposals import decision_adapter, proposal_adapter, validate_proposal
from ticketmind.agent.semantic_judge import GuardrailFailure, validate_judgment
from ticketmind.agent.state import durable_state, typed_state


def normalize_decision(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict) and value.get("next_step") == "escalate":
        value = {key: item for key, item in value.items() if key != "risk_flags"}
    return decision_adapter.validate_python(value)


def normalize_proposal(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    proposal = proposal_adapter.validate_python(value)
    return proposal


def judge_candidate(state, proposal, *, judge, remaining):
    """Validate one candidate and judge it; only passed candidates become final."""
    remaining()
    proposal = normalize_proposal(proposal)
    validate_proposal(proposal, {hit.source_id for hit in state["retrieval_hits"]})
    state.pop("judge_result", None)
    state["candidate_proposal"] = proposal.model_dump(mode="json")
    result = validate_judgment(judge(typed_state(durable_state(state)), proposal), proposal)
    state["judge_result"] = result.model_dump(mode="json")
    remaining()
    if not result.passed:
        if state["repair_attempt"] == 1:
            raise GuardrailFailure()
        state["guardrail_feedback"] = {
            "proposal": proposal.model_dump(),
            "violations": [violation.model_dump() for violation in result.violations],
        }
    return proposal, result


def repair_candidate(state, *, repair, config, remaining):
    """One final-only regeneration, using the original step and time budgets."""
    remaining()
    if state["repair_attempt"] != 0:
        raise GuardrailFailure()
    if state["agent_steps"] >= config.max_agent_steps:
        raise GuardrailFailure("guardrail_step_limit")
    state["agent_steps"] += 1
    state["repair_attempt"] = 1
    # The rejected judgment refers to the old candidate, never the replacement.
    state.pop("judge_result", None)
    try:
        proposal = normalize_proposal(repair(typed_state(durable_state(state))))
        remaining()
        validate_proposal(proposal, {hit.source_id for hit in state["retrieval_hits"]})
        if (proposal.next_step == "ask_clarification" and
                state.get("clarification_rounds", 0) >= config.max_clarification_rounds):
            raise ValueError("重生成不得绕过澄清轮数限制")
    except Exception as exc:
        raise GuardrailFailure("guardrail_repair_failed") from exc
    state["candidate_proposal"] = proposal.model_dump(mode="json")
    return proposal


def decision_turn(state, *, decide, config, remaining):
    """One model turn; tools and final-proposal validation belong to later nodes."""
    remaining()
    if state["agent_steps"] >= config.max_agent_steps:
        decision = normalize_decision(escalation("Agent 执行步数达到上限"))
        state["decision_result"] = decision.model_dump(mode="json")
        return decision
    state["agent_steps"] += 1
    decision = normalize_decision(decide(typed_state(durable_state(state))))
    state["decision_result"] = decision.model_dump(mode="json")
    return decision


def execute_decision_tool(state, decision, *, corpus, config, remaining, search_fn, detail_fn=None):
    """Shared guard/execution boundary. Return a deterministic proposal on rejection."""
    audit = state["tool_calls"]
    params = {"query": decision.query} if decision.next_step == "search_cases" else {"source_id": decision.source_id}
    record = {"tool": decision.next_step, "parameters": params, "reason": decision.reason,
              "status": "rejected", "duration_ms": 0, "result_source_ids": []}
    audit.append(record)
    if state["agent_steps"] >= config.max_agent_steps - 1:
        record["error"] = "agent_step_limit"
        return escalation("Agent 执行步数不足以继续查询和决策")
    if decision.next_step == "search_cases":
        if state["search_rounds"] >= config.max_search_rounds or decision.query.strip().casefold() in state["seen_queries"]:
            record["error"] = "search_limit_or_duplicate"
            return escalation("检索次数已达上限或查询重复")
        try:
            validate_query(decision.query, state)
        except ValueError:
            record["error"] = "invented_query_facts"
            return escalation("查询包含客户未提供的错误码或版本")
    elif decision.source_id not in {hit.source_id for hit in state["retrieval_hits"]}:
        record["error"] = "unknown_candidate"
        return escalation("详情查询来源不属于已检索候选")
    elif decision.source_id in state["detail_ids"] or len(state["detail_ids"]) >= config.max_case_details:
        record["error"] = "detail_limit_or_duplicate"
        return escalation("详情读取次数已达上限或来源重复")
    state["agent_steps"] += 1
    started = monotonic()
    try:
        remaining()
        if decision.next_step == "search_cases":
            hits = search_fn(decision.query, record)
            state["seen_queries"].append(decision.query.strip().casefold())
            state["search_rounds"] += 1
            merged = {hit.source_id: hit for hit in state["retrieval_hits"]}
            merged.update({hit.source_id: hit for hit in hits})
            state["retrieval_hits"] = list(merged.values())
            record["result_source_ids"] = [hit.source_id for hit in hits]
            record["result_summary"] = f"返回 {len(hits)} 条候选"
        else:
            state["case_details"][decision.source_id] = (detail_fn or corpus.get_case_detail)(decision.source_id)
            state["detail_ids"].append(decision.source_id)
            record["result_source_ids"] = [decision.source_id]
            from ticketmind.knowledge.repository import KnowledgeStore
            record["result_summary"] = ("读取本次版本的完整案例" if isinstance(corpus, KnowledgeStore)
                                        else "读取本次版本的完整合成案例")
        remaining()
        record["status"] = "succeeded"
    except Exception as exc:
        record["status"], record["error"] = "failed", "tool_execution_failed"
        if hasattr(exc, "code"):
            record["retrieval_error"] = exc.code
        raise
    finally:
        record["duration_ms"] = round((monotonic() - started) * 1000)
    return None


def final_candidate(state, proposal, config):
    if proposal.next_step == "ask_clarification" and state.get("clarification_rounds", 0) >= config.max_clarification_rounds:
        return escalation("已达到主动澄清轮数上限")
    return proposal
