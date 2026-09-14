from datetime import datetime
from typing import Annotated
from uuid import UUID
from pydantic import BaseModel,ConfigDict, StringConstraints
from ticketmind.tickets.enums import TicketChannel,TicketStatus, TicketPriority
class TicketCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    channel: TicketChannel
    requester_role:Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
class TicketRead(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
    )
    id: UUID
    ticket_number:str
    subject: str
    channel: TicketChannel
    status: TicketStatus
    priority: TicketPriority
    requester_role:str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None