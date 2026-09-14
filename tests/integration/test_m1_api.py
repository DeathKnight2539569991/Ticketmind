"""Real PostgreSQL/transactions + HTTP ASGI, synthetic Agent output (no model calls)."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from ticketmind.agent.proposals import Clarification
from ticketmind.agent.runtime import RunOutput
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import AuthSettings, ProcessingSettings
from ticketmind.db.testing import isolated_database
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.main import create_app
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.tickets.enums import ProcessingRunStatus, TicketStatus
from ticketmind.tickets.models import ProcessingResult, Ticket, TicketMessage

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("TICKETMIND_RUN_DB_TESTS") != "1",
                                                         reason="set TICKETMIND_RUN_DB_TESTS=1 for real PostgreSQL")]


@pytest.fixture(scope="module")
def database():
    with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as database:
        yield database


class SyntheticRunner:
    def __init__(self, factory):
        self.factory, self.calls, self.snapshots = factory, 0, []
        self.corpus = load_sources(ProcessingSettings().corpus_path)
        self.metadata = {"agent_version": "m1-integration-double", "corpus_version": self.corpus.version,
                         "retrieval_mode": "dense", "model_config": {"synthetic_test_double": True}}
        self.error = False
        self.entered = self.release = None
        self.change_version = False
        self.proposal_override = None

    def __call__(self, snapshot):
        self.calls += 1
        self.snapshots.append(snapshot)
        with self.factory() as session, session.begin():
            # NOWAIT proves the processing transaction released its ticket row lock.
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            ticket = session.scalar(select(Ticket).where(Ticket.id == UUID(snapshot["ticket_id"])).with_for_update(nowait=True))
            assert session.scalar(select(ProcessingResult.run_status).where(ProcessingResult.ticket_id == ticket.id)
                                   .order_by(ProcessingResult.run_sequence.desc()).limit(1)) == ProcessingRunStatus.RUNNING
            if self.change_version:
                ticket.version += 1
        if self.entered:
            self.entered.set()
            assert self.release.wait(10)
        if self.error:
            raise RuntimeError("synthetic failure; credential must not leak: sk-private-test")
        case = self.corpus.cases["SYN-HIST-V2-007"]
        hit = RetrievalHit(source_id=case.source_id, text=build_case_text(case), score=0.5)
        proposal = Clarification(next_step="ask_clarification", reason="缺少必要环境信息",
                                  reply="请补充是否使用本机代理。", questions=["是否使用本机代理？"],
                                  evidence_ids=[case.source_id])
        proposal = self.proposal_override or proposal
        return RunOutput({"understanding": TicketUnderstanding(summary="synthetic test", error_codes=[], environment=[]),
                          "proposal": proposal}, self.corpus.evidence([hit]), {"synthetic_test_double": True})


@pytest.fixture
def setup(database):
    engine, factory, schema = database
    runner = SyntheticRunner(factory)
    auth = AuthSettings(_env_file=None, operator_token="operator-test-" + "x" * 32,
                         reviewer_token="reviewer-test-" + "y" * 32)
    app = create_app(session_factory=factory, runner=runner, auth_settings=auth)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + auth.operator_token.get_secret_value()
        yield client, runner, factory


def create(client, key=None, **changes):
    payload = {"subject": "从数据库读取的标题", "body": "从数据库读取的原始正文", "channel": "web", "requester_role": "reviewer"}
    payload.update(changes)
    response = client.post("/tickets", json=payload, headers={"Idempotency-Key": key or uuid4().hex})
    assert response.status_code in (200, 201), response.text
    ticket = client.get("/tickets/" + response.json()["id"]).json()
    return ticket


def run(client, ticket, key=None, **changes):
    payload = {"expected_version": ticket["version"], "trigger_message_id": ticket["messages"][-1]["id"]}
    payload.update(changes)
    return client.post(f'/tickets/{ticket["id"]}/runs', json=payload,
                        headers={"Idempotency-Key": key or uuid4().hex})


def test_http_to_run_and_database_are_consistent(setup):
    client, runner, factory = setup
    ticket = create(client)
    response = run(client, ticket)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["run_status"] == "waiting_review" and result["action"] == "ask_clarification"
    assert result["actor_id"] == "operator"  # requester_role cannot grant reviewer identity.
    assert runner.snapshots[0]["body"] == ticket["messages"][0]["body"]
    assert runner.snapshots[0]["subject"] == ticket["subject"]
    detail = client.get(f'/tickets/{ticket["id"]}').json()
    assert detail["status"] == "open" and detail["version"] == 1
    assert len(detail["messages"]) == 1 and detail["latest_run"]["id"] == result["id"]
    assert client.get(f'/tickets/{ticket["id"]}/runs/{result["id"]}').json() == result
    assert client.get(f'/tickets/{ticket["id"]}/runs').json()[0] == result
    source = client.get(f'/sources/SYN-HIST-V2-007?corpus_version={result["corpus_version"]}')
    assert source.status_code == 200 and source.json()["source"]["synthetic"] is True
    with factory() as session:
        saved = session.get(ProcessingResult, UUID(result["id"]))
        assert saved.proposal == result["proposal"]
        assert saved.retrieval_evidence == result["retrieval_evidence"]
        assert saved.input_snapshot["messages"][0]["body"] == ticket["messages"][0]["body"]


def test_idempotency_replays_before_version_check_and_without_runner(setup):
    client, runner, factory = setup
    ticket = create(client)
    key = uuid4().hex
    result = run(client, ticket, key).json()
    with factory() as session, session.begin():
        session.get(Ticket, UUID(ticket["id"])).version += 1
    client.app.state.runner = None
    # A replay never needs model configuration or new provider initialization.
    response = run(client, ticket, key)
    assert response.status_code == 200 and response.json()["id"] == result["id"]
    assert runner.calls == 1
    conflict = run(client, ticket, key, expected_version=2)
    assert conflict.status_code == 409 and conflict.json()["error_code"] == "idempotency_conflict"


def test_creation_replays_and_changed_body_conflicts(setup):
    client, _, factory = setup
    key = uuid4().hex
    first, second = create(client, key), create(client, key)
    assert first["id"] == second["id"]
    response = client.post("/tickets", json={"subject": "changed", "body": "changed", "channel": "web", "requester_role": "user"},
                            headers={"Idempotency-Key": key})
    assert response.status_code == 409
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.idempotency_key == key)) == 1


def test_failure_persists_and_replay_does_not_retry(setup):
    client, runner, factory = setup
    ticket = create(client)
    runner.error = True
    key = uuid4().hex
    response = run(client, ticket, key)
    assert response.status_code == 201
    assert response.json()["run_status"] == "failed"
    assert response.json()["error_code"] == "agent_execution_failed"
    assert "sk-private-test" not in response.text
    assert run(client, ticket, key).status_code == 200 and runner.calls == 1
    runner.error = False
    assert run(client, ticket).json()["run_status"] == "waiting_review"
    assert client.get(f'/tickets/{ticket["id"]}').json()["messages"] == ticket["messages"]


def test_version_and_trigger_ownership_and_extra_fields(setup):
    client, runner, _ = setup
    first, second = create(client), create(client)
    assert run(client, first, expected_version=2).status_code == 409
    assert run(client, first, trigger_message_id=second["messages"][0]["id"]).status_code == 404
    assert run(client, first, body="forged", actor_id="reviewer").status_code == 422
    result = run(client, first).json()
    assert client.get(f'/tickets/{second["id"]}/runs/{result["id"]}').status_code == 404
    assert client.get('/sources/SYN-HIST-V2-007?corpus_version=obsolete').status_code == 404
    assert runner.calls == 1


def test_same_ticket_concurrent_requests_and_active_unique_index(setup):
    client, runner, factory = setup
    ticket, key = create(client), uuid4().hex
    runner.entered, runner.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(run, client, ticket, key)
        try:
            assert runner.entered.wait(5)
            replay = run(client, ticket, key)
            assert replay.status_code == 200 and replay.json()["run_status"] == "running"
            assert run(client, ticket).status_code == 409
        finally:
            runner.release.set()
        assert future.result().json()["run_status"] == "waiting_review"
    assert runner.calls == 1
    with factory() as session:
        original = session.scalar(select(ProcessingResult).where(ProcessingResult.ticket_id == UUID(ticket["id"])))
        session.add(ProcessingResult(ticket_id=original.ticket_id, trigger_message_id=original.trigger_message_id,
                                      run_sequence=2, agent_version="unit", run_status=ProcessingRunStatus.RUNNING))
        with pytest.raises(IntegrityError):
            session.commit()


def test_final_version_check_prevents_waiting_review(setup):
    client, runner, _ = setup
    runner.change_version = True
    response = run(client, create(client))
    assert response.json()["run_status"] == "failed"
    assert response.json()["error_code"] == "version_conflict"


@pytest.mark.parametrize("status", [TicketStatus.ESCALATED, TicketStatus.RESOLVED])
def test_terminal_or_manual_ticket_cannot_run(setup, status):
    client, runner, factory = setup
    ticket = create(client)
    with factory() as session, session.begin():
        session.get(Ticket, UUID(ticket["id"])).status = status
    assert run(client, ticket).status_code == 409
    assert runner.calls == 0


def test_migration_preserves_legacy_rows_and_matches_models():
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from ticketmind.db.base import Base
    with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL"), revision="55ee2375ea43") as (engine, factory, schema):
        ticket_id, message_id = uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(text("""INSERT INTO tickets (id,ticket_number,subject,channel,priority,status,requester_role)
                VALUES (:id,'legacy-ticket','legacy subject','web','P3','open','legacy')"""), {"id": ticket_id})
            connection.execute(text("""INSERT INTO ticket_messages (id,ticket_id,sequence_number,author_type,body)
                VALUES (:id,:ticket_id,1,'customer','legacy body')"""), {"id": message_id, "ticket_id": ticket_id})
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            # Locked Alembic reports emulated Enum type-bound checks as removed.
            # Check these constraints explicitly; still reject all other schema drift.
            from sqlalchemy import inspect
            enum_checks = {"agent_action", "processing_run_status", "message_author_type",
                           "ticket_channel", "ticket_priority", "ticket_status"}
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert [item for item in differences if not (
                item[0] == "remove_constraint" and item[1].name in enum_checks)] == []
            for table in Base.metadata.tables.values():
                checks = {c["name"]: c["sqltext"] for c in inspect(connection).get_check_constraints(table.name)}
                for column in table.columns:
                    if getattr(column.type, "create_constraint", False):
                        assert all(f"'{value}'" in checks[column.type.name] for value in column.type.enums)
        with factory() as session:
            ticket = session.get(Ticket, ticket_id)
            assert ticket.subject == "legacy subject" and ticket.version == 1
            assert ticket.messages[0].body == "legacy body" and ticket.actor_id is None


def test_real_local_dependency_failure_is_persisted_without_model_calls(setup, monkeypatch):
    import socket
    from ticketmind.agent import runtime
    from ticketmind.core.config import QwenSettings, MilvusSettings
    client, _, factory = setup

    def forbidden(**kwargs):
        raise AssertionError("No model request is authorized in this test")

    monkeypatch.setattr(runtime, "understand_ticket", forbidden)
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))  # Own the port, but deliberately do not listen.
        port = unavailable.getsockname()[1]
        client.app.state.runner = runtime.AgentRunner(
            QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit-only", DASHSCOPE_WORKSPACE_ID="unit-only"),
            MilvusSettings(_env_file=None, uri=f"http://127.0.0.1:{port}", timeout_seconds=1), ProcessingSettings())
        response = run(client, create(client))
    result = response.json()
    assert response.status_code == 201 and result["run_status"] == "failed"
    assert "initialization" in result["error_summary"]
    with factory() as session:
        assert session.get(ProcessingResult, UUID(result["id"])).run_status == ProcessingRunStatus.FAILED


@pytest.mark.parametrize("next_step, action", [("propose_resolution", "resolve"), ("escalate", "escalate")])
def test_other_proposals_also_wait_for_review(setup, next_step, action):
    from ticketmind.agent.proposals import proposal_adapter
    client, runner, _ = setup
    runner.proposal_override = proposal_adapter.validate_python({
        "next_step": next_step, "reason": "unit test", "reply": "unit draft", "evidence_ids": ["SYN-HIST-V2-007"]})
    ticket = create(client)
    result = run(client, ticket).json()
    assert result["action"] == action and result["run_status"] == "waiting_review"
    assert client.get(f'/tickets/{ticket["id"]}').json()["status"] == "open"
