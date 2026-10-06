"""Offline/live capability acceptance adapter over the production workflow.

The initial retrieval is supplied by the caller. Routing, guards and repair are
still executed by ReviewWorkflow, never a parallel Python orchestration loop.
"""
from types import SimpleNamespace

from ticketmind.agent.runtime import AgentRunner, AgentExecution, RunFailure
from ticketmind.agent.schemas import AgentRunInput
from ticketmind.agent.state import durable_state, typed_state
from ticketmind.core.config import QwenSettings, MilvusSettings


def run_decision_workflow(state, *, decide, judge, corpus, config, remaining, audit,
                          search_fn, state_callback=None, detail_fn=None, repair_fn=None):
    seed = typed_state(durable_state(state))
    class Capabilities(AgentExecution):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.budget.remaining = remaining
        def retrieve(self, current, record):
            return {"retrieval_query": seed["retrieval_query"], "retrieval_hits": seed["retrieval_hits"]}
        def decide(self, current):
            self.stage = "decision"
            return decide(current)
        def judge(self, current, proposal):
            return judge(current, proposal)
        def repair(self, current):
            return (repair_fn or decide)(current)
        def search_cases(self, query, record):
            return search_fn(query, record)
        def get_case_detail(self, source_id):
            return (detail_fn or corpus.get_case_detail)(source_id)
    class CapabilityRunner(AgentRunner):
        def new_execution(self, agent_input=None, clarification_rounds=0, *, data=None):
            return Capabilities(self, agent_input, clarification_rounds, data=data)
    evidence_corpus = SimpleNamespace(evidence=lambda hits: [{"source_id": hit.source_id} for hit in hits],
        get_case_detail=getattr(corpus, "get_case_detail", None))
    runner = CapabilityRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"), config, corpus=evidence_corpus)
    try:
        output = runner(AgentRunInput.model_validate({"subject": seed["subject"], "messages": seed["messages"]}),
                        clarification_rounds=seed.get("clarification_rounds", 0))
        final = durable_state(output.state)
        return output.state["proposal"], output.state["retrieval_hits"]
    except RunFailure as exc:
        final = durable_state(exc.partial)
        raise exc.__cause__ or exc
    finally:
        if "final" in locals():
            # Hide the supplied initial retrieval from the caller's tool sink.
            final["tool_calls"] = durable_state({**seed, "tool_calls": audit})["tool_calls"] + final["tool_calls"][1:]
            final.pop("proposal", None)
            audit[:] = durable_state(final)["tool_calls"]
            if state_callback is not None:
                state_callback(durable_state(final))
