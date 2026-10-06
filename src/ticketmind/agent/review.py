"""Durable orchestration. Nodes have no ORM writes; review resume never calls a model."""
from typing import TypedDict
from dataclasses import dataclass
from langgraph.runtime import Runtime
import math
import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ticketmind.agent.proposals import proposal_adapter, validate_proposal
from ticketmind.agent.runtime import AgentRunner, RunOutput, RunFailure, fatal_failure
from ticketmind.agent.semantic_judge import validate_judgment
from ticketmind.agent.state import copy_data, durable_state
from ticketmind.agent.schemas import AgentRunInput
from ticketmind.agent.tracing import trace_for

logger = logging.getLogger(__name__)


def _finish_trace(trace, *, outcome, error=None):
    if trace is not None:
        try:
            trace.finish(outcome=outcome, error=error)
        except Exception:
            pass


def has_reported_tokens(value):
    if isinstance(value, dict):
        return any((key in {"total_tokens", "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"}
                    and type(item) in (int, float)) or has_reported_tokens(item) for key, item in value.items())
    return isinstance(value, list) and any(has_reported_tokens(item) for item in value)


@dataclass
class WorkflowContext:
    runner: object = None
    failures: list | None = None
    trace: object = None


class ReviewState(TypedDict, total=False):
    snapshot: dict
    output: dict
    approved_review: dict
    agent_data: dict
    failure_descriptor: dict
    failure_observation: dict
    failed_attempts: list
    resume_plan: dict
    adapter_output_only: bool


def review_node(state, runtime: Runtime[WorkflowContext]):
    trace = getattr(runtime.context, "trace", None)
    if trace is not None:
        try:
            trace.event("ticketmind.review.interrupt", node="review",
                        outputs={"run_id": state["snapshot"]["run_id"],
                                 "proposal": state["output"]["state"]["proposal"]})
        except Exception:
            pass
    review = interrupt(
        {
            "run_id": state["snapshot"]["run_id"],
            "proposal": state["output"]["state"]["proposal"],
        }
    )
    if trace is not None:
        try:
            trace.event("ticketmind.review.resumed", node="review",
                        outputs={"run_id": state["snapshot"]["run_id"], "review": review})
        except Exception:
            pass
    return {"approved_review": review}


def agent_input_from_snapshot(snapshot: dict) -> AgentRunInput:
    """Project the durable run snapshot onto the minimal business input seen by the Agent."""
    return AgentRunInput.model_validate(
        {
            "subject": snapshot["subject"],
            "messages": [
                {
                    "role": "customer" if message["author_type"] == "customer" else "support",
                    "content": message["body"],
                }
                for message in snapshot["messages"]
            ],
        }
    )


class ReviewWorkflow:
    def __init__(self, checkpointer):
        self.checkpointer = checkpointer
        self._graph = self._build_graph()

    def graph(self):
        return self._graph

    def _build_graph(self):
        def callable_output(state, runner):
            if runner is None:
                raise RuntimeError("审核恢复禁止重新计算 Agent")
            snapshot = state["snapshot"]
            output = runner(
                agent_input_from_snapshot(snapshot),
                clarification_rounds=snapshot.get("clarification_rounds", 0),
            )
            proposal = proposal_adapter.validate_python(output.state["proposal"])
            validate_proposal(proposal, {hit["source_id"] for hit in output.evidence})
            return {
                "output": {
                    "state": {
                        "proposal": proposal.model_dump(mode="json"),
                        "tool_calls": output.state.get("tool_calls", []),
                    },
                    "evidence": output.evidence,
                    "usage": output.usage,
                }
            }

        def execute_node(state, node, context):
            runner, failures = context.runner, context.failures
            if node == "bootstrap_retrieve" and runner is not None and not isinstance(runner, AgentRunner):
                return callable_output(state, runner)
            if not isinstance(runner, AgentRunner):
                raise RuntimeError("计算恢复缺少 Agent runtime")
            execution = None
            try:
                if "agent_data" in state:
                    execution = runner.new_execution(data=state["agent_data"])
                else:
                    snapshot = state["snapshot"]
                    execution = runner.new_execution(agent_input_from_snapshot(snapshot),
                                               snapshot.get("clarification_rounds", 0))
                execution.trace = context.trace
                data = execution.run_node(node)
                if state.get("failed_attempts"):
                    data["usage"]["failed_attempts"] = copy_data(state["failed_attempts"])
                update = {"agent_data": data, "failure_observation": None, "resume_plan": None}
                if node in ("judge", "decision", "repair") and "proposal" in data:
                    proposal = proposal_adapter.validate_python(data["proposal"])
                    validate_proposal(proposal, {hit["source_id"] for hit in data["evidence"]})
                    update["output"] = {"state": {"proposal": data["proposal"],
                                                   "tool_calls": data["tool_calls"]},
                                        "evidence": data["evidence"], "usage": data["usage"]}
                return update
            except Exception as original:
                if isinstance(original, RunFailure):
                    exc = original
                else:
                    exc = RunFailure(execution.stage if execution else "initialization",
                                     execution.partial if execution else {},
                                     execution.evidence if execution else [],
                                     {**execution.usage, "execution_budget": {
                                         "observed_elapsed_seconds": execution.budget.stop() - execution.partial.get("compute_estimated_seconds", 0),
                                         "estimated_elapsed_seconds": execution.partial.get("compute_estimated_seconds", 0),
                                         "total_elapsed_seconds": execution.budget.elapsed_seconds}} if execution else {})
                    exc.__cause__ = original
                if not fatal_failure(exc) and exc.usage.get("execution_budget", {}).get("total_elapsed_seconds",
                        exc.usage.get("execution_budget", {}).get("observed_elapsed_seconds", 0)) < runner.config.processing_timeout_seconds:
                    raise exc
                try:
                    scope_usage = copy_data(exc.usage)
                except ValueError:
                    # Invalid adapter data must not prevent the terminal marker
                    # or the business failure write from being JSON serializable.
                    scope_usage = {"diagnostic": {"code": "invalid_usage_data"},
                                   "execution_budget": {
                                       "observed_elapsed_seconds": execution.budget.elapsed_seconds - execution.partial.get("compute_estimated_seconds", 0) if execution else 0,
                                       "estimated_elapsed_seconds": execution.partial.get("compute_estimated_seconds", 0) if execution else 0,
                                       "total_elapsed_seconds": execution.budget.elapsed_seconds if execution else 0,
                                       "estimates": copy_data(execution.partial.get("compute_estimates", [])) if execution else []}}
                    exc.usage = scope_usage
                if failures is not None:
                    failures.append(exc)
                # Persist terminal failure before the business result write. No
                # exception/client object enters the durable state.
                return {"failure_descriptor": {"fatal": True, "node": node,
                    "stage": exc.stage, "error_type": type(exc.__cause__).__name__,
                    "code": getattr(exc.__cause__, "code", "agent_execution_failed"),
                    "failure_scope_usage": scope_usage,
                    "observed_elapsed_seconds": exc.usage.get("execution_budget", {}).get(
                        "observed_elapsed_seconds", 0)}}

        builder = StateGraph(ReviewState, context_schema=WorkflowContext)
        def route(state, target):
            if state.get("failure_descriptor"):
                return END
            if (state.get("resume_plan") or {}).get("phase") == "prepared":
                return "resume_gate"
            return target
        builder.add_node("resume_gate", lambda state: {"resume_plan": {**state["resume_plan"], "phase": "started"}})
        builder.add_conditional_edges("resume_gate", lambda state: END if state.get("failure_descriptor") else
                                      state["resume_plan"]["node"], [END, "decision", "judge", "repair"])
        builder.add_node("review", review_node)
        def capability_node(node):
            def invoke(state, runtime: Runtime[WorkflowContext]):
                return execute_node(state, node, runtime.context or WorkflowContext())
            return invoke
        for node in ("bootstrap_retrieve", "decision", "search_cases", "search_docs", "judge", "repair"):
            builder.add_node(node, capability_node(node))

        def route_decision(state, config):
            if state.get("failure_descriptor"):
                return END
            if state["agent_data"].get("step_limit_reached"):
                return END if config.get("configurable", {}).get("standalone_compute", False) else "review"
            action = state["agent_data"]["decision_result"]["next_step"]
            return route(state, action if action in ("search_cases", "search_docs") else "judge")

        def route_tool(state):
            if state.get("failure_descriptor"):
                return END
            return route(state, "decision")
        # Callable adapters supply output at the entry boundary; no compute resume.
        builder.add_edge(START, "bootstrap_retrieve")
        builder.add_conditional_edges("bootstrap_retrieve", lambda state: route(state, "review" if "output" in state else "decision"), [END, "decision", "review", "resume_gate"])
        builder.add_conditional_edges("decision", route_decision,
                                      [END, "search_cases", "search_docs", "judge", "review", "resume_gate"])
        for node in ("search_cases", "search_docs"):
            builder.add_conditional_edges(node, route_tool, [END, "decision", "resume_gate"])
        def route_judge(state, config):
            if state.get("failure_descriptor"):
                return END
            target = ("review" if not config.get("configurable", {}).get("standalone_compute", False) else END) if state["agent_data"]["judge_result"]["passed"] else "repair"
            return route(state, target)
        builder.add_conditional_edges("judge", route_judge, [END, "review", "repair", "resume_gate"])
        def route_repair(state, config):
            if state.get("failure_descriptor"):
                return END
            if state["agent_data"].get("step_limit_reached"):
                return END if config.get("configurable", {}).get("standalone_compute", False) else "review"
            return route(state, "judge")
        builder.add_conditional_edges("repair", route_repair, [END, "judge", "review", "resume_gate"])
        builder.add_edge("review", END)
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def config(thread_id):
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _validated_output(output):
        evidence = output["evidence"]
        proposal = proposal_adapter.validate_python(output["state"]["proposal"])
        validate_proposal(proposal, {hit["source_id"] for hit in evidence})
        state = {**output["state"], "proposal": proposal}
        return RunOutput(state, evidence, output["usage"])

    def start(self, snapshot, thread_id, runner):
        failures = []
        graph = self.graph()
        value = {"snapshot": snapshot}
        if not isinstance(runner, AgentRunner):
            value["adapter_output_only"] = True
        trace = trace_for(snapshot, thread_id, "start", runner=runner)
        try:
            result = self._invoke(graph, value, self.config(thread_id), context=WorkflowContext(runner, failures, trace))
            if failures:
                raise failures[0]
            if not result.get("__interrupt__"):
                raise RuntimeError("图未持久化审核中断")
            output = self._validated_output(result["output"])
            _finish_trace(trace, outcome="waiting_review")
            return output
        except Exception as exc:
            _finish_trace(trace, outcome="failed", error=type(exc).__name__)
            raise

    def compute(self, agent_input, runner, *, clarification_rounds=0):
        """Standalone compute uses this topology, ending after Judge."""
        if self.checkpointer is not None:
            raise ValueError("Standalone compute requires a workflow without a checkpointer")
        failures = []
        snapshot = {"run_id": "standalone", "subject": agent_input.subject,
                    "messages": [{"author_type": m.role, "body": m.content} for m in agent_input.messages],
                    "clarification_rounds": clarification_rounds}
        trace = trace_for(snapshot, "standalone:" + str(id(snapshot)), "compute", runner=runner)
        try:
            result = self._invoke(self.graph(), {"snapshot": snapshot}, {"configurable": {"standalone_compute": True}},
                                  context=WorkflowContext(runner, failures, trace))
            if failures:
                raise failures[0]
            from ticketmind.agent.state import typed_state
            data = result["agent_data"]
            _finish_trace(trace, outcome="computed")
            return RunOutput(typed_state(data), copy_data(data["evidence"]), copy_data(data["usage"]))
        except Exception as exc:
            _finish_trace(trace, outcome="failed", error=type(exc).__name__)
            raise

    def inspect_compute(self, thread_id):
        """Read-only validation of a durable computing checkpoint.

        Never start a new invocation for missing/legacy/corrupt checkpoints. Only
        unfinished nodes can replay; waiting review uses the existing review API.
        """
        graph, config = self.graph(), self.config(thread_id)
        checkpoint = graph.get_state(config)
        if checkpoint.values.get("adapter_output_only"):
            raise RuntimeError("Callable adapter checkpoints cannot resume compute")
        if checkpoint.next == ("resume_gate",):
            plan = checkpoint.values.get("resume_plan", {})
            if plan.get("phase") != "prepared" or plan.get("node") not in ("decision", "judge", "repair"):
                raise RuntimeError("非法恢复预算 gate")
            checkpoint = checkpoint._replace(next=(plan["node"],))
        if checkpoint.values.get("failure_descriptor"):
            raise RuntimeError("不可恢复的 Agent failure")
        if not checkpoint.values or checkpoint.next not in (("bootstrap_retrieve",), ("decision",), ("search_cases",),
                                                              ("search_docs",), ("judge",), ("repair",)):
            raise RuntimeError("检查点不在可恢复的计算位置")
        values = copy_data(checkpoint.values)
        if "agent_data" in values and values["agent_data"].get("state_version") != 2:
            raise RuntimeError("缺少新版本计算状态，禁止升级续算")
        snapshot = values.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("run_id"):
            raise RuntimeError("非法计算检查点 snapshot")
        frozen = snapshot.get("runtime_contract")
        if frozen is not None and (not isinstance(frozen, dict) or frozen.get("model_config", {}).get("state_version") != 2):
            raise RuntimeError("旧计算契约不得升级续算")
        agent_input = agent_input_from_snapshot(snapshot)
        if checkpoint.next != ("bootstrap_retrieve",):
            data = durable_state(values["agent_data"])
            if (not isinstance(data.get("tool_calls"), list) or
                    any(not isinstance(call, dict) or call.get("tool") not in ("search_cases", "search_docs") or
                        call.get("status") not in ("succeeded", "rejected") or
                        not isinstance(call.get("parameters"), dict) or set(call["parameters"]) != {"query"} or
                        not isinstance(call["parameters"]["query"], str) or not call["parameters"]["query"].strip() or
                        not isinstance(call.get("result_source_ids"), list) or
                        any(not isinstance(source, str) for source in call["result_source_ids"]) or
                        len(call["result_source_ids"]) != len(set(call["result_source_ids"]))
                        for call in data["tool_calls"]) or
                    not isinstance(data.get("evidence"), list) or
                    any(not isinstance(item, dict) or not isinstance(item.get("source_id"), str) for item in data["evidence"])):
                raise RuntimeError("非法计算检查点审计结构")
            limits = snapshot.get("execution_limits")
            if limits is not None and data.get("execution_limits") != {
                    key: limits[key] for key in ("max_search_rounds", "max_docs_search_rounds", "max_agent_steps",
                                                "max_clarification_rounds")}:
                raise RuntimeError("检查点 execution limits 与冻结快照不一致")
            if (data["subject"] != agent_input.subject or
                    data["messages"] != [m.model_dump(mode="json") for m in agent_input.messages] or
                    data.get("clarification_rounds") != snapshot.get("clarification_rounds", 0) or
                    not isinstance(data.get("retrieval_query"), str) or
                    "retrieval_hits" not in data or "compute_elapsed_seconds" not in data or
                    "evidence" not in data or "usage" not in data or
                    data.get("agent_steps", 0) < 1 or data.get("search_rounds", 0) < 1 or
                    "proposal" in data or "output" in values or data.get("step_limit_reached") or
                    data.get("repair_attempt") not in (0, 1) or
                    data["retrieval_query"].strip().casefold() not in data.get("seen_queries", []) or
                    not data.get("tool_calls") or data["tool_calls"][0].get("status") != "succeeded" or
                    data["tool_calls"][0]["tool"] != "search_cases" or
                    data["tool_calls"][0]["parameters"]["query"] != data["retrieval_query"]):
                raise RuntimeError("非法计算检查点 Agent data")
            if data["agent_steps"] != len(data["tool_calls"]) + data.get("decision_rounds", -1) + data["repair_attempt"]:
                raise RuntimeError("非法计算检查点 step accounting")
            source_ids = {hit["source_id"] for hit in data["retrieval_hits"]}
            docs_ids = {hit["source_id"] for hit in data.get("docs_hits", [])}
            if (data["search_rounds"] != len(data.get("seen_queries", [])) or
                    data.get("docs_search_rounds") != len(data.get("seen_docs_queries", [])) or
                    len(source_ids) != len(data["retrieval_hits"]) or
                    len(docs_ids) != len(data.get("docs_hits", [])) or source_ids & docs_ids or
                    {hit["source_id"] for hit in data["evidence"]} != source_ids | docs_ids or
                    len(data["evidence"]) != len(source_ids) + len(docs_ids) or
                    (limits is not None and (data["agent_steps"] > limits["max_agent_steps"] or
                        data["search_rounds"] > limits["max_search_rounds"] or
                        data["docs_search_rounds"] > limits["max_docs_search_rounds"]))):
                raise RuntimeError("非法计算检查点 counters 或工具结果")
            for tool, rounds, queries, ids in (
                    ("search_cases", data["search_rounds"], data["seen_queries"], source_ids),
                    ("search_docs", data["docs_search_rounds"], data["seen_docs_queries"], docs_ids)):
                audit = [call for call in data["tool_calls"] if call.get("tool") == tool]
                success = [call for call in audit if call.get("status") == "succeeded"]
                if (len(success) != rounds or
                        [call.get("parameters", {}).get("query", "").strip().casefold() for call in success] != queries or
                        set().union(*(set(call.get("result_source_ids", [])) for call in success)) != ids):
                    raise RuntimeError("非法计算检查点工具审计或来源")
            if any(call.get("tool") not in ("search_cases", "search_docs") or
                   call.get("status") not in ("succeeded", "rejected") or
                   (call.get("status") == "rejected" and (call.get("error") not in ("duplicate_query", "search_limit", "invented_query_facts") or call.get("result_source_ids")))
                   for call in data["tool_calls"]):
                raise RuntimeError("非法计算检查点工具审计")
            for evidence in data["evidence"]:
                if evidence["source_id"] in docs_ids:
                    hit = next(item for item in data["docs_hits"] if item["source_id"] == evidence["source_id"])
                    if evidence != {"kind": "docs", **hit}:
                        raise RuntimeError("非法计算检查点 Docs 快照")
            contract_docs = snapshot.get("runtime_contract", {}).get("model_config", {}).get("docs", {})
            if contract_docs and any(hit["docs_version"] != contract_docs.get("docs_version") for hit in data["docs_hits"]):
                raise RuntimeError("Docs 证据与冻结版本不一致")
            if checkpoint.next[0] in ("decision", "search_cases", "search_docs") and (
                    data["repair_attempt"] != 0 or "candidate_proposal" in data or
                    "judge_result" in data or "guardrail_feedback" in data):
                raise RuntimeError("非法计算检查点 repair position")
            if checkpoint.next[0] in ("search_cases", "search_docs", "judge", "repair"):
                decision = data.get("decision_result", {})
                action = decision.get("next_step")
                if (checkpoint.next[0] in ("search_cases", "search_docs") and
                        action != checkpoint.next[0]) or not action:
                    raise RuntimeError("非法计算检查点 Decision")
                if checkpoint.next == ("judge",) and action in ("search_cases", "search_docs") and "candidate_proposal" not in data:
                    raise RuntimeError("非法计算检查点 final proposal")
            if checkpoint.next == ("repair",):
                if (data["repair_attempt"] != 0 or "candidate_proposal" not in data or
                        data.get("judge_result", {}).get("passed") is not False or
                        data.get("guardrail_feedback") != {
                            "proposal": data["candidate_proposal"],
                            "violations": data["judge_result"]["violations"]}):
                    raise RuntimeError("非法计算检查点 rejected judgment")
            elif checkpoint.next == ("judge",):
                if "judge_result" in data or (data["repair_attempt"] == 1 and (
                        "candidate_proposal" not in data or not data.get("guardrail_feedback") or
                        data["agent_steps"] < 3)) or (
                        data["repair_attempt"] == 0 and "guardrail_feedback" in data):
                    raise RuntimeError("非法计算检查点 repaired candidate")
                if data["repair_attempt"] == 1:
                    feedback = data["guardrail_feedback"]
                    previous = proposal_adapter.validate_python(feedback["proposal"])
                    rejected = validate_judgment({"violations": feedback["violations"]}, previous)
                    if rejected.passed or set(feedback) != {"proposal", "violations"}:
                        raise RuntimeError("非法计算检查点 repair feedback")
        elif "agent_data" in values:
            data = durable_state(values["agent_data"])
            if (set(data) - {"compute_observed_seconds"} != {"state_version", "subject", "messages", "clarification_rounds",
                             "tool_calls", "usage", "evidence", "compute_elapsed_seconds"} or
                    data["subject"] != agent_input.subject or
                    data["messages"] != [m.model_dump(mode="json") for m in agent_input.messages] or
                    data["clarification_rounds"] != snapshot.get("clarification_rounds", 0) or
                    data["tool_calls"] or data["usage"] or data["evidence"]):
                raise RuntimeError("非法 initial retrieval 检查点")
        observation = values.get("failure_observation")
        if observation is not None:
            elapsed = observation.get("observed_elapsed_seconds")
            if (observation.get("node") != checkpoint.next[0] or
                    type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0):
                raise RuntimeError("非法失败预算观测")
        return checkpoint

    @staticmethod
    def is_budget_fallback(checkpoint):
        data = checkpoint.values.get("agent_data", {})
        maximum = data.get("execution_limits", {}).get("max_agent_steps")
        return (checkpoint.next in (("decision",), ("repair",)) and
                type(maximum) is int and data.get("agent_steps", 0) >= maximum)

    @staticmethod
    def unknown_timeout(checkpoint):
        if ReviewWorkflow.is_budget_fallback(checkpoint):
            return 0.0
        contract = checkpoint.values["snapshot"].get("runtime_contract", {})
        cap = contract.get("model_call_timeout_seconds")
        limit = contract.get("model_config", {}).get("limits", {}).get("processing_timeout_seconds")
        if (checkpoint.next[0] not in ("decision", "judge", "repair") or
                type(cap) not in (int, float) or cap != 30 or
                type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0):
            raise RuntimeError("未完成调用的冻结 effective timeout 无法可靠确定")
        elapsed = checkpoint.values.get("agent_data", {}).get("compute_elapsed_seconds", 0)
        return min(cap, max(0, limit - elapsed))

    def continue_compute(self, thread_id, runner, *, observation=None):
        checkpoint = self.graph().get_state(self.config(thread_id))
        snapshot = checkpoint.values.get("snapshot", {})
        trace = trace_for(snapshot, thread_id, "continue_compute", runner=runner,
                          checkpoint_id=checkpoint.config.get("configurable", {}).get("checkpoint_id"),
                          attempt=len(checkpoint.values.get("failed_attempts", [])) + 2)
        try:
            result = self._continue_compute(thread_id, runner, observation=observation, trace=trace)
            _finish_trace(trace, outcome="waiting_review")
            return result
        except Exception as exc:
            _finish_trace(trace, outcome="failed", error=type(exc).__name__)
            raise

    def _continue_compute(self, thread_id, runner, *, observation=None, trace=None):
        if not isinstance(runner, AgentRunner):
            raise RuntimeError("计算恢复需要 Agent runtime")
        checkpoint = self.inspect_compute(thread_id)
        values = copy_data(checkpoint.values)
        if "runtime_contract" in values["snapshot"] and (
                values["snapshot"]["runtime_contract"] != runner.recovery_contract):
            raise RuntimeError("Agent 冻结配置已变化")
        failures = []
        graph, config = self.graph(), self.config(thread_id)
        observation = observation or values.get("failure_observation")
        if observation is None and self.is_budget_fallback(checkpoint):
            data = values["agent_data"]
            observation = {"node": checkpoint.next[0], "observed_elapsed_seconds":
                           data["compute_elapsed_seconds"] - data.get("compute_estimated_seconds", 0)}
        if observation is None:
            plan = values.get("resume_plan") or {}
            if plan.get("phase") != "prepared":
                timeout = self.unknown_timeout(checkpoint)
                data = durable_state(values["agent_data"])
                origin = checkpoint.config["configurable"]["checkpoint_id"]
                ledger = copy_data(data.get("compute_estimates", []))
                ledger.append({"classification": "conservative/estimated", "effective_timeout_seconds": timeout,
                               "origin_checkpoint_id": origin, "node": checkpoint.next[0]})
                data["compute_estimated_seconds"] = data.get("compute_estimated_seconds", 0) + timeout
                data["compute_observed_seconds"] = data.get("compute_observed_seconds", data["compute_elapsed_seconds"])
                data["compute_elapsed_seconds"] += timeout
                data["compute_estimates"] = ledger
                plan = {"phase": "prepared", "node": checkpoint.next[0], "origin_checkpoint_id": origin}
                update = {"agent_data": data, "resume_plan": plan}
                if data["compute_elapsed_seconds"] >= runner.config.processing_timeout_seconds:
                    budget = {"observed_elapsed_seconds": data["compute_observed_seconds"],
                              "estimated_elapsed_seconds": data["compute_estimated_seconds"],
                              "total_elapsed_seconds": data["compute_elapsed_seconds"], "estimates": ledger}
                    update["failure_descriptor"] = {"fatal": True, "node": checkpoint.next[0], "code": "execution_budget_exhausted",
                                                    "classification": "conservative/estimated", "execution_budget": budget}
                    graph.update_state(config, update, as_node="resume_gate")
                    failure = RunFailure("execution_budget", {}, [], {"execution_budget": budget})
                    raise failure from TimeoutError("处理时间预算已耗尽")
                # Apply this pure recovery update through a route that always
                # schedules the prepared gate. Inferring the last writer after
                # a gate crash would schedule the unfinished capability directly.
                # update_state does not execute retrieve or any capability.
                graph.update_state(config, update, as_node="bootstrap_retrieve")
            if graph.get_state(config).next != ("resume_gate",):
                raise RuntimeError("预算 gate 未成为唯一待执行节点")
            result = self._invoke(graph, None, config, context=WorkflowContext(runner, failures, trace))
            if failures:
                raise failures[0]
            return self._validated_output(result["output"])
        update = None
        if observation is not None:
            if observation.get("node") != checkpoint.next[0]:
                raise RuntimeError("失败观测与未完成节点不一致")
            elapsed = observation["observed_elapsed_seconds"]
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                raise RuntimeError("非法失败预算观测")
            data = values.get("agent_data")
            if data is not None:
                data["compute_observed_seconds"] = max(data.get("compute_observed_seconds", data["compute_elapsed_seconds"] - data.get("compute_estimated_seconds", 0)), elapsed)
                data["compute_elapsed_seconds"] = data["compute_observed_seconds"] + data.get("compute_estimated_seconds", 0)
                update = {"agent_data": durable_state(data), "failure_observation": None}
            else:
                # Initial retrieval may fail before any committed Agent data.
                snapshot = values["snapshot"]
                execution = runner.new_execution(agent_input_from_snapshot(snapshot),
                                           snapshot.get("clarification_rounds", 0))
                data = execution.data
                data["compute_elapsed_seconds"] = elapsed
                update = {"agent_data": durable_state(data), "failure_observation": None}
            if update is not None and observation.get("failed_attempts"):
                update["failed_attempts"] = copy_data(observation["failed_attempts"])
        result = self._invoke(graph, Command(update=update) if update else None, config, context=WorkflowContext(runner, failures, trace))
        if failures:
            raise failures[0]
        if not result.get("__interrupt__"):
            raise RuntimeError("图未持久化审核中断")
        return self._validated_output(result["output"])

    @staticmethod
    def _invoke(graph, value, config, *, context=None):
        try:
            return graph.invoke(value, config, context=context, **({"durability": "sync"} if graph.checkpointer is not None else {}))
        except RunFailure as exc:
            if graph.checkpointer is None or context is None or not isinstance(context.runner, AgentRunner):
                raise
            # Preserve the committed logical state. Failed attempts are separate
            # diagnostics; only their observed active time is carried forward.
            try:
                checkpoint = graph.get_state(config)
                if checkpoint.next in (("bootstrap_retrieve",), ("decision",), ("search_cases",),
                                       ("search_docs",), ("judge",), ("repair",)):
                    budget = exc.usage.setdefault("execution_budget", {"observed_elapsed_seconds": 0})
                    budget.update(node=checkpoint.next[0], checkpoint_id=checkpoint.config["configurable"]["checkpoint_id"])
                    committed = checkpoint.values.get("agent_data", {}).get("usage", {})
                    reported = {key: copy_data(exc.usage[key][len(committed.get(key, [])):])
                                for key in ("decisions", "embeddings", "semantic_judge")
                                if isinstance(exc.usage.get(key), list) and len(exc.usage[key]) > len(committed.get(key, []))}
                    attempts = copy_data(checkpoint.values.get("failed_attempts", []))
                    attempt = {"node": budget["node"], "checkpoint_id": budget["checkpoint_id"],
                        "stage": exc.stage, "error_type": type(exc.__cause__).__name__,
                        "reported_usage": reported, "provider_usage_unknown": not has_reported_tokens(reported),
                        "observed_elapsed_seconds": budget["observed_elapsed_seconds"]}
                    attempts.append(attempt)
                    exc.usage["failed_attempts"] = attempts
                    graph.update_state(config, {"failure_observation": {
                        **budget, "stage": exc.stage, "error_type": type(exc.__cause__).__name__,
                        "failed_attempts": attempts}, "failed_attempts": attempts})
                    if graph.get_state(config).next != checkpoint.next:
                        raise RuntimeError("失败观测改变了待执行节点")
            except Exception as diagnostic_error:
                logger.warning("failure_observation_not_saved error=%s", type(diagnostic_error).__name__)
            raise

    def pending_output(self, thread_id):
        """Return already-computed output only when the checkpoint is durably waiting for review."""
        graph, config = self.graph(), self.config(thread_id)
        state = graph.get_state(config)
        if not state.values or state.next != ("review",):
            return None
        if not any(task.interrupts for task in state.tasks):
            return None
        output = state.values.get("output")
        data = state.values.get("agent_data", {})
        if data.get("step_limit_reached"):
            from ticketmind.agent.policy import escalation
            fixed = escalation("Agent 执行步数达到上限").model_dump(mode="json")
            if (output is None or output["state"]["proposal"] != fixed or data.get("proposal") != fixed or
                    any(key in data for key in ("candidate_proposal", "judge_result", "guardrail_feedback"))):
                raise RuntimeError("非法程序预算转人工提案")
        return self._validated_output(output) if output is not None else None

    def terminal_budget(self, thread_id):
        """Read the persisted budget failure after a business-save crash."""
        state = self.graph().get_state(self.config(thread_id))
        descriptor = state.values.get("failure_descriptor", {})
        if descriptor.get("fatal") and descriptor.get("code") == "execution_budget_exhausted":
            return copy_data(descriptor.get("execution_budget") or
                             descriptor.get("failure_scope_usage", {}).get("execution_budget", {}))
        return None

    def resume(self, thread_id, saved_review):
        graph, config = self.graph(), self.config(thread_id)
        state = graph.get_state(config)
        snapshot = state.values.get("snapshot", {})
        trace = trace_for(snapshot, thread_id, "review_resume",
                          checkpoint_id=state.config.get("configurable", {}).get("checkpoint_id"))
        if not state.values:
            raise RuntimeError("缺少持久化检查点，请勿重新调用模型")
        try:
            if state.next:
                if state.next != ("review",) or not any(task.interrupts for task in state.tasks):
                    raise RuntimeError("检查点不在可恢复的审核中断处")
                graph.invoke(Command(resume=saved_review), config, context=WorkflowContext(None, None, trace),
                             durability="sync")
                state = graph.get_state(config)
            if state.next or state.values.get("approved_review") != saved_review:
                raise RuntimeError("检查点审核结果与持久化审核不一致")
            _finish_trace(trace, outcome="review_resumed")
            return state.values["output"]
        except Exception as exc:
            _finish_trace(trace, outcome="failed", error=type(exc).__name__)
            raise

    def recovery_kind(self, thread_id, snapshot):
        """Read-only classification, shared by startup and explicit recovery."""
        if not thread_id:
            return "missing"
        checkpoint = self.graph().get_state(self.config(thread_id))
        if not checkpoint.values:
            return "missing"
        if copy_data(checkpoint.values.get("snapshot")) != copy_data(snapshot):
            raise ValueError("检查点 snapshot 与业务运行不一致")
        if checkpoint.values.get("failure_descriptor"):
            return "fatal"
        if self.pending_output(thread_id) is not None:
            return "review"
        if not checkpoint.next and checkpoint.values.get("approved_review"):
            return "completed"
        if (checkpoint.next in (("compute",), ("retrieve",), ("get_case_detail",)) or
                checkpoint.values.get("agent_data", {}).get("state_version") == 1 or
                (checkpoint.values.get("snapshot", {}).get("runtime_contract") is not None and
                 checkpoint.values["snapshot"]["runtime_contract"].get("model_config", {}).get("state_version") != 2) or
                checkpoint.values.get("adapter_output_only")):
            return "legacy_compute"
        if not checkpoint.next:
            return "invalid"
        self.inspect_compute(thread_id)
        return "compute"
