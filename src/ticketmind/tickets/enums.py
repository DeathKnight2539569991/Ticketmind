from enum import StrEnum
from sqlalchemy import Enum as SqlEnum

class TicketStatus(StrEnum):
    OPEN="open"
    AWAITING_CUSTOMER="awaiting_customer"
    RESOLVED="resolved"
    ESCALATED="escalated"
class TicketPriority(StrEnum):
    P1="P1"
    P2="P2"
    P3="P3"
    P4="P4"

class TicketChannel(StrEnum):
    WEB="web"
    EMAIL="email"
    API="api"
class MessageAuthorType(StrEnum):
    CUSTOMER="customer"
    AGENT="agent"
    HUMAN_SUPPORT="human_support"
    SYSTEM="system"
class ProcessingRunStatus(StrEnum):
    RUNNING="running"
    WAITING_REVIEW="waiting_review"
    COMPLETED="completed"
    FAILED="failed"
class AgentAction(StrEnum):
    RESOLVE = "resolve"
    ASK_CLARIFICATION = "ask_clarification"
    ESCALATE = "escalate"
def database_enum(enum_class: type[StrEnum],name:str) -> SqlEnum:
    """Create a SQLAlchemy Enum type from a StrEnum class."""
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )
