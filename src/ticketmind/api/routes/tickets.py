from typing import Annotated
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session  
from ticketmind.api.schemas.tickets import TicketCreate, TicketRead
from ticketmind.db.session import get_db_session
from ticketmind.tickets.service import create_ticket

router = APIRouter(prefix="/tickets", tags=["tickets"])
@router.post(
    "",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
)
def create_ticket_endpoint(
    payload:TicketCreate,
    session:Annotated[Session, Depends(get_db_session)],
)->TicketRead:
    with session.begin():
        ticket = create_ticket(
            session,
            subject=payload.subject,
            body=payload.body,
            channel=payload.channel,
            requester_role=payload.requester_role,
        )
        response = TicketRead.model_validate(ticket)
    return response