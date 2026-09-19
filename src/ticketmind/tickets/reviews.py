"""Claim immutable reviews, resume outside transactions, atomically apply once."""
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from ticketmind.agent.proposals import proposal_adapter
from ticketmind.api.schemas.runs import RunRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.enums import AgentAction, MessageAuthorType, ProcessingRunStatus as RunStatus, TicketStatus
from ticketmind.tickets.models import ProcessingResult, ProcessingReview
from ticketmind.tickets.processing import check_replay, request_hash, require_ticket
from ticketmind.tickets.writes import append_message

logger = logging.getLogger(__name__)


def require_run(session, ticket_id, run_id):
    run = session.scalar(select(ProcessingResult).where(ProcessingResult.id == run_id,
                                                       ProcessingResult.ticket_id == ticket_id))
    if run is None:
        raise AppError(404, "run_not_found", "该工单下不存在指定运行")
    return run


def review_payload(review):
    return {"review_id": str(review.id), "run_id": str(review.run_id), "reviewer_id": review.reviewer_id,
            "decision": review.decision, "edited_reply": review.edited_reply, "comment": review.comment,
            "expected_version": review.expected_version}


def review_run(factory, workflow, ticket_id, run_id, payload, actor_id, key):
    digest, created = request_hash(payload), False
    with factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        run = require_run(session, ticket_id, run_id)
        review = run.review
        if review:
            if review.reviewer_id != actor_id or review.idempotency_key != key:
                raise AppError(409, "review_conflict", "该运行已有不可变审核，请重试原请求")
            check_replay(review, digest)
            if review.applied_at is not None:
                return RunRead.model_validate(run), False
            if run.run_status == RunStatus.RUNNING:
                return RunRead.model_validate(run), False
        if run.run_status == RunStatus.CANCELLED or ticket.version != payload.expected_version or ticket.version != run.ticket_version:
            raise AppError(409, "stale_review", "工单或方案已变化，旧方案不能审核")
        if ticket.status != TicketStatus.OPEN:
            raise AppError(409, "ticket_not_reviewable", "当前工单状态不允许审核")
        if (review is None and run.run_status != RunStatus.WAITING_REVIEW) or (review and run.run_status != RunStatus.FAILED):
            raise AppError(409, "run_not_reviewable", "运行不处于可审核或可重试审核状态")
        if session.scalar(select(ProcessingResult.id).where(ProcessingResult.ticket_id == ticket_id,
            ProcessingResult.id != run_id, ProcessingResult.run_status.in_([RunStatus.RUNNING, RunStatus.WAITING_REVIEW]))):
            raise AppError(409, "active_run_exists", "该工单已有另一条有效运行")
        if review is None:
            review = ProcessingReview(run_id=run_id, reviewer_id=actor_id, idempotency_key=key,
                                      request_hash=digest, **payload.model_dump())
            session.add(review)
            created = True
        run.run_status, run.error_code, run.error_summary = RunStatus.RUNNING, None, None
        run.completed_at = None
        session.flush()
        saved_review, thread_id = review_payload(review), run.thread_id
        original_proposal = run.proposal
    try:
        output = workflow.resume(thread_id, saved_review)
        if output["state"]["proposal"] != original_proposal:
            raise RuntimeError("检查点与业务提案不一致")
        apply_review(factory, ticket_id, run_id)
    except Exception as exc:
        logger.error("run_id=%s stage=review_apply error=%s", run_id, type(exc).__name__)
        with factory() as session, session.begin():
            require_ticket(session, ticket_id, lock=True)
            run = require_run(session, ticket_id, run_id)
            if run.review.applied_at is None and run.run_status == RunStatus.RUNNING:
                run.run_status = RunStatus.FAILED
                run.error_code, run.error_summary = "review_application_failed", "审核已保存但应用失败；可重试同一审核请求"
                run.completed_at = datetime.now(UTC)
    with factory() as session:
        return RunRead.model_validate(require_run(session, ticket_id, run_id)), created


def apply_review(factory, ticket_id, run_id):
    with factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        run = require_run(session, ticket_id, run_id)
        review = run.review
        if review.applied_at is not None:
            return
        if run.run_status != RunStatus.RUNNING or ticket.version != review.expected_version or ticket.version != run.ticket_version:
            raise AppError(409, "stale_review", "版本已变化，审核未应用")
        proposal = proposal_adapter.validate_python(run.proposal)
        action = AgentAction.ESCALATE if review.decision == "escalate" else run.action
        reply = review.edited_reply if review.decision == "edit" else proposal.reply
        if review.decision == "escalate":
            reply = "人工审核转交人工处理：" + review.comment
        message = append_message(session, ticket,
            body=reply, actor_id=review.reviewer_id,
            author_type=MessageAuthorType.SYSTEM if action == AgentAction.ESCALATE else MessageAuthorType.HUMAN_SUPPORT,
            operation="review", key=str(review.id), digest=review.request_hash)
        ticket.status = {AgentAction.RESOLVE: TicketStatus.OPEN, AgentAction.ASK_CLARIFICATION: TicketStatus.AWAITING_CUSTOMER,
                         AgentAction.ESCALATE: TicketStatus.ESCALATED}[action]
        run.published_message_id = message.id
        review.applied_at = datetime.now(UTC)
        run.run_status, run.completed_at = RunStatus.COMPLETED, review.applied_at
        run.error_code = run.error_summary = None


def recover_interrupted_runs(factory):
    """Only called after acquiring the single-instance lease. Never invokes a model."""
    with factory() as session, session.begin():
        runs = session.scalars(select(ProcessingResult).where(ProcessingResult.run_status == RunStatus.RUNNING)).all()
        for run in runs:
            run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
            run.error_code = "review_interrupted" if run.review else "execution_interrupted"
            run.error_summary = "进程中断；重试原审核请求" if run.review else "计算被中断；确认调用预算后使用新 key 发起新运行"
        return len(runs)
