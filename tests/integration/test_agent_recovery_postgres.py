"""Explicit recovery over real business PostgreSQL and PostgresSaver, no paid calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import test_m1_api as m1
from ticketmind.agent import runtime
from ticketmind.agent.runtime import AgentRunner
from ticketmind.api.schemas.runs import RunRecover
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.main import create_app
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.tickets.enums import ProcessingRunStatus as RunStatus
from ticketmind.tickets.models import ProcessingRecovery, ProcessingResult, Ticket
from ticketmind.tickets.recovery import recover_run

pytestmark = m1.pytestmark
database = m1.database
AUTH = AuthSettings(_env_file=None, operator_token="o" * 32, reviewer_token="r" * 32)


class Capabilities:
    def __init__(self, factory, monkeypatch, *, tool=None, failure=None, slow=False):
        self.factory, self.tool, self.failure, self.slow = factory, tool, failure, slow
        self.calls = {"retrieve": 0, "search": 0, "detail": 0, "decision": 0, "judge": 0}
        self.now, self.timeouts, self.callback = 0., [], None
        self.config = ProcessingSettings(_env_file=None, retrieval_mode="bm25")
        self.corpus = load_sources(self.config.corpus_path)
        self.case = next(iter(self.corpus.cases.values()))
        self.hit = RetrievalHit(source_id=self.case.source_id, text=build_case_text(self.case), score=.5)
        monkeypatch.setattr(runtime, "monotonic", lambda: self.now)
        monkeypatch.setattr(runtime, "retrieve_cases", self.retrieve)

    def retrieve(self, *args, **kwargs):
        key = "retrieve" if self.calls["retrieve"] == 0 else "search"
        self.calls[key] += 1
        if self.slow and key == "retrieve":
            self.now += 80
        return [self.hit]

    def decision(self, state, timeout, usage):
        self.calls["decision"] += 1
        self.timeouts.append(timeout)
        usage.setdefault("decisions", []).append({"total_tokens": 7})
        if self.callback:
            self.callback()
        if self.tool and self.calls["decision"] == 1:
            return {"next_step": self.tool, "reason": "inspect facts", **(
                {"query": "additional customer facts"} if self.tool == "search_cases" else
                {"source_id": self.case.source_id})}
        if self.calls["decision"] == (2 if self.tool else 1):
            self.now += 2
            raise self.failure or RuntimeError("synthetic interrupted decision")
        self.now += 1
        return {"next_step": "ask_clarification", "reason": "missing facts", "reply": "请补充代理配置"}

    def judge(self, *args):
        self.calls["judge"] += 1
        return {"violations": []}

    def runner(self):
        corpus = SimpleNamespace(version=self.corpus.version, cases=self.corpus.cases, evidence=self.corpus.evidence,
            get_case_detail=self.detail)
        return AgentRunner(QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
            MilvusSettings(_env_file=None, uri="http://unused.invalid"), self.config,
            corpus=corpus, decision_fn=self.decision, judge_fn=self.judge,
            milvus_factory=lambda _: SimpleNamespace(close=lambda: None))

    def detail(self, source_id):
        self.calls["detail"] += 1
        return self.corpus.get_case_detail(source_id)

    def app(self):
        return create_app(session_factory=self.factory, runner=self.runner(), auth_settings=AUTH)


def client_login(client):
    client.headers["Authorization"] = "Bearer " + "o" * 32


def recover(client, ticket, run, key=None, version=None):
    return client.post(f'/tickets/{ticket["id"]}/runs/{run["id"]}/recover',
        json={"expected_version": version or ticket["version"]}, headers={
            "Authorization": "Bearer " + "r" * 32, "Idempotency-Key": key or uuid4().hex})


@pytest.mark.parametrize("tool", [None, "search_cases", "get_case_detail"])
def test_explicit_api_recovery_skips_committed_nodes_and_review_is_exactly_once(database, monkeypatch, tool):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch, tool=tool)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        assert run["run_status"] == "failed"
    calls = dict(caps.calls)
    # Simulate business-save crash: startup must classify without capabilities.
    with factory() as session, session.begin():
        saved = session.get(ProcessingResult, UUID(run["id"]))
        saved.run_status, saved.usage = RunStatus.RUNNING, None
    with TestClient(caps.app()) as client:
        client_login(client)
        assert caps.calls == calls
        pending = client.get(f'/tickets/{ticket["id"]}/runs/{run["id"]}').json()
        assert pending["run_status"] == "running" and pending["error_code"] == "explicit_recovery_required"
        assert m1.run(client, ticket).status_code == 409
        key = uuid4().hex
        result = recover(client, ticket, run, key)
        assert result.status_code == 200, result.text
        result = result.json()
        assert result["run_status"] == "waiting_review" and result["published_message_id"] is None
        assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 1
        assert caps.calls == {"retrieve": 1, "search": int(tool == "search_cases"),
            "detail": int(tool == "get_case_detail"), "decision": 3 if tool else 2, "judge": 1}
        assert len(result["usage"]["decisions"]) == (2 if tool else 1)
        assert result["usage"]["failed_attempts"][0]["reported_usage"]["decisions"] == [{"total_tokens": 7}]
        assert result["usage"]["failed_attempts"][0]["provider_usage_unknown"] is False
        assert len(result["tool_calls"]) == (2 if tool else 1)
        count = dict(caps.calls)
        assert recover(client, ticket, run, key).json() == result
        assert recover(client, ticket, run, key, version=2).status_code == 409
        review_key = uuid4().hex
        headers = {"Authorization": "Bearer " + "r" * 32, "Idempotency-Key": review_key}
        path = f'/tickets/{ticket["id"]}/runs/{run["id"]}/review'
        first = client.post(path, json={"decision": "approve", "expected_version": 1}, headers=headers)
        assert first.status_code == 201, first.text
        assert client.post(path, json={"decision": "approve", "expected_version": 1}, headers=headers).json() == first.json()
        assert caps.calls == count
        assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 2


def test_known_failed_budget_survives_offline_and_connection_is_released(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch, slow=True)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        assert run["usage"]["execution_budget"]["observed_elapsed_seconds"] == 82
        caps.now += 10000
        def verify_unlocked():
            with factory() as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                saved = session.scalar(select(Ticket).where(Ticket.id == UUID(ticket["id"])).with_for_update(nowait=True))
                assert saved.version == 1
                # No checked-out business connection from recovery remains.
                assert session.get_bind().pool.checkedout() == 1
        caps.callback = verify_unlocked
        result = recover(client, ticket, run)
        assert result.status_code == 200, result.text
        assert result.json()["run_status"] == "waiting_review"
        assert caps.timeouts == [10, 8]
        checkpoint = client.app.state.workflow.graph().get_state(client.app.state.workflow.config(run["thread_id"]))
        assert checkpoint.values["agent_data"]["compute_elapsed_seconds"] == 83


@pytest.mark.parametrize("failure", [ValueError("bad protocol"), PermissionError("denied")])
def test_fatal_checkpoint_survives_business_save_crash_without_capabilities(database, monkeypatch, failure):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch, failure=failure)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        checkpoint = client.app.state.workflow.graph().get_state(client.app.state.workflow.config(run["thread_id"]))
        assert checkpoint.next == () and checkpoint.values["failure_descriptor"]["fatal"] is True
    count = dict(caps.calls)
    with factory() as session, session.begin():
        saved = session.get(ProcessingResult, UUID(run["id"]))
        saved.run_status, saved.usage, saved.error_code = RunStatus.RUNNING, None, None
    with TestClient(caps.app()) as client:
        client_login(client)
        result = recover(client, ticket, run)
        assert result.status_code == 200 and result.json()["run_status"] == "failed"
        assert result.json()["error_code"] == "agent_fatal_failure"
        assert caps.calls == count


@pytest.mark.parametrize("drift", ["decision_model", "retrieval_rrf_k", "max_agent_steps", "knowledge_dataset"])
def test_frozen_config_drift_refuses_before_capability(database, monkeypatch, drift):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        count = dict(caps.calls)
        changed = {"decision_model": "another-model", "retrieval_rrf_k": 61,
                   "max_agent_steps": 7, "knowledge_dataset": "another-dataset"}[drift]
        client.app.state.runner = caps.runner()
        client.app.state.runner.config = caps.config.model_copy(update={drift: changed})
        # Effective Decision settings are part of the contract too.
        if drift == "decision_model":
            client.app.state.runner.decision_settings = client.app.state.runner.decision_settings.model_copy(update={"model": changed})
        response = recover(client, ticket, run)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "recovery_configuration_changed"
        assert caps.calls == count


def test_live_recovery_concurrent_and_version_change_cannot_publish(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        entered, release = Event(), Event()
        def blocked():
            entered.set()
            assert release.wait(10)
            with factory() as session, session.begin():
                session.get(Ticket, UUID(ticket["id"])).version += 1
        caps.callback = blocked
        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(recover, client, ticket, run)
            try:
                assert entered.wait(5)
                response = recover(client, ticket, run)
                assert response.status_code == 409 and response.json()["error_code"] == "execution_in_progress"
            finally:
                release.set()
            result = pending.result().json()
        assert result["run_status"] == "failed" and result["error_code"] == "version_conflict"
        assert result["proposal"] is None and result["published_message_id"] is None
        assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 1


def test_claim_crash_same_key_can_resume(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    class Crash(BaseException):
        pass
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        workflow = client.app.state.workflow
        original = workflow.continue_compute
        monkeypatch.setattr(workflow, "continue_compute", lambda *args, **kwargs: (_ for _ in ()).throw(Crash()))
        key = uuid4().hex
        with pytest.raises(Crash):
            recover_run(factory, workflow, UUID(ticket["id"]), UUID(run["id"]),
                RunRecover(expected_version=1), SimpleNamespace(role="reviewer", actor_id="reviewer"), key,
                runner_factory=lambda mode: caps.runner())
        with factory() as session:
            claim = session.scalar(select(ProcessingRecovery).where(ProcessingRecovery.idempotency_key == key))
            assert claim.resulting_status == "running"
        monkeypatch.setattr(workflow, "continue_compute", original)
        response = recover(client, ticket, run, key)
        assert response.status_code == 200, response.text
        assert response.json()["run_status"] == "waiting_review"
        assert caps.calls["retrieve"] == 1 and caps.calls["decision"] == 2


@pytest.mark.parametrize("damage", ["missing", "snapshot", "budget", "observation", "contract", "unknown_elapsed",
                                   "steps", "rounds", "details"])
def test_invalid_checkpoint_refuses_capabilities(database, monkeypatch, damage):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        workflow = client.app.state.workflow
        graph, config = workflow.graph(), workflow.config(run["thread_id"])
        values = graph.get_state(config).values
        if damage == "missing":
            workflow.checkpointer.delete_thread(run["thread_id"])
        elif damage == "snapshot":
            values["snapshot"]["run_id"] = str(uuid4())
            graph.update_state(config, {"snapshot": values["snapshot"]})
        elif damage == "budget":
            values["agent_data"]["compute_elapsed_seconds"] = -1
            graph.update_state(config, {"agent_data": values["agent_data"]})
        elif damage == "observation":
            values["failure_observation"]["observed_elapsed_seconds"] = -1
            graph.update_state(config, {"failure_observation": values["failure_observation"]})
        elif damage == "unknown_elapsed":
            values["snapshot"]["runtime_contract"].pop("model_call_timeout_seconds")
            with factory() as session, session.begin():
                session.get(ProcessingResult, UUID(run["id"])).input_snapshot = values["snapshot"]
            graph.update_state(config, {"failure_observation": None, "snapshot": values["snapshot"]})
        elif damage in ("steps", "rounds", "details"):
            if damage == "steps":
                values["agent_data"]["agent_steps"] = 100
            elif damage == "rounds":
                values["agent_data"]["search_rounds"] = 10
            else:
                values["agent_data"]["detail_ids"] = ["unknown-source"]
                values["agent_data"]["case_details"] = {"unknown-source": {"text": "unknown"}}
            graph.update_state(config, {"agent_data": values["agent_data"]})
        else:
            with factory() as session, session.begin():
                saved = session.get(ProcessingResult, UUID(run["id"]))
                saved.model_config = {**saved.model_config, "decision_protocol": "different"}
        count = dict(caps.calls)
        response = recover(client, ticket, run)
        if damage == "missing":
            assert response.status_code == 200 and response.json()["run_status"] == "failed"
        else:
            assert response.status_code == 409, response.text
        assert caps.calls == count


def discard_observation(client, factory, run):
    workflow = client.app.state.workflow
    workflow.graph().update_state(workflow.config(run["thread_id"]), {"failure_observation": None})
    with factory() as session, session.begin():
        session.get(ProcessingResult, UUID(run["id"])).usage = None


@pytest.mark.parametrize("crash", [None, "prepared", "started"])
def test_unknown_model_timeout_charge_durable_and_attempt_identity(database, monkeypatch, crash):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    class Crash(BaseException): pass
    key = uuid4().hex
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        discard_observation(client, factory, run)
        workflow = client.app.state.workflow
        original = workflow._invoke
        node_original = runtime.AgentExecution.run_node
        if crash == "prepared":
            monkeypatch.setattr(workflow, "_invoke", lambda *args, **kwargs: (_ for _ in ()).throw(Crash()))
        elif crash == "started":
            monkeypatch.setattr(runtime.AgentExecution, "run_node", lambda *args, **kwargs: (_ for _ in ()).throw(Crash()))
        if crash:
            with pytest.raises(Crash):
                recover_run(factory, workflow, UUID(ticket["id"]), UUID(run["id"]), RunRecover(expected_version=1),
                    SimpleNamespace(role="reviewer", actor_id="reviewer"), key, runner_factory=lambda _: caps.runner())
            state = workflow.graph().get_state(workflow.config(run["thread_id"]))
            assert state.next == (("resume_gate",) if crash == "prepared" else ("decision",))
            assert state.values["agent_data"]["compute_estimated_seconds"] == 30
            monkeypatch.setattr(workflow, "_invoke", original)
            monkeypatch.setattr(runtime.AgentExecution, "run_node", node_original)
    caps.now += 10000
    with TestClient(caps.app()) as client:
        client_login(client)
        result = recover(client, ticket, run, key)
        assert result.status_code == 200, result.text
        assert result.json()["run_status"] == "waiting_review"
        budget = result.json()["usage"]["execution_budget"]
        assert budget["estimated_elapsed_seconds"] == (60 if crash == "started" else 30)
        assert budget["observed_elapsed_seconds"] == 1
        assert all(item["classification"] == "conservative/estimated" for item in budget["estimates"])
        assert len(budget["estimates"]) == (2 if crash == "started" else 1)
        calls = dict(caps.calls)
        assert recover(client, ticket, run, key).json() == result.json()
        assert caps.calls == calls and calls["retrieve"] == 1 and calls["decision"] == 2
        assert len(result.json()["tool_calls"]) == 1


@pytest.mark.parametrize("elapsed", [60, 80, 90])
def test_conservative_or_already_exhausted_budget_stops_all_capabilities(database, monkeypatch, elapsed):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        discard_observation(client, factory, run)
        graph = client.app.state.workflow.graph()
        config = client.app.state.workflow.config(run["thread_id"])
        data = graph.get_state(config).values["agent_data"]
        data.update(compute_elapsed_seconds=elapsed, compute_observed_seconds=elapsed)
        graph.update_state(config, {"agent_data": data})
        calls = dict(caps.calls)
        result = recover(client, ticket, run)
        assert result.status_code == 200, result.text
        assert result.json()["run_status"] == "failed" and result.json()["error_code"] == "execution_budget_exhausted"
        assert caps.calls == calls
        state = graph.get_state(config)
        assert state.next == () and state.values["failure_descriptor"]["fatal"]
        budget = result.json()["usage"]["execution_budget"]
        assert budget["estimated_elapsed_seconds"] == 90-elapsed
        assert budget["observed_elapsed_seconds"] == elapsed
        assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 1


def test_estimated_then_observed_failure_remains_separate(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        discard_observation(client, factory, run)
        def failed_attempt():
            caps.now += 2
            raise RuntimeError("observed second attempt")
        caps.callback = failed_attempt
        failed = recover(client, ticket, run).json()
        assert failed["run_status"] == "failed"
        assert failed["usage"]["execution_budget"]["observed_elapsed_seconds"] == 2
        assert failed["usage"]["execution_budget"]["estimated_elapsed_seconds"] == 30
        caps.callback = None
        caps.now += 10000
        result = recover(client, ticket, run).json()
        assert result["run_status"] == "waiting_review"
        budget = result["usage"]["execution_budget"]
        assert budget["observed_elapsed_seconds"] == 3
        assert budget["estimated_elapsed_seconds"] == 30
        assert budget["total_elapsed_seconds"] == 33
        assert len(budget["estimates"]) == 1
        assert caps.calls["retrieve"] == 1 and caps.calls["decision"] == 3


@pytest.mark.parametrize("restart", [False, True])
def test_budget_terminal_survives_business_save_crash(database, monkeypatch, restart):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        discard_observation(client, factory, run)
        workflow = client.app.state.workflow
        graph, config = workflow.graph(), workflow.config(run["thread_id"])
        data = graph.get_state(config).values["agent_data"]
        data.update(compute_elapsed_seconds=80, compute_observed_seconds=80)
        graph.update_state(config, {"agent_data": data})
        result = recover(client, ticket, run).json()
        assert result["error_code"] == "execution_budget_exhausted"
        budget = result["usage"]["execution_budget"]
        with factory() as session, session.begin():
            saved = session.get(ProcessingResult, UUID(run["id"]))
            saved.run_status, saved.usage, saved.error_code = RunStatus.RUNNING, None, None
        calls = dict(caps.calls)
        if not restart:
            restored = recover(client, ticket, run).json()
            assert restored["run_status"] == "failed"
            assert restored["error_code"] == "execution_budget_exhausted"
            assert restored["usage"]["execution_budget"] == budget
            assert caps.calls == calls
            return
    with TestClient(caps.app()) as client:
        client_login(client)
        restored = client.get(f'/tickets/{ticket["id"]}/runs/{run["id"]}').json()
        assert restored["run_status"] == "failed"
        assert restored["error_code"] == "execution_budget_exhausted"
        assert restored["usage"]["execution_budget"] == budget
        assert caps.calls == calls


def test_diagnostic_write_failure_uses_matching_business_observation(database, monkeypatch):
    from ticketmind.agent.review import ReviewWorkflow
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch, slow=True)
    original = ReviewWorkflow.graph
    def graph_without_diagnostic(self, *args, **kwargs):
        graph = original(self, *args, **kwargs)
        update = graph.update_state
        def write(config, values, *args, **kwargs):
            if "failure_observation" in values and "agent_data" not in values:
                raise RuntimeError("diagnostic persistence unavailable")
            return update(config, values, *args, **kwargs)
        graph.update_state = write
        return graph
    monkeypatch.setattr(ReviewWorkflow, "graph", graph_without_diagnostic)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        assert run["usage"]["execution_budget"]["observed_elapsed_seconds"] == 82
        monkeypatch.setattr(ReviewWorkflow, "graph", original)
        workflow = client.app.state.workflow
        assert workflow.graph().get_state(workflow.config(run["thread_id"])).values.get("failure_observation") is None
        result = recover(client, ticket, run)
        assert result.status_code == 200, result.text
        assert result.json()["run_status"] == "waiting_review"
        assert caps.timeouts == [10, 8]
        assert caps.calls["retrieve"] == 1


def test_second_active_run_refuses_recovery_before_capabilities(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        old = m1.run(client, ticket).json()
        active = m1.run(client, ticket).json()
        assert active["run_status"] == "waiting_review"
        calls = dict(caps.calls)
        response = recover(client, ticket, old)
        assert response.status_code == 409 and response.json()["error_code"] == "active_run_exists"
        assert caps.calls == calls


@pytest.mark.parametrize("node", ["retrieve", "search_cases", "get_case_detail"])
def test_unknown_compound_call_refuses_without_capability(database, monkeypatch, node):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        if node == "retrieve":
            monkeypatch.setattr(runtime, "retrieve_cases", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("interrupted retrieval")))
        run = m1.run(client, ticket).json()
        workflow = client.app.state.workflow
        graph, config = workflow.graph(), workflow.config(run["thread_id"])
        if node != "retrieve":
            data = graph.get_state(config).values["agent_data"]
            data["decision_result"] = {"next_step": node, "reason": "inspect facts", **(
                {"query": "additional customer facts"} if node == "search_cases" else {"source_id": caps.case.source_id})}
            graph.update_state(config, {"agent_data": data, "failure_observation": None}, as_node="decision")
            with factory() as session, session.begin():
                session.get(ProcessingResult, UUID(run["id"])).usage = None
        else:
            discard_observation(client, factory, run)
        assert graph.get_state(config).next == (node,)
        calls = dict(caps.calls)
        response = recover(client, ticket, run)
        assert response.status_code == 409 and response.json()["error_code"] == "unknown_compute_elapsed", (response.text, graph.get_state(config).next)
        assert caps.calls == calls


@pytest.mark.parametrize("node", ["judge", "repair"])
def test_unknown_judge_and_repair_restore_original_node(database, monkeypatch, node):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    class Crash(BaseException): pass
    original = runtime.AgentExecution.run_node
    def crash_node(self, current):
        if current == node:
            raise Crash()
        return original(self, current)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        if node == "repair":
            # Reject only the first candidate; final-only repair then passes.
            caps.judge = lambda *args: {"violations": [{"type": "repeated_known_fact", "text": "代理配置", "reason": "test rejection"}]}
        monkeypatch.setattr(runtime.AgentExecution, "run_node", crash_node)
        workflow = client.app.state.workflow
        with pytest.raises(Crash):
            recover_run(factory, workflow, UUID(ticket["id"]), UUID(run["id"]), RunRecover(expected_version=1),
                SimpleNamespace(role="reviewer", actor_id="reviewer"), uuid4().hex, runner_factory=lambda _: caps.runner())
        assert workflow.graph().get_state(workflow.config(run["thread_id"])).next == (node,)
        discard_observation(client, factory, run)
        monkeypatch.setattr(runtime.AgentExecution, "run_node", original)
        caps.judge = lambda *args: {"violations": []}
    with TestClient(caps.app()) as client:
        client_login(client)
        response = recover(client, ticket, run)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["run_status"] == "waiting_review"
        assert result["usage"]["execution_budget"]["estimated_elapsed_seconds"] == 30
        assert caps.calls["retrieve"] == 1


def test_multiple_known_initial_failures_accumulate_active_time_and_attempt_diagnostics(database, monkeypatch):
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    calls, timeouts = [0], []
    def retrieval(*args, **kwargs):
        calls[0] += 1
        timeouts.append(kwargs["timeout"]())
        caps.now += 2 if calls[0] <= 2 else 1
        if calls[0] <= 2:
            raise RuntimeError("synthetic transient retrieval")
        return []
    monkeypatch.setattr(runtime, "retrieve_cases", retrieval)
    caps.calls["decision"] = 1  # this test fails only in retrieval
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        assert run["usage"]["execution_budget"]["observed_elapsed_seconds"] == 2
        caps.now += 10000
        failed = recover(client, ticket, run)
        assert failed.status_code == 200 and failed.json()["run_status"] == "failed"
        assert failed.json()["usage"]["execution_budget"]["observed_elapsed_seconds"] == 4
        caps.now += 10000
        output = recover(client, ticket, run)
        assert output.status_code == 200, output.text
        assert output.json()["run_status"] == "waiting_review"
        checkpoint = client.app.state.workflow.graph().get_state(client.app.state.workflow.config(run["thread_id"]))
        assert checkpoint.values["agent_data"]["compute_elapsed_seconds"] == 6
        assert len(output.json()["tool_calls"]) == 1
        attempts = output.json()["usage"]["failed_attempts"]
        assert len(attempts) == 2 and all(attempt["provider_usage_unknown"] for attempt in attempts)
        assert attempts[0]["checkpoint_id"] != attempts[1]["checkpoint_id"]
        assert calls[0] == 3 and timeouts == [10, 10, 10]


@pytest.mark.parametrize("retrieval_error", ["corpus_version_mismatch", "bm25_invalid_response", "wrapped_auth"])
def test_fatal_retrieval_configuration_protocol_and_wrapped_auth(database, monkeypatch, retrieval_error):
    from ticketmind.retrieval.schemas import RetrievalError
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    def failure(*args, **kwargs):
        if retrieval_error == "wrapped_auth":
            try:
                raise PermissionError("denied")
            except PermissionError as exc:
                raise RetrievalError("bm25_retrieval_failed") from exc
        raise RetrievalError(retrieval_error)
    monkeypatch.setattr(runtime, "retrieve_cases", failure)
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        checkpoint = client.app.state.workflow.graph().get_state(client.app.state.workflow.config(run["thread_id"]))
        assert checkpoint.next == () and checkpoint.values["failure_descriptor"]["fatal"]
        assert recover(client, ticket, run).json()["error_code"] == "agent_fatal_failure"
        assert caps.calls["decision"] == caps.calls["judge"] == 0


@pytest.mark.parametrize("boundary", ["review", "compute"])
def test_real_legacy_topology_checkpoint_compatibility(database, monkeypatch, boundary):
    from typing import TypedDict
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt
    _, factory, _ = database
    caps = Capabilities(factory, monkeypatch)
    class LegacyState(TypedDict, total=False):
        snapshot: dict
        output: dict
        approved_review: dict
    proposal = {"next_step": "ask_clarification", "reason": "missing facts", "reply": "请补充配置", "evidence_ids": []}
    output = {"state": {"proposal": proposal, "tool_calls": []}, "evidence": [], "usage": {}}
    def old_compute(state):
        if boundary == "compute":
            raise RuntimeError("old compute interruption")
        return {"output": output}
    def old_review(state):
        return {"approved_review": interrupt({"run_id": state["snapshot"]["run_id"], "proposal": state["output"]["state"]["proposal"]})}
    with TestClient(caps.app()) as client:
        client_login(client)
        ticket = m1.create(client)
        run = m1.run(client, ticket).json()
        saver = client.app.state.workflow.checkpointer
        saver.delete_thread(run["thread_id"])
        with factory() as session, session.begin():
            saved = session.get(ProcessingResult, UUID(run["id"]))
            snapshot = dict(saved.input_snapshot)
            snapshot.pop("runtime_contract")
            saved.input_snapshot = snapshot
            saved.run_status, saved.usage = RunStatus.RUNNING, None
        builder = StateGraph(LegacyState)
        builder.add_node("compute", old_compute)
        builder.add_node("review", old_review)
        builder.add_edge(START, "compute")
        builder.add_edge("compute", "review")
        builder.add_edge("review", END)
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": run["thread_id"]}}
        if boundary == "compute":
            with pytest.raises(RuntimeError):
                graph.invoke({"snapshot": snapshot}, config, durability="sync")
        else:
            graph.invoke({"snapshot": snapshot}, config, durability="sync")
    count = dict(caps.calls)
    with TestClient(caps.app()) as client:
        client_login(client)
        current = client.get(f'/tickets/{ticket["id"]}/runs/{run["id"]}').json()
        assert current["run_status"] == ("waiting_review" if boundary == "review" else "failed")
        if boundary == "review":
            key = uuid4().hex
            path = f'/tickets/{ticket["id"]}/runs/{run["id"]}/review'
            headers = {"Authorization": "Bearer " + "r" * 32, "Idempotency-Key": key}
            request = {"decision": "approve", "expected_version": 1}
            first = client.post(path, json=request, headers=headers)
            assert first.status_code == 201, first.text
            assert client.post(path, json=request, headers=headers).json() == first.json()
            assert len(client.get(f'/tickets/{ticket["id"]}').json()["messages"]) == 2
        else:
            assert recover(client, ticket, run).json()["run_status"] == "failed"
        assert caps.calls == count
