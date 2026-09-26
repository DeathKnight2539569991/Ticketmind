from ticketmind.db.base import Base
import ticketmind.knowledge.models
from ticketmind.tickets.models import ProcessingResult, Ticket, TicketMessage
from sqlalchemy.orm import configure_mappers

def test_ticket_models_are_registered_in_metadata() -> None:
    assert set(Base.metadata.tables) == {
        "tickets",
        "ticket_messages",
        "processing_results",
        "processing_recoveries",
        "processing_reviews",
        "knowledge_cases",
        "knowledge_datasets",
        "knowledge_operations",
        "knowledge_embeddings",
    }


def test_ticket_message_sequence_is_unique_per_ticket() -> None:
    constraint_names = {
        constraint.name
        for constraint in TicketMessage.__table__.constraints
    }

    assert "uq_ticket_messages_ticket_sequence" in constraint_names


def test_processing_result_removed_obsolete_confidence_and_keeps_action_constraint() -> None:
    constraint_names = {
        constraint.name
        for constraint in ProcessingResult.__table__.constraints
    }

    assert "confidence" not in ProcessingResult.__table__.columns
    assert "ck_processing_results_confidence_range" not in constraint_names
    assert "ck_processing_results_completed_requires_action" in constraint_names

def test_trigger_message_relationship_only_syncs_message_id() -> None:
    configure_mappers()

    synchronized_columns = [
        (source.name, target.name)
        for source, target
        in ProcessingResult.trigger_message.property.synchronize_pairs
    ]

    assert synchronized_columns == [
        ("id", "trigger_message_id"),
    ]
