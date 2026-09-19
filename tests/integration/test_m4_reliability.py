"""Real SDK -> isolated loopback fault endpoint -> real HTTP/PostgreSQL failure state.

No provider receives a request. Existing shared services are never interrupted.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep, monotonic
from uuid import UUID, uuid4

import pytest
import httpx
from sqlalchemy import text

import test_m1_api as m1
from test_m2_reviews import review
from ticketmind.agent.runtime import AgentRunner
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.llm import client as llm_client
from ticketmind.tickets.models import ProcessingResult

pytestmark = m1.pytestmark
database = m1.database
setup = m1.setup


@pytest.mark.parametrize("fault", ["timeout", "http_503", "invalid_json"])
def test_real_model_transport_fails_once_and_preserves_ticket(setup, monkeypatch, fault):
    client, _, factory = setup
    sends = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            sends.append(self.path)
            if fault == "timeout":
                sleep(0.4)
            status = 503 if fault == "http_503" else 200
            body = b'{"error":{"message":"synthetic-secret-do-not-expose"}}' if status == 503 else b"malformed JSON"
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    original = llm_client.OpenAI
    def local_sdk(**kwargs):
        assert kwargs["max_retries"] == 0
        kwargs.update(base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout=0.1,
                      http_client=httpx.Client(trust_env=False))
        return original(**kwargs)
    monkeypatch.setattr(llm_client, "OpenAI", local_sdk)
    from ticketmind.agent import runtime
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [])
    class UnusedMilvus:
        closed = False
        def close(self):
            self.closed = True
    milvus = UnusedMilvus()
    runner = AgentRunner(QwenSettings(_env_file=None, DASHSCOPE_API_KEY="synthetic-local-only", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"), ProcessingSettings(retrieval_mode="bm25"),
        milvus_factory=lambda _: milvus, session_factory=factory)
    client.app.state.runner = runner
    try:
        ticket, key = m1.create(client), uuid4().hex
        started = monotonic()
        response = m1.run(client, ticket, key=key)
        elapsed = monotonic() - started
        result = response.json()
        assert response.status_code == 201 and result["run_status"] == "failed", result
        assert result["error_code"] == "agent_execution_failed"
        assert "decision" in result["error_summary"]
        assert "synthetic-secret" not in response.text
        assert len(sends) == 1 and milvus.closed and elapsed < 10
        assert m1.run(client, ticket, key=key).json() == result and len(sends) == 1
        current = client.get(f'/tickets/{ticket["id"]}').json()
        assert current["status"] == "open" and current["version"] == 1 and len(current["messages"]) == 1
        with factory() as session:
            saved = session.get(ProcessingResult, UUID(result["id"]))
            assert saved.error_code == result["error_code"] and saved.proposal is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_missing_real_checkpoint_refuses_recompute_or_publish(setup):
    client, runner, factory = setup
    ticket = m1.create(client)
    run = m1.run(client, ticket).json()
    with factory() as session, session.begin():
        assert session.execute(text("SELECT current_schema()")).scalar_one().startswith("tm_test_")
        # Only this test's own thread is damaged, in its UUID schema.
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            session.execute(text(f"DELETE FROM {table} WHERE thread_id = :thread"), {"thread": run["thread_id"]})
    key = uuid4().hex
    for _ in range(2):
        failed = review(client, ticket, run, key=key).json()
        assert failed["run_status"] == "failed" and failed["review"]["applied_at"] is None
        assert failed["published_message_id"] is None
        current = client.get(f'/tickets/{ticket["id"]}').json()
        assert current["version"] == 1 and len(current["messages"]) == 1 and runner.calls == 1
    assert m1.run(client, ticket).status_code == 409


def test_finished_checkpoint_with_other_review_refuses_publication(setup):
    client, runner, _ = setup
    ticket = m1.create(client)
    run = m1.run(client, ticket).json()
    # This is corruption injection into our isolated test thread, not an authorized business review.
    client.app.state.workflow.resume(run["thread_id"], {"id": "synthetic-unrelated-review"})
    failed = review(client, ticket, run).json()
    assert failed["run_status"] == "failed" and failed["review"]["applied_at"] is None
    assert failed["published_message_id"] is None and runner.calls == 1
    current = client.get(f'/tickets/{ticket["id"]}').json()
    assert current["version"] == 1 and len(current["messages"]) == 1


def test_evaluation_export_keeps_synthetic_final_separate_from_absent_model_output(database, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import evaluate_m4
    from ticketmind.agent.dev_acceptance import AttemptLedger, CATEGORIES
    from ticketmind.evaluation.dataset import digest
    _, factory, _ = database
    synthetic = m1.SyntheticRunner(factory)
    @contextmanager
    def owned_database(_):
        yield database
    monkeypatch.setattr(evaluate_m4, "isolated_database", owned_database)
    monkeypatch.setattr(evaluate_m4, "CACHE", tmp_path)
    monkeypatch.setattr(evaluate_m4, "AgentRunner", lambda *args, **kwargs: synthetic)
    ledger = AttemptLedger(tmp_path / "attempts.json", {c: 0 for c in CATEGORIES})
    case = {"case_id": "synthetic-export-check", "input": {"subject": "测试读取真实快照", "body": "纯替身输出",
            "channel": "web", "requester_role": "operator"}, "forbidden_label_marker": "MUST_NOT_ENTER_AGENT"}
    result = evaluate_m4.execute_agent(case, QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
                                      ProcessingSettings(), ledger, "glm-5.2")
    assert result["status"] == "succeeded" and result["input_hash"] == digest(case["input"])
    assert result["raw_proposal"] is None and result["final_proposal"]["next_step"] == "ask_clarification"
    assert not ledger.data["attempts"] and synthetic.calls == 1
    assert "MUST_NOT_ENTER_AGENT" not in str(synthetic.inputs)
    assert result["business_review"] == "not_executed" and result["http_database_consistent"]
