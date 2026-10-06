"""Abandoned compute can be terminated; live execution remains protected. No paid calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import test_m1_api as m1
from test_agent_recovery_postgres import AUTH, Capabilities, recover
from ticketmind.agent import runtime
from ticketmind.api.schemas.runs import RunCreate
from ticketmind.main import create_app
from ticketmind.tickets.enums import ProcessingRunStatus as RunStatus
from ticketmind.tickets.models import ProcessingResult, Ticket, TicketMessage
from ticketmind.tickets.processing import create_run
from ticketmind.tickets.reviews import recover_interrupted_runs

pytestmark = m1.pytestmark
database = m1.database


def cancel(client, ticket, run, *, key=None, reason="无法恢复，转为人工处理", version=None, reviewer=True):
    return client.post(f'/tickets/{ticket["id"]}/runs/{run["id"]}/cancel',
        json={"expected_version": version or ticket["version"], "reason": reason},
        headers={"Authorization": "Bearer " + ("r" if reviewer else "o") * 32,
                 "Idempotency-Key": key or uuid4().hex})


@pytest.mark.parametrize("node", ["bootstrap_retrieve", "search_cases", "search_docs"])
def test_hard_exit_cancel_unlocks_manual_handling_and_never_reexecutes(database, monkeypatch, node):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch, tool=None if node == "bootstrap_retrieve" else node)
    runner = caps.runner()
    original = runtime.AgentExecution.run_node
    class Crash(BaseException): pass
    def execute(self, current):
        if current == node:
            raise Crash()
        return original(self, current)
    monkeypatch.setattr(runtime.AgentExecution, "run_node", execute)
    app = create_app(session_factory=factory, runner=runner, auth_settings=AUTH)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        ticket = m1.create(client)
        with pytest.raises(Crash):
            create_run(factory, lambda: runner, UUID(ticket["id"]), RunCreate(expected_version=1,
                trigger_message_id=ticket["messages"][0]["id"]), "operator", uuid4().hex,
                workflow=app.state.workflow)
        recover_interrupted_runs(factory, app.state.workflow)
        with factory() as session:
            saved = session.scalar(select(ProcessingResult).where(ProcessingResult.ticket_id == UUID(ticket["id"])))
            run = {"id": str(saved.id), "thread_id": saved.thread_id}
            assert saved.run_status == RunStatus.RUNNING
        refused = recover(client, ticket, run)
        assert refused.status_code == 409 and refused.json()["error_code"] == "unknown_compute_elapsed"
        calls = dict(caps.calls)
        key = uuid4().hex
        cancelled = cancel(client, ticket, run, key=key)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["run_status"] == "cancelled"
        assert cancelled.json()["error_code"] == "run_cancelled_by_reviewer"
        assert cancelled.json()["published_message_id"] is None
        assert cancel(client, ticket, run, key=key).json() == cancelled.json()
        assert cancel(client, ticket, run, key=key, reason="different").status_code == 409
        current = client.get(f'/tickets/{ticket["id"]}').json()
        assert current["version"] == 2 and current["status"] == "open"
        audit = current["messages"][-1]
        assert audit["operation"] == "cancel_run" and audit["actor_id"] == "reviewer"
        assert audit["author_type"] == "system" and run["id"] in audit["body"]
        assert recover(client, current, run).json()["run_status"] == "cancelled"
        review = client.post(f'/tickets/{ticket["id"]}/runs/{run["id"]}/review',
            json={"decision": "approve", "expected_version": 2}, headers={
                "Authorization": "Bearer " + "r" * 32, "Idempotency-Key": uuid4().hex})
        assert review.status_code == 409
        # A service restart must not restore the cancelled checkpoint.
        recover_interrupted_runs(factory, app.state.workflow)
        assert caps.calls == calls
        for path, payload in [
            ("messages", {"kind": "human_reply", "body": "人工已接手", "expected_version": 2}),
            ("escalate", {"reason": "人工接管", "expected_version": 3}),
            ("close", {"reason": "人工确认解决", "expected_version": 4}),
        ]:
            response = client.post(f'/tickets/{ticket["id"]}/{path}', json=payload, headers={
                "Authorization": "Bearer " + "r" * 32, "Idempotency-Key": uuid4().hex})
            assert response.status_code in (200, 201), response.text
        assert client.get(f'/tickets/{ticket["id"]}').json()["status"] == "resolved"


@pytest.mark.parametrize("activity", ["compute", "recovery", "review"])
def test_cancellation_refuses_live_compute_recovery_and_review(database, monkeypatch, activity):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        ticket = m1.create(client)
        entered, release = Event(), Event()
        def blocked():
            entered.set()
            assert release.wait(10)
        if activity == "compute":
            caps.callback = blocked
            work = lambda: m1.run(client, ticket)
        elif activity == "recovery":
            run = m1.run(client, ticket).json()
            caps.callback = blocked
            work = lambda: recover(client, ticket, run)
        else:
            # Skip the initial synthetic failure to reach waiting_review.
            caps.calls["decision"] = 1
            run = m1.run(client, ticket).json()
            original_resume = client.app.state.workflow.resume
            def resume(*args):
                blocked()
                return original_resume(*args)
            monkeypatch.setattr(client.app.state.workflow, "resume", resume)
            work = lambda: client.post(f'/tickets/{ticket["id"]}/runs/{run["id"]}/review',
                json={"decision": "approve", "expected_version": 1}, headers={
                    "Authorization": "Bearer " + "r" * 32, "Idempotency-Key": uuid4().hex})
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(work)
            try:
                assert entered.wait(5)
                with factory() as session:
                    saved = session.scalar(select(ProcessingResult).where(ProcessingResult.ticket_id == UUID(ticket["id"])))
                    target = {"id": str(saved.id)}
                response = cancel(client, ticket, target)
                assert response.status_code == 409 and response.json()["error_code"] == "execution_in_progress"
                with factory() as session:
                    assert session.get(ProcessingResult, UUID(target["id"])).run_status == RunStatus.RUNNING
                    assert session.get(Ticket, UUID(ticket["id"])).version == 1
            finally:
                release.set()
            assert pending.result().status_code in (200, 201)


def test_cancel_permissions_version_rollback_and_cross_run_idempotency(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        with factory() as session, session.begin():
            session.get(ProcessingResult, UUID(run["id"])).run_status = RunStatus.RUNNING
        assert cancel(client, ticket, run, reviewer=False).status_code == 403
        assert cancel(client, ticket, run, version=999).json()["error_code"] == "version_conflict"
        # A late transaction failure cannot leave a cancelled run without audit.
        from ticketmind.tickets import cancellation
        append = cancellation.append_message
        def fail(*args, **kwargs):
            append(*args, **kwargs)
            raise RuntimeError("synthetic audit write failure")
        monkeypatch.setattr(cancellation, "append_message", fail)
        with pytest.raises(RuntimeError):
            cancel(client, ticket, run)
        with factory() as session:
            assert session.get(ProcessingResult, UUID(run["id"])).run_status == RunStatus.RUNNING
            assert session.get(Ticket, UUID(ticket["id"])).version == 1
            assert session.scalar(select(TicketMessage.id).where(TicketMessage.ticket_id == UUID(ticket["id"]),
                TicketMessage.operation == "cancel_run")) is None
        monkeypatch.setattr(cancellation, "append_message", append)
        key = uuid4().hex
        assert cancel(client, ticket, run, key=key).status_code == 200
        current = client.get(f'/tickets/{ticket["id"]}').json()
        # Cancelled checkpoint remains cancelled even when the original key is replayed.
        assert cancel(client, ticket, run).json()["error_code"] == "version_conflict"
        assert cancel(client, current, run).json()["error_code"] == "run_not_cancellable"
        assert m1.run(client, current).json()["error_code"] == "customer_trigger_required"
        response = client.post(f'/tickets/{ticket["id"]}/messages', json={
            "kind": "customer_update", "body": "新客户事实", "expected_version": 2},
            headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 201
        new_ticket = client.get(f'/tickets/{ticket["id"]}').json()
        another = m1.run(client, new_ticket).json()
        assert cancel(client, ticket, another, key=key).json()["error_code"] == "idempotency_conflict"


@pytest.mark.parametrize("state", [RunStatus.WAITING_REVIEW, RunStatus.COMPLETED, RunStatus.FAILED])
def test_cancel_does_not_change_other_lifecycle_states(database, monkeypatch, state):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        with factory() as session, session.begin():
            saved = session.get(ProcessingResult, UUID(run["id"]))
            saved.run_status = state
            if state == RunStatus.COMPLETED:
                from ticketmind.tickets.enums import AgentAction
                saved.action = AgentAction.ESCALATE
        response = cancel(client, ticket, run)
        assert response.status_code == 409 and response.json()["error_code"] == "run_not_cancellable"
        assert client.get(f'/tickets/{ticket["id"]}/runs/{run["id"]}').json()["run_status"] == state


def test_cancel_does_not_require_checkpoint_or_agent_configuration(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client.headers["Authorization"] = "Bearer " + "o" * 32
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        with factory() as session, session.begin():
            session.get(ProcessingResult, UUID(run["id"])).run_status = RunStatus.RUNNING
        client.app.state.workflow = None
        def forbidden(*args, **kwargs):
            pytest.fail("cancellation must not construct an Agent")
        monkeypatch.setattr("ticketmind.api.routes.runs.get_runner", forbidden)
        assert cancel(client, ticket, run).json()["run_status"] == "cancelled"
