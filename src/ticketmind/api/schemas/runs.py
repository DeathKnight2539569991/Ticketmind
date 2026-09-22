from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer
from ticketmind.agent.proposals import Proposal, Text

from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger_message_id: UUID
    expected_version: int = Field(ge=1)
    retrieval_mode: Literal["dense", "bm25", "hybrid"] | None = None


class ReviewBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ApproveReview(ReviewBase):
    decision: Literal["approve"]
    comment: Text | None = None


class EditReview(ReviewBase):
    decision: Literal["edit"]
    edited_reply: Text
    comment: Text
    final_action: AgentAction | None = None


class EscalateReview(ReviewBase):
    decision: Literal["escalate"]
    comment: Text


ReviewCreate = Annotated[ApproveReview | EditReview | EscalateReview, Field(discriminator="decision")]
review_create_adapter = TypeAdapter(ReviewCreate)


class ReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    reviewer_id: str
    idempotency_key: str
    decision: Literal["approve", "edit", "escalate"]
    edited_reply: str | None
    final_action: AgentAction | None = None
    comment: str | None
    expected_version: int
    created_at: datetime
    applied_at: datetime | None


class RunRecover(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    retrieval_evidence: list[dict[str, Any]]
    proposal: Proposal | None
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
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    published_message_id: UUID | None = None
    review: ReviewRead | None = None

    @field_serializer("created_at", "completed_at")
    def utc_timestamp(self, value):
        return value.astimezone(UTC) if value is not None else None
