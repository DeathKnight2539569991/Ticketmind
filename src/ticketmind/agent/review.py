"""Durable orchestration. Nodes have no ORM writes; review resume never calls a model."""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ticketmind.agent.proposals import proposal_adapter, validate_proposal
from ticketmind.agent.runtime import RunOutput
from ticketmind.agent.schemas import TicketUnderstanding


class ReviewState(TypedDict, total=False):
    snapshot: dict
    output: dict
    approved_review: dict


def review_node(state):
    review = interrupt({"run_id": state["snapshot"]["run_id"],
                        "proposal": state["output"]["state"]["proposal"]})
    # The coordinator builds this only from the immutable database row.
    return {"approved_review": review}


class ReviewWorkflow:
    def __init__(self, checkpointer):
        self.checkpointer = checkpointer

    def graph(self, runner=None):
        def compute(state):
            if runner is None:
                raise RuntimeError("审核恢复禁止重新计算 Agent")
            output = runner(state["snapshot"])
            proposal = proposal_adapter.validate_python(output.state["proposal"])
            validate_proposal(proposal, {hit["source_id"] for hit in output.evidence})
            return {"output": {"state": {
                "proposal": proposal.model_dump(mode="json"),
                "understanding": output.state["understanding"].model_dump(mode="json"),
                "tool_calls": output.state.get("tool_calls", []),
            }, "evidence": output.evidence, "usage": output.usage}}

        builder = StateGraph(ReviewState)
        builder.add_node("compute", compute)
        builder.add_node("review", review_node)
        builder.add_edge(START, "compute")
        builder.add_edge("compute", "review")
        builder.add_edge("review", END)
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def config(thread_id):
        return {"configurable": {"thread_id": thread_id}}

    def start(self, snapshot, thread_id, runner):
        graph = self.graph(runner)
        result = graph.invoke({"snapshot": snapshot}, self.config(thread_id), durability="sync")
        if not result.get("__interrupt__"):
            raise RuntimeError("图未持久化审核中断")
        output = result["output"]
        state = {**output["state"], "understanding": TicketUnderstanding.model_validate(output["state"]["understanding"]),
                 "proposal": proposal_adapter.validate_python(output["state"]["proposal"])}
        return RunOutput(state, output["evidence"], output["usage"])

    def resume(self, thread_id, saved_review):
        graph, config = self.graph(), self.config(thread_id)
        state = graph.get_state(config)
        if not state.values:
            raise RuntimeError("缺少持久化检查点，请勿重新调用模型")
        if state.next:
            if state.next != ("review",) or not any(task.interrupts for task in state.tasks):
                raise RuntimeError("检查点不在可恢复的审核中断处")
            graph.invoke(Command(resume=saved_review), config, durability="sync")
            state = graph.get_state(config)
        if state.next or state.values.get("approved_review") != saved_review:
            raise RuntimeError("检查点审核结果与持久化审核不一致")
        return state.values["output"]
