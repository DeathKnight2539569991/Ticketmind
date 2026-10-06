"""Abandon a stopped running request without reading or executing its checkpoint."""
import hashlib
from datetime import UTC, datetime

from sqlalchemy import select

from ticketmind.api.schemas.runs import RunRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.activity import ticket_activity
from ticketmind.tickets.enums import MessageAuthorType, ProcessingRunStatus as RunStatus
from ticketmind.tickets.models import TicketMessage
from ticketmind.tickets.processing import check_replay, request_hash, require_ticket
from ticketmind.tickets.reviews import require_run
from ticketmind.tickets.writes import append_message


def cancel_interrupted_run(factory, ticket_id, run_id, payload, actor, key):
    if actor.role != "reviewer":
        raise AppError(403, "reviewer_required", "终止中断运行需要 reviewer 权限")
    # The message key is ticket-scoped; bind the path's run identity too.
    digest = hashlib.sha256(f"{run_id}:{request_hash(payload)}".encode()).hexdigest()
    # The same exclusive gate as recovery blocks live compute/review/recovery
    # and prevents new requests from entering until this transaction commits.
    with ticket_activity(ticket_id, recovery=True), factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        run = require_run(session, ticket_id, run_id)
        existing = session.scalar(select(TicketMessage).where(
            TicketMessage.ticket_id == ticket_id, TicketMessage.actor_id == actor.actor_id,
            TicketMessage.operation == "cancel_run", TicketMessage.idempotency_key == key))
        if existing:
            check_replay(existing, digest)
            return RunRead.model_validate(run)
        if ticket.version != payload.expected_version:
            raise AppError(409, "version_conflict", "工单版本已变化，请重新读取")
        if run.run_status != RunStatus.RUNNING:
            raise AppError(409, "run_not_cancellable", "只有执行请求已结束的 running 运行可以终止")
        # A missing, invalid, or unavailable checkpoint must not prevent manual
        # handling. Preserve it for audit; cancelled runs cannot resume or apply.
        run.run_status, run.completed_at = RunStatus.CANCELLED, datetime.now(UTC)
        run.error_code, run.error_summary = "run_cancelled_by_reviewer", "reviewer 已终止中断运行：" + payload.reason
        append_message(session, ticket, body=f"人工终止中断运行 {run_id}：\n{payload.reason}",
            actor_id=actor.actor_id, author_type=MessageAuthorType.SYSTEM,
            operation="cancel_run", key=key, digest=digest)
        session.flush()
        return RunRead.model_validate(run)
