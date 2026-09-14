from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID
from pydantic import BaseModel,ConfigDict, StringConstraints, field_serializer
from ticketmind.tickets.enums import TicketChannel,TicketStatus, TicketPriority
from ticketmind.api.schemas.runs import RunRead
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
    version: int = 1
    ticket_number:str
    subject: str
    channel: TicketChannel
    status: TicketStatus
    priority: TicketPriority
    requester_role:str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None

    @field_serializer("created_at", "updated_at", "resolved_at")
    def utc_timestamp(self, value):
        return value.astimezone(UTC) if value is not None else None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    sequence_number: int
    author_type: str
    body: str
    created_at: datetime

    @field_serializer("created_at")
    def utc_timestamp(self, value):
        return value.astimezone(UTC)


class TicketDetail(TicketRead):
    messages: list[MessageRead]
    latest_run: RunRead | None = None
