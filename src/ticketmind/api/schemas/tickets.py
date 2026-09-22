from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict, StringConstraints, field_serializer, Field
from ticketmind.core.text import Text
from ticketmind.tickets.enums import TicketChannel,TicketStatus, TicketPriority
from ticketmind.api.schemas.runs import RunRead
class TicketCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    body: Text
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
    actor_id: str | None = None
    operation: str | None = None

    @field_serializer("created_at")
    def utc_timestamp(self, value):
        return value.astimezone(UTC)


class TicketDetail(TicketRead):
    messages: list[MessageRead]
    latest_run: RunRead | None = None


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["customer_update", "human_reply"]
    body: Text
    expected_version: int = Field(ge=1)


class TicketClose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Text
    expected_version: int = Field(ge=1)


class TicketEscalate(TicketClose):
    """A reviewer explicitly takes over without requiring an Agent proposal."""
