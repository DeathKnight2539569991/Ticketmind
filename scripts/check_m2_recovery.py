"""Real process termination + HTTP + PostgreSQL checkpoints; synthetic Agent, zero model calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import uvicorn

from ticketmind.agent.proposals import Clarification
from ticketmind.agent.review import ReviewWorkflow
from ticketmind.agent.runtime import RunOutput
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import AuthSettings, Settings
from ticketmind.db.testing import isolated_database
from ticketmind.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def mark_boundary(path):
    path.write_text("fault boundary reached", encoding="utf-8")
    Event().wait(120)  # Parent terminates only this test-owned process.
    raise RuntimeError("test termination did not occur")


def serve(args):
    if not re.fullmatch(r"tm_test_[0-9a-f]{32}", args.schema):
        raise ValueError("Only an isolated test schema is allowed")
    url = os.getenv("TICKETMIND_TEST_DATABASE_URL") or Settings().database_url.unicode_string()
    engine = create_engine(url, connect_args={"options": f"-csearch_path={args.schema}", "connect_timeout": 5})
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    boundary = ROOT / "data/cache/m2" / f"{args.schema}.boundary"

    class SyntheticRunner:
        metadata = {"agent_version": "m2-process-recovery-synthetic", "corpus_version": "synthetic-no-retrieval",
                    "retrieval_mode": "dense", "model_config": {"synthetic_test_double": True}}

        def __call__(self, snapshot):
            if args.phase == "compute_interrupted":
                mark_boundary(boundary)
            return RunOutput({"understanding": TicketUnderstanding(summary="合成测试输入", error_codes=[], environment=[]),
                "proposal": Clarification(next_step="ask_clarification", reason="合成测试缺少环境信息",
                    reply="请提供当前代理配置。", questions=["当前代理配置是什么？"])}, [], {"model_calls": 0})

    original = ReviewWorkflow.resume
    def faulted_resume(self, thread_id, review):
        if args.phase == "review_saved":
            mark_boundary(boundary)
        result = original(self, thread_id, review)
        if args.phase == "graph_finished":
            mark_boundary(boundary)
        return result
    ReviewWorkflow.resume = faulted_resume
    try:
        uvicorn.run(create_app(session_factory=factory, runner=SyntheticRunner(), auth_settings=AuthSettings(_env_file=None)),
                    host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    finally:
        engine.dispose()


def wait_until(predicate, message, timeout=25):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.1)
    raise RuntimeError(message)


def start(schema, phase, environment):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", "--schema", schema,
        "--phase", phase, "--port", str(port)], cwd=ROOT, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=40,
                         headers={"Authorization": "Bearer " + environment["TICKETMIND_OPERATOR_TOKEN"]})
    def ready():
        if process.poll() is not None:
            raise RuntimeError("Isolated HTTP test process exited during startup")
        try:
            return client.get("/health", timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False
    try:
        wait_until(ready, "HTTP process did not start")
    except BaseException:
        stop(process, client)
        raise
    return process, client


def stop(process, client):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    client.close()


def verify_phase(phase, environment):
    with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (engine, _, schema):
        boundary = ROOT / "data/cache/m2" / f"{schema}.boundary"
        process, client = start(schema, phase, environment)
        key, run_id = uuid4().hex, None
        headers = {"Authorization": "Bearer " + environment["TICKETMIND_REVIEWER_TOKEN"], "Idempotency-Key": key}
        review_payload = {"decision": "approve", "expected_version": 1}
        try:
            created = client.post("/tickets", json={"subject": "进程恢复验收（合成）", "body": "合成测试正文",
                "channel": "api", "requester_role": "reviewer"}, headers={"Idempotency-Key": uuid4().hex})
            assert created.status_code == 201
            ticket_id = created.json()["id"]
            ticket = client.get(f"/tickets/{ticket_id}").json()
            def create_run():
                return client.post(f"/tickets/{ticket_id}/runs", json={"expected_version": 1,
                    "trigger_message_id": ticket["messages"][0]["id"]}, headers={"Idempotency-Key": uuid4().hex})
            with ThreadPoolExecutor(1) as pool:
                if phase == "compute_interrupted":
                    pending = pool.submit(create_run)
                else:
                    result = create_run().json()
                    assert result["run_status"] == "waiting_review"
                    run_id = result["id"]
                    pending = None if phase == "waiting" else pool.submit(client.post,
                        f"/tickets/{ticket_id}/runs/{run_id}/review", json=review_payload, headers=headers)
                try:
                    if phase != "waiting":
                        wait_until(boundary.exists, "fault boundary was not reached")
                    run_id = client.get(f"/tickets/{ticket_id}/runs").json()[0]["id"]
                finally:
                    process.kill()
                    process.wait(timeout=10)
                if pending:
                    try:
                        pending.result(timeout=10)
                    except httpx.HTTPError:
                        pass
        finally:
            stop(process, client)
        process, client = start(schema, "normal", environment)
        try:
            restored = client.get(f"/tickets/{ticket_id}/runs/{run_id}").json()
            if phase == "compute_interrupted":
                assert restored["run_status"] == "failed" and restored["error_code"] == "execution_interrupted"
                final_count = 1
            else:
                assert restored["run_status"] == ("waiting_review" if phase == "waiting" else "failed")
                if phase != "waiting":
                    assert restored["error_code"] == "review_interrupted"
                response = client.post(f"/tickets/{ticket_id}/runs/{run_id}/review", json=review_payload, headers=headers)
                assert response.json()["run_status"] == "completed", response.text
                repeated = client.post(f"/tickets/{ticket_id}/runs/{run_id}/review", json=review_payload, headers=headers)
                assert repeated.status_code == 200 and repeated.json() == response.json()
                final_count = 2
            detail = client.get(f"/tickets/{ticket_id}").json()
            assert len(detail["messages"]) == final_count
            with engine.connect() as connection:
                count = connection.execute(text("SELECT count(*) FROM ticket_messages WHERE ticket_id=:id"), {"id": ticket_id}).scalar_one()
                assert count == final_count
            return {"phase": phase, "passed": True, "restart_status": restored["run_status"],
                    "restart_error_code": restored["error_code"], "message_count": final_count,
                    "model_calls": 0, "agent": "synthetic_test_double"}
        finally:
            stop(process, client)
            boundary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--phase", default="normal", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args)
        return
    report_dir = ROOT / "data/cache/m2"
    report_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TICKETMIND_OPERATOR_TOKEN=secrets.token_urlsafe(32),
                       TICKETMIND_REVIEWER_TOKEN=secrets.token_urlsafe(32),
                       TICKETMIND_OPERATOR_ID="operator", TICKETMIND_REVIEWER_ID="reviewer")
    results = []
    for phase in ("waiting", "compute_interrupted", "review_saved", "graph_finished"):
        result = verify_phase(phase, environment)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (report_dir / "process_recovery.json").write_text(json.dumps({"verification": "real_process_http_postgresql_synthetic_agent",
        "results": results, "model_calls": 0}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
