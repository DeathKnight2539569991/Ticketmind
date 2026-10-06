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
    validate_proposal(proposal, evidence_ids(state))
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
        return step_limit_proposal(state)
    state["agent_steps"] += 1
    state["repair_attempt"] = 1
    # The rejected judgment refers to the old candidate, never the replacement.
    state.pop("judge_result", None)
    try:
        proposal = normalize_proposal(repair(typed_state(durable_state(state))))
        remaining()
        validate_proposal(proposal, evidence_ids(state))
        if (proposal.next_step == "ask_clarification" and
                state.get("clarification_rounds", 0) >= config.max_clarification_rounds):
            raise ValueError("重生成不得绕过澄清轮数限制")
    except Exception as exc:
        raise GuardrailFailure("guardrail_repair_failed") from exc
    state["candidate_proposal"] = proposal.model_dump(mode="json")
    return proposal


def evidence_ids(state):
    return {hit.source_id for hit in state.get("retrieval_hits", []) + state.get("docs_hits", [])}


def step_limit_proposal(state):
    # The program-generated handoff replaces any failed model candidate.
    for key in ("candidate_proposal", "judge_result", "guardrail_feedback"):
        state.pop(key, None)
    proposal = escalation("Agent 执行步数达到上限")
    state["decision_result"] = normalize_decision(proposal).model_dump(mode="json")
    state["proposal"] = proposal
    state["step_limit_reached"] = True
    return proposal


def decision_turn(state, *, decide, config, remaining):
    """One model turn; exhaustion is a fixed, reviewable proposal with no Judge."""
    remaining()
    if state["agent_steps"] >= config.max_agent_steps:
        return step_limit_proposal(state)
    state["agent_steps"] += 1
    state["decision_rounds"] += 1
    decision = normalize_decision(decide(typed_state(durable_state(state))))
    state["decision_result"] = decision.model_dump(mode="json")
    if state["agent_steps"] >= config.max_agent_steps and decision.next_step in ("search_cases", "search_docs"):
        return step_limit_proposal(state)
    return decision


def execute_decision_tool(state, decision, *, config, remaining, search_fn, docs_fn):
    """Every admitted tool attempt costs a step; guard rejection returns an observation."""
    remaining()
    if state["agent_steps"] >= config.max_agent_steps:
        raise ValueError("工具节点缺少执行步数")
    state["agent_steps"] += 1
    tool = decision.next_step
    record = {"tool": tool, "parameters": {"query": decision.query}, "reason": decision.reason,
              "status": "rejected", "duration_ms": 0, "result_source_ids": []}
    state["tool_calls"].append(record)
    rounds, queries, hits_key, limit, fn = (
        ("search_rounds", "seen_queries", "retrieval_hits", config.max_search_rounds, search_fn)
        if tool == "search_cases" else
        ("docs_search_rounds", "seen_docs_queries", "docs_hits", config.max_docs_search_rounds, docs_fn))
    normalized = decision.query.strip().casefold()
    if normalized in state[queries]:
        record["error"] = "duplicate_query"
        return
    if state[rounds] >= limit:
        record["error"] = "search_limit"
        return
    try:
        validate_query(decision.query, state)
    except ValueError:
        record["error"] = "invented_query_facts"
        return
    started = monotonic()
    try:
        hits = fn(decision.query, record)
        remaining()
        state[queries].append(normalized)
        state[rounds] += 1
        merged = {hit.source_id: hit for hit in state[hits_key]}
        merged.update({hit.source_id: hit for hit in hits})
        state[hits_key] = list(merged.values())
        record.update(status="succeeded", result_source_ids=[hit.source_id for hit in hits],
                      result_summary=f"返回 {len(hits)} 条候选")
    except Exception as exc:
        record.update(status="failed", error="tool_execution_failed")
        if hasattr(exc, "code"):
            record["retrieval_error"] = exc.code
        raise
    finally:
        record["duration_ms"] = round((monotonic() - started) * 1000)


def final_candidate(state, proposal, config):
    if proposal.next_step == "ask_clarification" and state.get("clarification_rounds", 0) >= config.max_clarification_rounds:
        return escalation("已达到主动澄清轮数上限")
    return proposal
