import pytest
from pydantic import ValidationError

from ticketmind.api.schemas.tickets import TicketCreate
from ticketmind.tickets.enums import TicketChannel


def test_create_ticket_normalizes_text() -> None:
    ticket = TicketCreate(
        subject="  API 调用失败  ",
        body="  返回错误 E_TIMEOUT  ",
        channel="api",
        requester_role="developer",
    )

    assert ticket.subject == "API 调用失败"
    assert ticket.body == "返回错误 E_TIMEOUT"
    assert ticket.channel is TicketChannel.API


@pytest.mark.parametrize(
    "updates",
    [
        {"body": " \n\t "},
        {"channel": "sms"},
        {"status": "resolved"},
        {"priority": "P1"},
        {"ticket_number": "custom"},
    ],
)
def test_create_ticket_rejects_invalid_input(updates) -> None:
    payload = {
        "subject": "API 调用失败",
        "body": "返回错误 E_TIMEOUT",
        "channel": "api",
        "requester_role": "developer",
    }
    payload.update(updates)

    with pytest.raises(ValidationError):
        TicketCreate.model_validate(payload)