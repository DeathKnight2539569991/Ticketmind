from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger_message_id: UUID
    expected_version: int = Field(ge=1)


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ticket_id: UUID
    trigger_message_id: UUID
    run_sequence: int
    run_status: ProcessingRunStatus
    actor_id: str | None
    ticket_version: int | None
    thread_id: str | None
    action: AgentAction | None
    reason: str | None
    final_reply: str | None
    confidence: float | None
    extracted_information: dict[str, Any]
    retrieval_evidence: list[dict[str, Any]]
    proposal: dict[str, Any] | None
    agent_version: str
    corpus_version: str | None
    retrieval_mode: str | None
    models: dict[str, Any] | None = Field(validation_alias="model_config", serialization_alias="models")
    usage: dict[str, Any] | None
    error_code: str | None
    error_summary: str | None
    created_at: datetime
    completed_at: datetime | None
    duration_ms: int | None

    @field_serializer("created_at", "completed_at")
    def utc_timestamp(self, value):
        return value.astimezone(UTC) if value is not None else None
