from ticketmind.agent.understand import understand_ticket
from ticketmind.core.config import QwenSettings


def main() -> None:
    result = understand_ticket(
        settings=QwenSettings(),
        subject="API 调用失败",
        body=(
            "今天上午调用订单查询接口时多次返回 E_TIMEOUT。"
            "运行环境是 Python 3.12，昨天还可以正常调用。"
        ),
    )

    print(result.model_dump_json(indent=2))

    assert result.error_codes == ["E_TIMEOUT"]
    assert any("Python 3.12" in item for item in result.environment)


if __name__ == "__main__":
    main()