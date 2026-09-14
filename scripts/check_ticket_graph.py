from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.agent.state import TicketAgentState
from ticketmind.core.config import QwenSettings


def main() -> None:
    graph = build_ticket_graph(QwenSettings())

    initial_state: TicketAgentState = {
        "subject": "API 调用失败",
        "body": (
            "今天上午调用订单查询接口时多次返回 E_TIMEOUT。"
            "运行环境是 Python 3.12，昨天还可以正常调用。"
        ),
    }

    final_state = graph.invoke(initial_state)

    assert final_state["subject"] == initial_state["subject"]
    assert final_state["body"] == initial_state["body"]

    understanding = final_state["understanding"]
    assert isinstance(understanding, TicketUnderstanding)
    assert understanding.error_codes == ["E_TIMEOUT"]
    assert any(
        "Python 3.12" in item
        for item in understanding.environment
    )

    print("最终状态包含：", list(final_state.keys()))
    print(understanding.model_dump_json(indent=2))


if __name__ == "__main__":
    main()