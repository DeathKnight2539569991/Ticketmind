import hashlib
import json
import logging
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.semantic_judge import GuardrailFailure
from ticketmind.agent.runtime import RunFailure
from ticketmind.api.schemas.runs import RunCreate, RunRead
from ticketmind.api.schemas.tickets import TicketCreate, TicketRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus, TicketStatus
from ticketmind.tickets.models import ProcessingResult, ProcessingReview, Ticket, TicketMessage
from ticketmind.tickets.service import create_ticket
from ticketmind.tickets.activity import ticket_activity

logger = logging.getLogger(__name__)


def request_hash(payload) -> str:
    data = payload.model_dump(mode="json")
    # Omitted action preserves the request identity of historical edit reviews.
    if data.get("final_action") is None:
        data.pop("final_action", None)
    if data.get("retrieval_mode") is None:
        data.pop("retrieval_mode", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True,
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
    with ticket_activity(ticket_id):
        return _create_run(session_factory, runner_factory, ticket_id, payload, actor_id, key, workflow=workflow)


def _create_run(session_factory, runner_factory, ticket_id, payload, actor_id, key, *, workflow):
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
            ProcessingResult.ticket_id == ticket_id, ProcessingResult.run_status == ProcessingRunStatus.COMPLETED)).all()
        previous = [run for run in previous if run.review and run.review.applied_at is not None
                    and run.review.decision != "escalate"
                    and (run.review.final_action or run.action) == AgentAction.ASK_CLARIFICATION]
        snapshot = {
            "ticket_id": str(ticket.id),
            "ticket_version": ticket.version,
            "trigger_message_id": str(payload.trigger_message_id),
            "subject": ticket.subject,
            "messages": [
                {
                    "id": str(message.id),
                    "sequence_number": message.sequence_number,
                    "author_type": message.author_type.value,
                    "body": message.body,
                }
                for message in messages
            ],
            "clarification_rounds": len(previous),
        }
        from ticketmind.agent.review import agent_input_from_snapshot
        from ticketmind.core.text import CONVERSATION_MAX_CHARS, MESSAGE_MAX_CHARS
        try:
            agent_input_from_snapshot(snapshot)
        except ValidationError:
            raise AppError(422, "agent_input_too_large",
                f"Agent 单条消息限 {MESSAGE_MAX_CHARS} 字符，完整上下文限 {CONVERSATION_MAX_CHARS} 字符；"
                "未创建运行或调用模型，请人工接管此工单") from None
        runner = runner_factory()
        metadata = runner.metadata
        sequence = session.scalar(select(func.max(ProcessingResult.run_sequence))
                                  .where(ProcessingResult.ticket_id == ticket_id)) or 0
        run_id = uuid4()
        snapshot.update(run_id=str(run_id), agent_version=metadata["agent_version"],
                        corpus_version=metadata["corpus_version"], retrieval_mode=metadata["retrieval_mode"],
                        execution_limits=metadata.get("model_config", {}).get("limits", {}))
        run = ProcessingResult(id=run_id, ticket_id=ticket_id, trigger_message_id=payload.trigger_message_id,
                               run_sequence=sequence + 1, actor_id=actor_id, idempotency_key=key,
                               request_hash=digest, ticket_version=ticket.version,
                               thread_id=f"ticket:{ticket_id}:run:{run_id}", input_snapshot=snapshot,
                               run_status=ProcessingRunStatus.RUNNING, **metadata)
        session.add(run)
        session.flush()
    # The transaction AND its connection are released before any network work.
    started = monotonic()
    try:
        if workflow is None:
            raise RuntimeError("持久化审核工作流未初始化")
        output = workflow.start(snapshot, f"ticket:{ticket_id}:run:{run_id}", runner)
        proposal = proposal_adapter.validate_python(output.state["proposal"])
    except Exception as exc:
        with session_factory() as session, session.begin():
            run = session.get(ProcessingResult, run_id)
            run.run_status = ProcessingRunStatus.FAILED
            from ticketmind.retrieval.schemas import RetrievalError
            cause = exc.__cause__ if isinstance(exc, RunFailure) else exc
            run.error_code = cause.code if isinstance(cause, (RetrievalError, GuardrailFailure)) else "agent_execution_failed"
            stage = exc.stage if isinstance(exc, RunFailure) else "agent"
            logger.exception(
    "run_id=%s stage=%s error=%s",
    run_id,
    stage,
    type(exc.__cause__ or exc).__name__,
)
            run.error_summary = f"{stage} 阶段未成功完成；请检查模型、检索服务及输出约束后发起新运行"
            if isinstance(cause, GuardrailFailure):
                run.error_summary = str(cause)
            if isinstance(exc, RunFailure):
                run.retrieval_evidence, run.usage = exc.evidence, exc.usage
                run.tool_calls = exc.partial.get("tool_calls", [])
            run.completed_at = datetime.now(UTC)
            run.duration_ms = round((monotonic() - started) * 1000)
            session.flush()
            return RunRead.model_validate(run), True
    try:
        return save_run_output(session_factory, ticket_id, run_id, output, round((monotonic() - started) * 1000)), True
    except SQLAlchemyError:
        logger.error("run_id=%s stage=result_persistence error=database_unavailable", run_id)
        raise AppError(503, "run_result_not_saved",
            f"运行 {run_id} 的结果未确认落库；数据库恢复后请使用恢复运行入口，勿重新调用模型") from None


def save_run_output(session_factory, ticket_id, run_id, output, duration_ms):
    with session_factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        run = session.get(ProcessingResult, run_id)
        run.completed_at = None
        run.duration_ms = duration_ms
        proposal = proposal_adapter.validate_python(output.state["proposal"])
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
            run.run_status = ProcessingRunStatus.WAITING_REVIEW
        session.flush()
        return RunRead.model_validate(run)
