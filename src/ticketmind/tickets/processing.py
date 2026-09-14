import hashlib
import json
import logging
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import func, select, text

from ticketmind.agent.proposals import proposal_adapter, validate_proposal
from ticketmind.agent.runtime import RunFailure
from ticketmind.api.schemas.runs import RunCreate, RunRead
from ticketmind.api.schemas.tickets import TicketCreate, TicketRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus, TicketStatus
from ticketmind.tickets.models import ProcessingResult, ProcessingReview, Ticket, TicketMessage
from ticketmind.tickets.service import create_ticket

logger = logging.getLogger(__name__)


def request_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def check_replay(record, digest):
    if record.request_hash != digest:
        raise AppError(409, "idempotency_conflict", "同一 Idempotency-Key 已用于不同请求")


def create_ticket_once(session, payload: TicketCreate, actor_id: str, key: str):
    digest = request_hash(payload)
    with session.begin():
        # No ticket row exists yet; serialize only this actor/key, backed by UNIQUE too.
        lock = int.from_bytes(hashlib.sha256(f"create-ticket:{actor_id}:{key}".encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
        existing = session.scalar(select(Ticket).where(Ticket.actor_id == actor_id, Ticket.idempotency_key == key))
        if existing:
            check_replay(existing, digest)
            return TicketRead.model_validate(existing), False
        ticket = create_ticket(session, **payload.model_dump())
        ticket.actor_id, ticket.idempotency_key, ticket.request_hash = actor_id, key, digest
        first_message = session.scalar(select(TicketMessage).where(TicketMessage.ticket_id == ticket.id))
        first_message.actor_id, first_message.operation = actor_id, "create_ticket"
        first_message.idempotency_key, first_message.request_hash = key, digest
        session.flush()
        return TicketRead.model_validate(ticket), True


def require_ticket(session, ticket_id, *, lock=False):
    query = select(Ticket).where(Ticket.id == ticket_id)
    ticket = session.scalar(query.with_for_update() if lock else query)
    if ticket is None:
        raise AppError(404, "ticket_not_found", "工单不存在")
    return ticket


def create_run(session_factory, runner_factory, ticket_id: UUID, payload: RunCreate, actor_id: str, key: str, *, workflow=None):
    digest = request_hash(payload)
    with session_factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        existing = session.scalar(select(ProcessingResult).where(
            ProcessingResult.ticket_id == ticket_id, ProcessingResult.actor_id == actor_id,
            ProcessingResult.idempotency_key == key))
        if existing:
            check_replay(existing, digest)
            return RunRead.model_validate(existing), False
        if ticket.version != payload.expected_version:
            raise AppError(409, "version_conflict", "工单版本已变化，请重新读取工单")
        if ticket.status != TicketStatus.OPEN:
            raise AppError(409, "ticket_not_processable", "当前工单状态不允许自动处理")
        active = session.scalar(select(ProcessingResult.id).where(
            ProcessingResult.ticket_id == ticket_id,
            ProcessingResult.run_status.in_([ProcessingRunStatus.RUNNING, ProcessingRunStatus.WAITING_REVIEW])))
        if active:
            raise AppError(409, "active_run_exists", "该工单已有执行中或待审核运行")
        if session.scalar(select(ProcessingResult.id).join(ProcessingReview).where(
            ProcessingResult.ticket_id == ticket_id, ProcessingResult.run_status == ProcessingRunStatus.FAILED,
            ProcessingReview.applied_at.is_(None))):
            raise AppError(409, "pending_review_recovery", "请重试已保存的审核，或追加新信息使旧方案失效")
        messages = session.scalars(select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
                                   .order_by(TicketMessage.sequence_number)).all()
        if not any(message.id == payload.trigger_message_id for message in messages):
            raise AppError(404, "trigger_message_not_found", "触发消息不属于该工单")
        if messages[-1].id != payload.trigger_message_id:
            raise AppError(409, "stale_trigger_message", "必须使用工单最新消息触发处理")
        if messages[-1].author_type.value != "customer":
            raise AppError(409, "customer_trigger_required", "请使用客户消息触发新运行")
        previous = session.scalars(select(ProcessingResult).where(
            ProcessingResult.ticket_id == ticket_id, ProcessingResult.run_status == ProcessingRunStatus.COMPLETED,
            ProcessingResult.action == AgentAction.ASK_CLARIFICATION)).all()
        previous = [run for run in previous if run.review and run.review.decision != "escalate"]
        body = messages[0].body if len(messages) == 1 else "\n\n".join(
            f"[{message.sequence_number} {message.author_type.value}]\n{message.body}" for message in messages)
        snapshot = {"ticket_id": str(ticket.id), "ticket_version": ticket.version,
                    "trigger_message_id": str(payload.trigger_message_id), "subject": ticket.subject,
                    "body": body, "messages": [{"id": str(m.id), "sequence_number": m.sequence_number,
                    "author_type": m.author_type.value, "body": m.body} for m in messages]}
        snapshot.update(clarification_rounds=len(previous),
                        asked_questions=[q for run in previous for q in (run.proposal or {}).get("questions", [])],
                        approved_clarifications=[run.review.edited_reply or run.final_reply for run in previous])
        runner = runner_factory()
        sequence = session.scalar(select(func.max(ProcessingResult.run_sequence))
                                  .where(ProcessingResult.ticket_id == ticket_id)) or 0
        run_id = uuid4()
        snapshot.update(run_id=str(run_id), agent_version=runner.metadata["agent_version"],
                        corpus_version=runner.metadata["corpus_version"], retrieval_mode=runner.metadata["retrieval_mode"],
                        execution_limits=runner.metadata.get("model_config", {}).get("limits", {}))
        run = ProcessingResult(id=run_id, ticket_id=ticket_id, trigger_message_id=payload.trigger_message_id,
                               run_sequence=sequence + 1, actor_id=actor_id, idempotency_key=key,
                               request_hash=digest, ticket_version=ticket.version,
                               thread_id=f"ticket:{ticket_id}:run:{run_id}", input_snapshot=snapshot,
                               run_status=ProcessingRunStatus.RUNNING, **runner.metadata)
        session.add(run)
        session.flush()
    # The transaction AND its connection are released before any network work.
    started = monotonic()
    try:
        if workflow is None:
            raise RuntimeError("持久化审核工作流未初始化")
        output = workflow.start(snapshot, f"ticket:{ticket_id}:run:{run_id}", runner)
        proposal = proposal_adapter.validate_python(output.state["proposal"])
        validate_proposal(proposal, {hit["source_id"] for hit in output.evidence})
    except Exception as exc:
        with session_factory() as session, session.begin():
            run = session.get(ProcessingResult, run_id)
            run.run_status = ProcessingRunStatus.FAILED
            run.error_code = "agent_execution_failed"
            stage = exc.stage if isinstance(exc, RunFailure) else "agent"
            logger.error("run_id=%s stage=%s error=%s", run_id, stage, type(exc.__cause__ or exc).__name__)
            run.error_summary = f"{stage} 阶段未成功完成；请检查模型、检索服务及输出约束后发起新运行"
            if isinstance(exc, RunFailure):
                understanding = exc.partial.get("understanding")
                run.extracted_information = understanding.model_dump() if understanding is not None else {}
                run.retrieval_evidence, run.usage = exc.evidence, exc.usage
                run.tool_calls = exc.partial.get("tool_calls", [])
            run.completed_at = datetime.now(UTC)
            run.duration_ms = round((monotonic() - started) * 1000)
            session.flush()
            return RunRead.model_validate(run), True
    with session_factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        run = session.get(ProcessingResult, run_id)
        run.completed_at = None
        run.duration_ms = round((monotonic() - started) * 1000)
        run.extracted_information = output.state["understanding"].model_dump()
        run.retrieval_evidence, run.usage = output.evidence, output.usage
        run.tool_calls = output.state.get("tool_calls", [])
        if ticket.version != run.ticket_version:
            run.run_status = ProcessingRunStatus.FAILED
            run.completed_at = datetime.now(UTC)
            run.error_code, run.error_summary = "version_conflict", "工单版本在处理期间变化，结果未进入待审核"
        else:
            run.proposal = proposal.model_dump()
            run.action = {"propose_resolution": AgentAction.RESOLVE,
                          "ask_clarification": AgentAction.ASK_CLARIFICATION,
                          "escalate": AgentAction.ESCALATE}[proposal.next_step]
            run.reason, run.final_reply = proposal.reason, proposal.reply
            run.run_status = ProcessingRunStatus.WAITING_REVIEW
        session.flush()
        return RunRead.model_validate(run), True
