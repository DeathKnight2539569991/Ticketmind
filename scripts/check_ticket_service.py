from ticketmind.api.schemas.tickets import TicketCreate, TicketRead
from ticketmind.db.session import SesstionLocal
from ticketmind.tickets.service import create_ticket


request = TicketCreate(
    subject="API 调用失败",
    body="请求返回 E_TIMEOUT",
    channel="api",
    requester_role="developer",
)

with SesstionLocal() as session:
    session.begin()
    try:
        ticket = create_ticket(
            session,
            subject=request.subject,
            body=request.body,
            channel=request.channel,
            requester_role=request.requester_role,
        )

        response = TicketRead.model_validate(ticket)

        assert response.status.value == "open"
        assert response.priority.value == "P3"
        assert response.ticket_number.startswith("TM-")

        print(response.model_dump_json(indent=2))
    finally:
        # 本脚本只验证，不保留演示数据。
        session.rollback()