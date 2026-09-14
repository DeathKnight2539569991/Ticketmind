
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    ForeignKeyConstraint,
    and_
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
    )
    id : Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
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
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_processing_results_confidence_range",
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
    run_status: Mapped[ProcessingRunStatus] = mapped_column(
        database_enum(ProcessingRunStatus, "processing_run_status"),
        default=ProcessingRunStatus.RUNNING,
        nullable=False,
    )
    action: Mapped[AgentAction | None] = mapped_column(
        database_enum(AgentAction, "agent_action"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_information: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        nullable=False,
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