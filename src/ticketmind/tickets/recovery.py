"""Restore business state from durable output, without generating or publishing."""
from datetime import UTC, datetime

from sqlalchemy import select

from ticketmind.agent.proposals import proposal_adapter
from ticketmind.api.schemas.runs import RunRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.activity import ticket_activity
from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus as RunStatus, TicketStatus
from ticketmind.tickets.models import ProcessingRecovery, ProcessingResult
from ticketmind.tickets.processing import check_replay, request_hash, require_ticket
from ticketmind.tickets.reviews import require_run


def recover_run(factory, workflow, ticket_id, run_id, payload, actor, key):
    if actor.role != "reviewer":
        raise AppError(403, "reviewer_required", "恢复运行需要 reviewer 权限")
    digest = request_hash(payload)
    with ticket_activity(ticket_id, recovery=True), factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        run = require_run(session, ticket_id, run_id)
        previous = session.scalar(select(ProcessingRecovery).where(
            ProcessingRecovery.run_id == run_id, ProcessingRecovery.actor_id == actor.actor_id,
            ProcessingRecovery.idempotency_key == key))
        if previous:
            check_replay(previous, digest)
            return RunRead.model_validate(run)
        if ticket.version != payload.expected_version:
            raise AppError(409, "version_conflict", "工单版本已变化，请重新读取")
        before = run.run_status
        if before in (RunStatus.RUNNING, RunStatus.FAILED):
            active = session.scalar(select(ProcessingResult.id).where(
                ProcessingResult.ticket_id == ticket_id, ProcessingResult.id != run_id,
                ProcessingResult.run_status.in_([RunStatus.RUNNING, RunStatus.WAITING_REVIEW])))
            if active:
                raise AppError(409, "active_run_exists", "已有另一条有效运行，不能恢复旧运行")
            if ticket.status != TicketStatus.OPEN or ticket.version != run.ticket_version:
                run.run_status, run.completed_at = RunStatus.CANCELLED, datetime.now(UTC)
                run.error_code, run.error_summary = "proposal_invalidated", "工单已变化，旧运行不能恢复"
            elif run.review:
                # Keep the immutable review and its original idempotency key.
                # The reviewer retries it separately; recovery never publishes.
                run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
                run.error_code, run.error_summary = "review_interrupted", "执行请求已结束；请重试原审核请求"
            else:
                if workflow is None:
                    raise AppError(503, "checkpoint_unavailable", "检查点服务不可用，未更改运行状态")
                try:
                    output = workflow.pending_output(run.thread_id) if run.thread_id else None
                except Exception:
                    raise AppError(503, "checkpoint_unavailable", "无法读取检查点，未更改运行状态，请稍后重试") from None
                if output is None:
                    run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
                    if before == RunStatus.RUNNING:
                        run.error_code = "execution_interrupted"
                        run.error_summary = "请求已结束且没有可恢复提案；可人工接管，或确认调用预算后发起新运行"
                else:
                    proposal = proposal_adapter.validate_python(output.state["proposal"])
                    run.proposal = proposal.model_dump(mode="json")
                    run.action = {"propose_resolution": AgentAction.RESOLVE,
                                  "ask_clarification": AgentAction.ASK_CLARIFICATION,
                                  "escalate": AgentAction.ESCALATE}[proposal.next_step]
                    run.retrieval_evidence, run.usage = output.evidence, output.usage
                    run.tool_calls = output.state.get("tool_calls", [])
                    run.run_status, run.completed_at = RunStatus.WAITING_REVIEW, None
                    run.error_code = run.error_summary = None
        session.add(ProcessingRecovery(run_id=run_id, actor_id=actor.actor_id, idempotency_key=key,
            request_hash=digest, previous_status=before.value, resulting_status=run.run_status.value))
        session.flush()
        return RunRead.model_validate(run)
