from typing import Annotated
from fastapi import APIRouter, Depends, Response, Query, status
from sqlalchemy.orm import Session  
from ticketmind.api.schemas.tickets import TicketCreate, TicketRead, TicketDetail
from ticketmind.api.schemas.runs import RunRead
from ticketmind.api.dependencies import get_session, IdempotencyKey
from ticketmind.core.auth import ActorDependency
from ticketmind.tickets.processing import create_ticket_once, require_ticket
from ticketmind.tickets.models import Ticket, ProcessingResult
from ticketmind.tickets.enums import TicketStatus
from sqlalchemy import select
from uuid import UUID

router = APIRouter(prefix="/tickets", tags=["tickets"])
@router.post(
    "",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
)
def create_ticket_endpoint(
    payload:TicketCreate,
    session:Annotated[Session, Depends(get_session)],
    actor:ActorDependency,
    key:IdempotencyKey,
    response:Response,
)->TicketRead:
    result, created = create_ticket_once(session, payload, actor.actor_id, key)
    response.status_code = 201 if created else 200
    return result


@router.get("", response_model=list[TicketRead])
def list_tickets(actor: ActorDependency, session: Annotated[Session, Depends(get_session)],
                 status: TicketStatus | None = None, limit: int = Query(20, ge=1, le=100),
                 offset: int = Query(0, ge=0)):
    query = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())
    if status is not None:
        query = query.where(Ticket.status == status)
    return session.scalars(query.limit(limit).offset(offset)).all()


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: UUID, actor: ActorDependency, session: Annotated[Session, Depends(get_session)]):
    ticket = require_ticket(session, ticket_id)
    result = TicketDetail.model_validate(ticket)
    latest = session.scalar(select(ProcessingResult).where(ProcessingResult.ticket_id == ticket_id)
                            .order_by(ProcessingResult.run_sequence.desc()).limit(1))
    result.latest_run = RunRead.model_validate(latest) if latest else None
    return result
