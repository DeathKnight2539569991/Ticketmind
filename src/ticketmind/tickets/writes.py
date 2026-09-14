from datetime import UTC, datetime
from sqlalchemy import func, select

from ticketmind.api.schemas.tickets import MessageRead, TicketRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.enums import MessageAuthorType, ProcessingRunStatus as RunStatus, TicketStatus
from ticketmind.tickets.models import TicketMessage, ProcessingResult
from ticketmind.tickets.processing import check_replay, request_hash, require_ticket


def append_message(session, ticket, *, body, actor_id, author_type, operation, key, digest):
    sequence = session.scalar(select(func.max(TicketMessage.sequence_number)).where(TicketMessage.ticket_id == ticket.id)) or 0
    message = TicketMessage(ticket_id=ticket.id, sequence_number=sequence + 1, body=body,
        actor_id=actor_id, author_type=author_type, operation=operation, idempotency_key=key, request_hash=digest)
    session.add(message)
    ticket.version += 1
    session.flush()
    return message


def write_ticket(session, ticket_id, payload, actor, key, *, close=False):
    if (close or payload.kind == "human_reply") and actor.role != "reviewer":
        raise AppError(403, "reviewer_required", "人工回复或关闭需要 reviewer 权限")
    operation, digest = "close" if close else "message", request_hash(payload)
    with session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        existing = session.scalar(select(TicketMessage).where(TicketMessage.ticket_id == ticket_id,
            TicketMessage.actor_id == actor.actor_id, TicketMessage.operation == operation, TicketMessage.idempotency_key == key))
        if existing:
            check_replay(existing, digest)
            return (TicketRead.model_validate(ticket) if close else MessageRead.model_validate(existing)), False
        if ticket.version != payload.expected_version:
            raise AppError(409, "version_conflict", "工单版本已变化，请重新读取")
        if ticket.status == TicketStatus.RESOLVED:
            raise AppError(409, "ticket_resolved", "已关闭工单不能追加消息或再次关闭")
        runs = session.scalars(select(ProcessingResult).where(ProcessingResult.ticket_id == ticket_id)).all()
        if any(run.run_status == RunStatus.RUNNING for run in runs):
            raise AppError(409, "active_run_exists", "执行期间不允许写入工单")
        # Every material write increments the version and invalidates stale proposals.
        for run in runs:
            if run.run_status == RunStatus.WAITING_REVIEW or (run.run_status == RunStatus.FAILED and run.review and run.review.applied_at is None):
                run.run_status, run.completed_at = RunStatus.CANCELLED, datetime.now(UTC)
                run.error_code, run.error_summary = "proposal_invalidated", "工单有新信息或已关闭，旧提案失效"
        message = append_message(session, ticket, body=payload.reason if close else payload.body,
            actor_id=actor.actor_id, author_type=MessageAuthorType.SYSTEM if close else (
                MessageAuthorType.CUSTOMER if payload.kind == "customer_update" else MessageAuthorType.HUMAN_SUPPORT),
            operation=operation, key=key, digest=digest)
        if close:
            ticket.status, ticket.resolved_at = TicketStatus.RESOLVED, datetime.now(UTC)
        elif payload.kind == "customer_update" and ticket.status != TicketStatus.ESCALATED:
            ticket.status = TicketStatus.OPEN
        session.flush()
        return (TicketRead.model_validate(ticket) if close else MessageRead.model_validate(message)), True
