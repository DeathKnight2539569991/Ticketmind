from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from ticketmind.agent.proposals import Text

from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger_message_id: UUID
    expected_version: int = Field(ge=1)


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "edit", "escalate"]
    expected_version: int = Field(ge=1)
    edited_reply: Text | None = None
    comment: Text | None = None

    @model_validator(mode="after")
    def decision_fields(self):
        if self.decision == "edit" and (not self.edited_reply or not self.comment):
            raise ValueError("编辑审核需要修改文本和理由")
        if self.decision != "edit" and self.edited_reply is not None:
            raise ValueError("仅 edit 可携带修改文本")
        if self.decision == "escalate" and not self.comment:
            raise ValueError("转人工需要理由")
        return self


class ReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    reviewer_id: str
    decision: str
    edited_reply: str | None
    comment: str | None
    expected_version: int
    created_at: datetime
    applied_at: datetime | None


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
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    published_message_id: UUID | None = None
    review: ReviewRead | None = None

    @field_serializer("created_at", "completed_at")
    def utc_timestamp(self, value):
        return value.astimezone(UTC) if value is not None else None
