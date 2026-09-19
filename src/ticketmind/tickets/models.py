
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    ForeignKeyConstraint,
    and_,
    text,
)
from sqlalchemy.dialects.postgresql import UUID,JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship,foreign
from ticketmind.db.base import Base
from ticketmind.tickets.enums import (
    AgentAction,
    MessageAuthorType,
    ProcessingRunStatus,
    TicketChannel,
    TicketPriority,
    TicketStatus,
    database_enum,
)

class TicketMessage(Base):
    __tablename__="ticket_messages"
    __table_args__=(
        UniqueConstraint("ticket_id", "actor_id", "operation", "idempotency_key", name="uq_messages_request"),
        UniqueConstraint("ticket_id","sequence_number",name="uq_ticket_messages_ticket_sequence"),
        UniqueConstraint(
        "id",
        "ticket_id",
        name="uq_ticket_messages_id_ticket",
    ),
    )
    
    id : Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ticket_id : Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id"),
        nullable=False,

    )
    sequence_number : Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    author_type : Mapped[MessageAuthorType] = mapped_column(
        database_enum(MessageAuthorType,"message_author_type"),
        nullable=False,
    )
    body : Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    ticket : Mapped[Ticket] = relationship(back_populates="messages")
    processing_results: Mapped[list[ProcessingResult]] = relationship(
    primaryjoin=lambda: and_(
        TicketMessage.id == foreign(ProcessingResult.trigger_message_id),
        TicketMessage.ticket_id == ProcessingResult.ticket_id,
    ),
    viewonly=True,
)

class Ticket(Base):
    __tablename__="tickets"
    __table_args__=(
        Index("ix_tickets_status_priority", "status", "priority"),
        UniqueConstraint("actor_id", "idempotency_key", name="uq_tickets_actor_key"),
    )
    id : Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    ticket_number : Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    subject : Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    channel : Mapped[TicketChannel] = mapped_column(
        database_enum(TicketChannel,"ticket_channel"),  
        nullable=False,
    )
    priority : Mapped[TicketPriority] = mapped_column(
        database_enum(TicketPriority,"ticket_priority"),
        default=TicketPriority.P3,
        nullable=False,
    )
    status : Mapped[TicketStatus] = mapped_column(
        database_enum(TicketStatus,"ticket_status"),
        default=TicketStatus.OPEN,
        nullable=False,
    )
    requester_role:Mapped[str]= mapped_column(  
        String(64),
        nullable=False,
    )
    created_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False, 
    )
    updated_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    resolved_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    messages : Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket",
        order_by="TicketMessage.sequence_number",
    )
    processing_results: Mapped[list[ProcessingResult]] = relationship(
        back_populates="ticket",
        order_by="ProcessingResult.run_sequence",
    )
class ProcessingResult(Base):

    """An auditable result from one TicketAgent processing attempt."""

    __tablename__ = "processing_results"
    __table_args__ = (
        UniqueConstraint("ticket_id", "actor_id", "idempotency_key", name="uq_runs_ticket_actor_key"),
        Index("uq_runs_active_ticket", "ticket_id", unique=True,
              postgresql_where=text("run_status IN ('running', 'waiting_review')")),
        ForeignKeyConstraint(
        ["trigger_message_id", "ticket_id"],
        ["ticket_messages.id", "ticket_messages.ticket_id"],
        name="fk_processing_results_trigger_message_ticket",
    ),
        UniqueConstraint(
            "ticket_id",
            "run_sequence",
            name="uq_processing_results_ticket_run_sequence",
        ),
        CheckConstraint(
            "run_status != 'completed' OR action IS NOT NULL",
            name="ck_processing_results_completed_requires_action",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id"),
        nullable=False,
    )
    trigger_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    run_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    ticket_version: Mapped[int | None] = mapped_column(Integer)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    corpus_version: Mapped[str | None] = mapped_column(String(128))
    retrieval_mode: Mapped[str | None] = mapped_column(String(16))
    model_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    published_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ticket_messages.id"), unique=True)
    review: Mapped[ProcessingReview | None] = relationship(back_populates="run", uselist=False)
    run_status: Mapped[ProcessingRunStatus] = mapped_column(
        database_enum(ProcessingRunStatus, "processing_run_status"),
        default=ProcessingRunStatus.RUNNING,
        nullable=False,
    )
    action: Mapped[AgentAction | None] = mapped_column(
        database_enum(AgentAction, "agent_action"),
        nullable=True,
    )
    retrieval_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )
    agent_version: Mapped[str] = mapped_column(String(128), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    ticket: Mapped[Ticket] = relationship(back_populates="processing_results")
    trigger_message: Mapped[TicketMessage] = relationship(
    primaryjoin=lambda: and_(
        TicketMessage.id == foreign(ProcessingResult.trigger_message_id),
        TicketMessage.ticket_id == ProcessingResult.ticket_id,
    ),
)


class ProcessingReview(Base):
    """One immutable reviewer decision per run; only applied_at is updated."""
    __tablename__ = "processing_reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('approve', 'edit', 'escalate')", name="ck_review_decision"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("processing_results.id"), nullable=False, unique=True)
    reviewer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    edited_reply: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run: Mapped[ProcessingResult] = relationship(back_populates="review")
