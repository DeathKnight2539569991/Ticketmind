"""Only two read-only tools. No dynamic names, expressions, paths or write tools."""
from time import monotonic

from ticketmind.agent.policy import escalation, input_risks, validate_query
from ticketmind.agent.proposals import decision_adapter, validate_proposal
from ticketmind.retrieval.dense import search_case_vectors


def bounded_decision(state, *, decide, embeddings, client, corpus, config, remaining, audit, retrieval_timeout=None):
    state = dict(state)
    state.update(case_details={}, tool_calls=audit, search_rounds=1, agent_steps=2)
    state["execution_limits"] = config.model_dump(include={
        "max_search_rounds", "max_case_details", "max_agent_steps", "max_clarification_rounds"})
    seen_queries = {state["retrieval_query"].strip().casefold()}
    details = set()
    risks = input_risks(state["subject"] + "\n" + state["body"])
    if risks:
        return escalation("输入触发人工处理风险规则", risks=risks), state["retrieval_hits"]
    while state["agent_steps"] < config.max_agent_steps:
        remaining()
        state["agent_steps"] += 1
        decision = decision_adapter.validate_python(decide(state))
        if decision.next_step not in ("search_cases", "get_case_detail"):
            validate_proposal(decision, {hit.source_id for hit in state["retrieval_hits"]})
            if decision.next_step == "ask_clarification" and state.get("clarification_rounds", 0) >= config.max_clarification_rounds:
                decision = escalation("已达到主动澄清轮数上限")
            return decision, state["retrieval_hits"]
        params = {"query": decision.query} if decision.next_step == "search_cases" else {"source_id": decision.source_id}
        record = {"tool": decision.next_step, "parameters": params, "reason": decision.reason,
                  "status": "rejected", "duration_ms": 0, "result_source_ids": []}
        audit.append(record)
        if decision.next_step == "search_cases":
            record["missing_evidence"] = decision.missing_evidence
        if state["agent_steps"] >= config.max_agent_steps - 1:
            record["error"] = "agent_step_limit"
            return escalation("Agent 执行步数不足以继续查询和决策"), state["retrieval_hits"]
        if decision.next_step == "search_cases":
            if state["search_rounds"] >= config.max_search_rounds or decision.query.strip().casefold() in seen_queries:
                record["error"] = "search_limit_or_duplicate"
                return escalation("检索次数已达上限或查询重复"), state["retrieval_hits"]
            try:
                validate_query(decision.query, state)
            except ValueError:
                record["error"] = "invented_query_facts"
                return escalation("查询包含客户未提供的错误码或版本"), state["retrieval_hits"]
        elif decision.source_id not in {hit.source_id for hit in state["retrieval_hits"]}:
            record["error"] = "unknown_candidate"
            return escalation("详情查询来源不属于已检索候选"), state["retrieval_hits"]
        elif decision.source_id in details or len(details) >= config.max_case_details:
            record["error"] = "detail_limit_or_duplicate"
            return escalation("详情读取次数已达上限或来源重复"), state["retrieval_hits"]
        state["agent_steps"] += 1
        started = monotonic()
        try:
            remaining()
            if decision.next_step == "search_cases":
                vector = embeddings.embed_query(decision.query)
                hits = search_case_vectors(client, vector, top_k=config.retrieval_top_k,
                    timeout=min(remaining(), retrieval_timeout() if retrieval_timeout else remaining()))
                record["result_evidence"] = corpus.evidence(hits)
                seen_queries.add(decision.query.strip().casefold())
                state["search_rounds"] += 1
                merged = {hit.source_id: hit for hit in state["retrieval_hits"]}
                merged.update({hit.source_id: hit for hit in hits})
                state["retrieval_hits"] = list(merged.values())
                record["result_source_ids"] = [hit.source_id for hit in hits]
                record["result_summary"] = f"返回 {len(hits)} 条候选"
            else:
                case = corpus.cases[decision.source_id]
                state["case_details"][decision.source_id] = case.model_dump(mode="json")
                details.add(decision.source_id)
                record["result_source_ids"] = [decision.source_id]
                record["result_summary"] = "读取本次版本的完整合成案例"
            remaining()
            record["status"] = "succeeded"
        except Exception:
            record["status"], record["error"] = "failed", "tool_execution_failed"
            raise
        finally:
            record["duration_ms"] = round((monotonic() - started) * 1000)
    return escalation("Agent 执行步数达到上限"), state["retrieval_hits"]
