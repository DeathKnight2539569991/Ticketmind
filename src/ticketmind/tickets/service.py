from uuid import uuid4
from sqlalchemy.orm import Session
from ticketmind.tickets.enums import (
    MessageAuthorType,
    TicketChannel,
    TicketPriority,
    TicketStatus,
)
from ticketmind.tickets.models import Ticket, TicketMessage

def create_ticket(
        session: Session,
        *,
        subject: str,
        body: str,  
        channel: TicketChannel,
        requester_role: str
)->Ticket:
    ticket = Ticket(
        ticket_number=f"TM-{uuid4().hex}",
        subject=subject,
        channel=channel,
        requester_role=requester_role,
        status=TicketStatus.OPEN,
        priority=TicketPriority.P3,
    )
    session.add(ticket)
    session.flush()  # Flush to get the ticket ID for the message
    message = TicketMessage(
        ticket_id=ticket.id, 
        body=body, 
        author_type=MessageAuthorType.CUSTOMER,
        sequence_number=1
        )
    session.add(message)
    session.flush()
    return ticket