"""Explicit reviewer recovery: restore outputs or continue compute, never publish."""
from datetime import UTC, datetime
import math

from sqlalchemy import select

from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.api.schemas.runs import RunRead
from ticketmind.core.errors import AppError
from ticketmind.tickets.activity import ticket_activity
from ticketmind.tickets.enums import AgentAction, ProcessingRunStatus as RunStatus, TicketStatus
from ticketmind.tickets.models import ProcessingRecovery, ProcessingResult
from ticketmind.tickets.processing import check_replay, request_hash, require_ticket
from ticketmind.tickets.reviews import require_run


def _recover_output(factory, workflow, ticket_id, run_id, payload, actor, key):
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
                    kind = workflow.recovery_kind(run.thread_id, run.input_snapshot)
                    output = workflow.pending_output(run.thread_id) if kind == "review" else None
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


def recover_run(factory, workflow, ticket_id, run_id, payload, actor, key, *, runner_factory=None):
    """Only explicit reviewer recovery may continue compute; never apply a review."""
    if actor.role != "reviewer":
        raise AppError(403, "reviewer_required", "恢复运行需要 reviewer 权限")
    if runner_factory is None:
        return _recover_output(factory, workflow, ticket_id, run_id, payload, actor, key)
    digest = request_hash(payload)
    with ticket_activity(ticket_id, recovery=True):
        with factory() as session, session.begin():
            ticket = require_ticket(session, ticket_id, lock=True)
            run = require_run(session, ticket_id, run_id)
            claim = session.scalar(select(ProcessingRecovery).where(
                ProcessingRecovery.run_id == run_id, ProcessingRecovery.actor_id == actor.actor_id,
                ProcessingRecovery.idempotency_key == key))
            if claim:
                check_replay(claim, digest)
                if claim.resulting_status != RunStatus.RUNNING.value:
                    return RunRead.model_validate(run)
            if ticket.version != payload.expected_version:
                raise AppError(409, "version_conflict", "工单版本已变化，请重新读取")
            before = run.run_status
            if session.scalar(select(ProcessingResult.id).where(
                ProcessingResult.ticket_id == ticket_id, ProcessingResult.id != run_id,
                ProcessingResult.run_status.in_([RunStatus.RUNNING, RunStatus.WAITING_REVIEW]))):
                raise AppError(409, "active_run_exists", "已有另一条有效运行，不能恢复旧运行")
            compute = False
            if before in (RunStatus.RUNNING, RunStatus.FAILED) and not run.review:
                if ticket.status != TicketStatus.OPEN or ticket.version != run.ticket_version:
                    run.run_status, run.completed_at = RunStatus.CANCELLED, datetime.now(UTC)
                    run.error_code, run.error_summary = "proposal_invalidated", "工单已变化，旧运行不能恢复"
                else:
                    if workflow is None:
                        raise AppError(503, "checkpoint_unavailable", "检查点服务不可用")
                    try:
                        kind = workflow.recovery_kind(run.thread_id, run.input_snapshot)
                    except (ValueError, KeyError, TypeError, RuntimeError):
                        raise AppError(409, "checkpoint_invalid", "检查点不合法，禁止重新计算") from None
                    except Exception:
                        raise AppError(503, "checkpoint_unavailable", "无法读取检查点，请稍后重试") from None
                    if kind == "review":
                        _store_output(run, workflow.pending_output(run.thread_id))
                    elif kind == "compute":
                        checkpoint = workflow.inspect_compute(run.thread_id)
                        snapshot = run.input_snapshot
                        contract = snapshot.get("runtime_contract")
                        if (not contract or contract.get("agent_version") != run.agent_version or
                            contract.get("corpus_version") != run.corpus_version or
                            contract.get("retrieval_mode") != run.retrieval_mode or
                            contract.get("model_config") != run.model_config or
                            snapshot.get("agent_version") != run.agent_version or
                            snapshot.get("corpus_version") != run.corpus_version or
                            snapshot.get("retrieval_mode") != run.retrieval_mode or
                            snapshot.get("execution_limits") != run.model_config.get("limits") or
                            snapshot.get("run_id") != str(run.id) or snapshot.get("ticket_id") != str(ticket.id) or
                            snapshot.get("ticket_version") != run.ticket_version or
                            run.thread_id != f"ticket:{ticket.id}:run:{run.id}"):
                            raise AppError(409, "recovery_contract_invalid", "缺少一致的冻结执行配置，禁止续算")
                        elapsed = checkpoint.values.get("agent_data", {}).get("compute_elapsed_seconds", 0)
                        observation = checkpoint.values.get("failure_observation")
                        if observation is None:
                            known = (run.usage or {}).get("execution_budget", {})
                            if (known.get("node") == checkpoint.next[0] and known.get("checkpoint_id") ==
                                    checkpoint.config["configurable"]["checkpoint_id"]):
                                observed = known.get("total_elapsed_seconds", known.get("observed_elapsed_seconds"))
                                if type(observed) in (int, float) and math.isfinite(observed) and observed >= elapsed:
                                    observation = {**known, "failed_attempts": (run.usage or {}).get("failed_attempts", [])}
                        if observation:
                            elapsed = max(elapsed, observation.get("total_elapsed_seconds", observation["observed_elapsed_seconds"]))
                        else:
                            try:
                                workflow.unknown_timeout(checkpoint)
                            except RuntimeError:
                                raise AppError(409, "unknown_compute_elapsed", "无法可靠确定未完成调用的冻结时间上限，禁止续算") from None
                        thread_id, retrieval_mode = run.thread_id, run.retrieval_mode
                        run.run_status, run.completed_at = RunStatus.RUNNING, None
                        run.error_code, run.error_summary = "explicit_recovery_running", "reviewer 已显式认领计算恢复"
                        compute = True
                    else:
                        run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
                        run.error_code = "agent_fatal_failure" if kind == "fatal" else "execution_interrupted"
                        run.error_summary = "没有合法可恢复计算或提案；请人工接管"
                        if kind == "fatal":
                            budget = workflow.terminal_budget(run.thread_id)
                            if budget is not None:
                                run.error_code = "execution_budget_exhausted"
                                run.usage = {"execution_budget": budget}
            elif run.review and run.review.applied_at is None and before == RunStatus.RUNNING:
                run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
                run.error_code, run.error_summary = "review_interrupted", "请重试原不可变审核请求"
            if claim is None:
                claim = ProcessingRecovery(run_id=run_id, actor_id=actor.actor_id, idempotency_key=key,
                    request_hash=digest, previous_status=before.value, resulting_status=run.run_status.value)
                session.add(claim)
            else:
                claim.resulting_status = run.run_status.value
            session.flush()
            if not compute:
                return RunRead.model_validate(run)
            claim_id = claim.id
        # No business Session/connection is held during any external capability.
        output, failure = None, None
        try:
            runner = runner_factory(retrieval_mode)
            if not isinstance(runner, AgentRunner) or runner.recovery_contract != contract:
                raise AppError(409, "recovery_configuration_changed", "Agent 或知识/检索配置已变化，禁止续算")
            output = workflow.continue_compute(thread_id, runner, observation=observation)
        except Exception as exc:
            failure = exc
        with factory() as session, session.begin():
            ticket = require_ticket(session, ticket_id, lock=True)
            run = require_run(session, ticket_id, run_id)
            claim = session.get(ProcessingRecovery, claim_id)
            active = session.scalar(select(ProcessingResult.id).where(
                ProcessingResult.ticket_id == ticket_id, ProcessingResult.id != run_id,
                ProcessingResult.run_status.in_([RunStatus.RUNNING, RunStatus.WAITING_REVIEW])))
            if (ticket.version != run.ticket_version or ticket.status != TicketStatus.OPEN or
                run.run_status != RunStatus.RUNNING or run.review or active):
                if run.run_status == RunStatus.RUNNING:
                    run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
                    run.error_code, run.error_summary = "version_conflict", "工单或运行在恢复期间变化，结果未进入待审核"
            elif failure is not None:
                run.run_status, run.completed_at = RunStatus.FAILED, datetime.now(UTC)
                run.error_code = failure.code if isinstance(failure, AppError) else "agent_execution_failed"
                run.error_summary = "恢复未成功；请检查配置、服务和检查点诊断"
                if isinstance(failure, RunFailure):
                    if isinstance(failure.__cause__, TimeoutError) and str(failure.__cause__) == "处理时间预算已耗尽":
                        run.error_code = "execution_budget_exhausted"
                    run.usage, run.retrieval_evidence = failure.usage, failure.evidence
                    run.tool_calls = failure.partial.get("tool_calls", [])
            else:
                _store_output(run, output)
            claim.resulting_status = run.run_status.value
            session.flush()
            result = RunRead.model_validate(run)
        if isinstance(failure, AppError):
            raise failure
        return result


def _store_output(run, output):
    proposal = proposal_adapter.validate_python(output.state["proposal"])
    run.proposal = proposal.model_dump(mode="json")
    run.action = {"propose_resolution": AgentAction.RESOLVE, "ask_clarification": AgentAction.ASK_CLARIFICATION,
                  "escalate": AgentAction.ESCALATE}[proposal.next_step]
    run.retrieval_evidence, run.usage = output.evidence, output.usage
    run.tool_calls = output.state.get("tool_calls", [])
    run.run_status, run.completed_at = RunStatus.WAITING_REVIEW, None
    run.error_code = run.error_summary = None
