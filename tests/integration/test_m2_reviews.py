"""Real PostgreSQL + checkpoint saver + ASGI HTTP. Agent output is synthetic."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import test_m1_api as m1
from ticketmind.agent.proposals import proposal_adapter
from ticketmind.api.schemas.runs import ReviewCreate
from ticketmind.core.config import AuthSettings
from ticketmind.main import create_app
from ticketmind.tickets import reviews
from ticketmind.tickets.enums import ProcessingRunStatus as RunStatus
from ticketmind.tickets.models import ProcessingResult, ProcessingReview, Ticket, TicketMessage
from ticketmind.tickets.processing import request_hash

pytestmark = m1.pytestmark
database = m1.database
setup = m1.setup


def reviewer_headers(client, key=None):
    return {"Authorization": "Bearer " + client.app.state.auth_settings.reviewer_token.get_secret_value(),
            "Idempotency-Key": key or uuid4().hex}


def review(client, ticket, run, *, key=None, decision="approve", **changes):
    return client.post(f'/tickets/{ticket["id"]}/runs/{run["id"]}/review',
        json={"decision": decision, "expected_version": ticket["version"], **changes}, headers=reviewer_headers(client, key))


def message(client, ticket, *, key=None, kind="customer_update", **changes):
    return client.post(f'/tickets/{ticket["id"]}/messages', json={"kind": kind, "body": "客户补充当前环境信息",
        "expected_version": ticket["version"], **changes}, headers={"Idempotency-Key": key or uuid4().hex})


@pytest.mark.parametrize("proposal, decision, status", [("ask_clarification", "approve", "awaiting_customer"),
    ("propose_resolution", "edit", "open"), ("escalate", "approve", "escalated"),
    ("propose_resolution", "escalate", "escalated")])
def test_review_paths_preserve_original_and_apply_once(setup, proposal, decision, status):
    client, runner, factory = setup
    proposal_data = {
        "next_step": proposal,
        "reason": "synthetic reason",
        "reply": "原始草稿",
        "evidence_ids": ["SYN-HIST-V2-007"],
        "questions": ["当前配置是什么？"] if proposal == "ask_clarification" else [],
    }
    if proposal == "propose_resolution":
        proposal_data["evidence_quotes"] = {
            "SYN-HIST-V2-007": runner.corpus.cases["SYN-HIST-V2-007"].resolution.summary
        }
    runner.proposal_override = proposal_adapter.validate_python(proposal_data)
    ticket = m1.create(client)
    run = m1.run(client, ticket).json()
    key = uuid4().hex
    changes = {"edited_reply": "人工修改后的建议", "comment": "核对适用环境"} if decision == "edit" else (
        {"comment": "需要人工核查"} if decision == "escalate" else {})
    first = review(client, ticket, run, key=key, decision=decision, **changes)
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["run_status"] == "completed" and result["review"]["applied_at"]
    assert result["proposal"] == run["proposal"] and result["final_reply"] == "原始草稿"
    second = review(client, ticket, run, key=key, decision=decision, **changes)
    assert second.status_code == 200 and second.json() == result
    assert review(client, ticket, run, decision=decision, **changes).status_code == 409
    detail = client.get(f'/tickets/{ticket["id"]}').json()
    assert detail["status"] == status and detail["version"] == 2 and len(detail["messages"]) == 2
    assert detail["messages"][-1]["id"] == result["published_message_id"]
    if decision == "edit":
        assert detail["messages"][-1]["body"] == "人工修改后的建议"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProcessingReview).where(ProcessingReview.run_id == UUID(run["id"]))) == 1
    if status == "open":
        response = client.post(f'/tickets/{ticket["id"]}/close', json={"expected_version": 2, "reason": "客户确认已解决"},
                               headers=reviewer_headers(client, key))
        assert response.json()["status"] == "resolved" and response.json()["version"] == 3
        replay = client.post(f'/tickets/{ticket["id"]}/close', json={"expected_version": 2, "reason": "客户确认已解决"},
                            headers=reviewer_headers(client, key))
        assert replay.status_code == 200 and replay.json()["version"] == 3
        assert message(client, {**ticket, "version": 3}).status_code == 409


def test_operator_cannot_review_reply_or_close_and_extra_fields_rejected(setup):
    client, _, _ = setup
    ticket = m1.create(client, requester_role="reviewer")
    run = m1.run(client, ticket).json()
    headers = {"Idempotency-Key": uuid4().hex}
    for path, payload in [
        (f'/tickets/{ticket["id"]}/runs/{run["id"]}/review', {"decision": "approve", "expected_version": 1}),
        (f'/tickets/{ticket["id"]}/messages', {"kind": "human_reply", "body": "test", "expected_version": 1}),
        (f'/tickets/{ticket["id"]}/close', {"reason": "test", "expected_version": 1})]:
        assert client.post(path, json=payload, headers=headers).status_code == 403
    assert review(client, ticket, run, decision="edit", edited_reply="changed").status_code == 422
    assert review(client, ticket, run, decision="escalate").status_code == 422
    assert review(client, ticket, run, thread_id="client-controlled").status_code == 422
    assert review(client, ticket, run, expected_version=2).status_code == 409
    other = m1.create(client)
    assert review(client, other, run).status_code == 404


def test_customer_update_cancels_old_plan_and_replay_precedes_version(setup):
    client, runner, _ = setup
    ticket = m1.create(client)
    old_run = m1.run(client, ticket).json()
    key = uuid4().hex
    added = message(client, ticket, key=key)
    assert added.status_code == 201
    assert message(client, ticket, key=key).json() == added.json()
    assert message(client, ticket, key=key, body="different").status_code == 409
    assert review(client, ticket, old_run).status_code == 409
    assert client.get(f'/tickets/{ticket["id"]}/runs/{old_run["id"]}').json()["run_status"] == "cancelled"
    current = client.get(f'/tickets/{ticket["id"]}').json()
    new_run = m1.run(client, current).json()
    assert new_run["run_status"] == "waiting_review" and new_run["thread_id"] != old_run["thread_id"]
    assert any("客户补充" in message.content for message in runner.inputs[-1].messages if message.role == "customer")


def test_clarification_rounds_use_only_applied_non_escalated_reviews(setup):
    client, runner, _ = setup
    ticket = m1.create(client)
    for round_number in range(2):
        result = m1.run(client, ticket).json()
        assert runner.clarification_rounds[-1] == round_number
        assert review(client, ticket, result).json()["run_status"] == "completed"
        awaiting = client.get(f'/tickets/{ticket["id"]}').json()
        assert m1.run(client, awaiting).status_code == 409
        assert message(client, awaiting).status_code == 201
        ticket = client.get(f'/tickets/{ticket["id"]}').json()
    m1.run(client, ticket)
    assert runner.clarification_rounds[-1] == 2


def test_escalated_ticket_accepts_human_reply_and_close_but_no_agent(setup):
    client, _, _ = setup
    ticket = m1.create(client)
    result = m1.run(client, ticket).json()
    assert review(client, ticket, result, decision="escalate", comment="人工介入").json()["run_status"] == "completed"
    ticket = client.get(f'/tickets/{ticket["id"]}').json()
    assert message(client, ticket).status_code == 201
    ticket = client.get(f'/tickets/{ticket["id"]}').json()
    assert ticket["status"] == "escalated" and m1.run(client, ticket).status_code == 409
    response = client.post(f'/tickets/{ticket["id"]}/messages', json={"kind": "human_reply", "body": "已人工核查",
        "expected_version": ticket["version"]}, headers=reviewer_headers(client))
    assert response.status_code == 201
    ticket = client.get(f'/tickets/{ticket["id"]}').json()
    assert client.post(f'/tickets/{ticket["id"]}/close', json={"expected_version": ticket["version"], "reason": "客户确认解决"},
                       headers=reviewer_headers(client)).json()["status"] == "resolved"


def test_application_failure_rolls_back_and_retries_finished_graph(setup, monkeypatch):
    client, runner, factory = setup
    ticket = m1.create(client)
    result = m1.run(client, ticket).json()
    append = reviews.append_message
    def fail_after_flush(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("simulated failure after message flush")
    monkeypatch.setattr(reviews, "append_message", fail_after_flush)
    key = uuid4().hex
    failed = review(client, ticket, result, key=key).json()
    assert failed["run_status"] == "failed" and failed["review"]["applied_at"] is None
    current = client.get(f'/tickets/{ticket["id"]}').json()
    assert current["version"] == 1 and len(current["messages"]) == 1
    assert m1.run(client, ticket).status_code == 409
    state = client.app.state.workflow.graph().get_state(client.app.state.workflow.config(result["thread_id"]))
    assert state.next == () and state.values["approved_review"]
    monkeypatch.setattr(reviews, "append_message", append)
    restored = review(client, ticket, result, key=key).json()
    assert restored["run_status"] == "completed" and runner.calls == 1
    assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 2


def test_new_message_invalidates_failed_saved_review(setup, monkeypatch):
    client, _, _ = setup
    ticket = m1.create(client)
    result = m1.run(client, ticket).json()
    def fail(*args):
        raise RuntimeError("simulated checkpoint failure")
    monkeypatch.setattr(client.app.state.workflow, "resume", fail)
    key = uuid4().hex
    assert review(client, ticket, result, key=key).json()["run_status"] == "failed"
    assert message(client, ticket).status_code == 201
    assert review(client, ticket, result, key=key).status_code == 409
    assert client.get(f'/tickets/{ticket["id"]}/runs/{result["id"]}').json()["run_status"] == "cancelled"


def test_concurrent_review_is_claimed_once_and_blocks_ticket_writes(setup, monkeypatch):
    client, runner, _ = setup
    ticket = m1.create(client)
    result = m1.run(client, ticket).json()
    entered, release = Event(), Event()
    original = client.app.state.workflow.resume
    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(client.app.state.workflow, "resume", blocked)
    key = uuid4().hex
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(review, client, ticket, result, key=key)
        try:
            assert entered.wait(5)
            assert review(client, ticket, result, key=key).json()["run_status"] == "running"
            assert review(client, ticket, result).status_code == 409
            assert message(client, ticket).status_code == 409
            assert m1.run(client, ticket).status_code == 409
        finally:
            release.set()
        assert pending.result().json()["run_status"] == "completed"
    assert runner.calls == 1 and len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 2


@pytest.mark.parametrize("phase", ["waiting", "compute_interrupted", "review_saved", "graph_finished"])
def test_fresh_app_restores_checkpoints_and_identifies_interruption(database, phase):
    _, factory, _ = database
    auth = AuthSettings(_env_file=None, operator_token="o" * 32, reviewer_token="r" * 32)
    runner = m1.SyntheticRunner(factory)
    key, payload = uuid4().hex, ReviewCreate(decision="approve", expected_version=1)
    with TestClient(create_app(session_factory=factory, runner=runner, auth_settings=auth)) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        ticket = m1.create(client)
        result = m1.run(client, ticket).json()
        if phase != "waiting":
            with factory() as session, session.begin():
                saved_run = session.get(ProcessingResult, UUID(result["id"]))
                saved_run.run_status = RunStatus.RUNNING
                if phase != "compute_interrupted":
                    saved_review = ProcessingReview(run_id=saved_run.id, reviewer_id="reviewer", idempotency_key=key,
                        request_hash=request_hash(payload), **payload.model_dump())
                    session.add(saved_review)
                    session.flush()
                    resume = reviews.review_payload(saved_review)
            if phase == "graph_finished":
                client.app.state.workflow.resume(result["thread_id"], resume)
    # New saver/pool/compiled graph; no runner is needed to review after restart.
    with TestClient(create_app(session_factory=factory, auth_settings=auth)) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        current = client.get(f'/tickets/{ticket["id"]}/runs/{result["id"]}').json()
        if phase == "compute_interrupted":
            assert current["run_status"] == "failed" and current["error_code"] == "execution_interrupted"
            assert review(client, ticket, result, key=key).status_code == 409
        else:
            assert current["run_status"] == ("waiting_review" if phase == "waiting" else "failed")
            if phase != "waiting":
                assert current["error_code"] == "review_interrupted"
            applied = review(client, ticket, result, key=key)
            assert applied.json()["run_status"] == "completed", applied.text
            assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 2
    assert runner.calls == 1


def test_second_instance_cannot_relabel_live_runs(database):
    _, factory, _ = database
    with TestClient(create_app(session_factory=factory)):
        with pytest.raises(RuntimeError, match="已有 TicketMind 实例"):
            with TestClient(create_app(session_factory=factory)):
                pytest.fail("second instance unexpectedly started")
