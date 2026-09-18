"""Synthetic transport tests for acceptance budgets; never real model evidence."""
import importlib.util
import json
from pathlib import Path

import pytest

from ticketmind.agent import dev_acceptance as acceptance
from ticketmind.agent.decide import decision_messages
from ticketmind.agent.dev_acceptance import AcceptanceAdapters, AttemptLedger, CATEGORIES, acceptance_lock
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import QwenSettings


@pytest.fixture
def settings():
    return QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit-only", DASHSCOPE_WORKSPACE_ID="unit-only")


def ledger(path, **limits):
    return AttemptLedger(path / "attempts.json", {c: limits.get(c, 0) for c in CATEGORIES})


def state():
    return {"subject": "unit", "body": "unit", "agent_steps": 3, "retrieval_hits": [],
            "understanding": TicketUnderstanding(summary="unit", error_codes=[], environment=[]),
            "execution_limits": {"max_agent_steps": 3, "max_search_rounds": 1}}


def test_zero_budget_never_calls_and_categories_cannot_borrow(tmp_path):
    budget = ledger(tmp_path, decision=1)
    for category in CATEGORIES:
        if category != "decision":
            with pytest.raises(RuntimeError, match="额度|授权"):
                budget.attempt(category, "fp", lambda record: pytest.fail("unexpected call"))
    assert budget.data["attempts"] == []


def test_attempt_is_durable_before_send_and_failure_is_not_refunded(tmp_path):
    budget = ledger(tmp_path, decision=1)
    def fail(record):
        assert json.loads(budget.path.read_text())["attempts"][0]["status"] == "started"
        raise TimeoutError("synthetic failure")
    with pytest.raises(TimeoutError):
        budget.attempt("decision", "fp1", fail)
    resumed = ledger(tmp_path, decision=1)
    with pytest.raises(RuntimeError, match="自动重试"):
        resumed.attempt("decision", "fp1", lambda r: pytest.fail("retry"))
    with pytest.raises(RuntimeError, match="额度"):
        resumed.attempt("decision", "fp2", lambda r: pytest.fail("reset"))


def test_started_attempt_after_crash_blocks_retry(tmp_path):
    acceptance.write_json(tmp_path / "attempts.json", {"attempts": [
        {"category": "decision", "fingerprint": "fp", "status": "started"}]})
    with pytest.raises(RuntimeError, match="自动重试"):
        ledger(tmp_path, decision=2).attempt("decision", "fp", lambda r: pytest.fail("retry"))


def test_single_acceptance_session_lock(tmp_path):
    with acceptance_lock(tmp_path):
        with pytest.raises(FileExistsError):
            with acceptance_lock(tmp_path):
                pytest.fail("concurrent acceptance")
    assert not (tmp_path / "session.lock").exists()


@pytest.mark.parametrize("content", [
    '{"next_step":"ask_clarification","reason":"unit","reply":"请提供现有配置。","questions":["当前配置是什么？","当前配置是什么？"]}',
    '{"next_step":',
    '{"reason":"需要更多证据","query":"只读查询超时","missing_evidence":"适用案例"}',
])
def test_rejected_raw_decision_saved_and_replayed_without_new_call(tmp_path, settings, monkeypatch, content):
    sends = []
    def send(**kwargs):
        sends.append(True)
        kwargs["response_callback"]({"request_id": "synthetic", "usage": {"total_tokens": 10},
                                     "choices": [{"content": content, "finish_reason": "stop"}]})
        return content
    monkeypatch.setattr(acceptance, "generate_text", send)
    adapter = AcceptanceAdapters(settings, tmp_path, ledger(tmp_path, decision=1))
    with pytest.raises(ValueError):
        adapter.decision(state(), 1, {})
    raw = json.loads(next((tmp_path / "decision").glob("*.json")).read_text(encoding="utf-8"))
    assert raw["response"]["choices"][0]["content"] == content
    replay = AcceptanceAdapters(settings, tmp_path, ledger(tmp_path))
    usage = {}
    with pytest.raises(ValueError):
        replay.decision(state(), 1, usage)
    assert usage["acceptance_decisions"][0]["validation"]["status"] == "rejected"
    if '"next_step"' not in content:
        error = usage["acceptance_decisions"][0]["validation"]["errors"][0]
        assert error["type"] == "union_tag_not_found" and "next_step" in error["message"]
    assert len(sends) == 1 and len(replay.ledger.cache_hits) == 1


def test_multiround_decisions_use_separate_fingerprints_and_one_total_ceiling(tmp_path, settings, monkeypatch):
    content = '{"next_step":"escalate","reason":"unit","reply":"unit"}'
    def send(**kwargs):
        kwargs["response_callback"]({"request_id": "unit", "usage": None,
            "choices": [{"content": content, "finish_reason": "stop"}]})
        return content
    monkeypatch.setattr(acceptance, "generate_text", send)
    adapter = AcceptanceAdapters(settings, tmp_path, ledger(tmp_path, decision=2))
    for step in (3, 5):
        adapter.decision({**state(), "agent_steps": step}, 1, {})
    with pytest.raises(RuntimeError, match="额度"):
        adapter.decision({**state(), "agent_steps": 7}, 1, {})
    assert len(list((tmp_path / "decision").glob("*.json"))) == 2


def test_embedding_research_has_independent_ceiling_and_immediate_cache(tmp_path, settings, monkeypatch):
    class Embedder:
        def embed_query(self, query):
            return [1.0] * 1024
    monkeypatch.setattr(acceptance, "build_budgeted_embeddings", lambda *args: Embedder())
    adapter = AcceptanceAdapters(settings, tmp_path, ledger(tmp_path, initial_embedding=1))
    adapter.embeddings(lambda: 1)
    assert adapter.embed_query("first") == [1.0] * 1024
    with pytest.raises(RuntimeError, match="research_embedding"):
        adapter.embed_query("new query")
    replay = AcceptanceAdapters(settings, tmp_path, ledger(tmp_path))
    assert replay.embed_query("first") == [1.0] * 1024
    path = next((tmp_path / "query").glob("*.json"))
    data = json.loads(path.read_text())
    data["request_fingerprint"] = "invalid"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="不匹配"):
        AcceptanceAdapters(settings, tmp_path, ledger(tmp_path, initial_embedding=2)).embed_query("first")


def test_actual_limits_are_in_prompt():
    _, prompt = decision_messages(state())
    assert json.loads(prompt)["execution_limits"] == state()["execution_limits"]


def test_provider_response_is_captured_before_finish_reason_validation(settings, monkeypatch):
    from types import SimpleNamespace
    from ticketmind.llm import client
    completion = SimpleNamespace(id="synthetic", usage=None, choices=[
        SimpleNamespace(message=SimpleNamespace(content="truncated raw response"), finish_reason="length")])
    class OpenAI:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: completion))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(client, "OpenAI", OpenAI)
    captured = []
    with pytest.raises(ValueError, match="finish reason"):
        client.generate_text(settings=settings, system_prompt="unit", user_prompt="unit", response_callback=captured.append)
    assert captured[0]["choices"][0]["content"] == "truncated raw response"


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts/check_m2_acceptance.py"
    spec = importlib.util.spec_from_file_location("m2_acceptance_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_is_offline_and_labels_are_separate(settings):
    from ticketmind.agent.policy import input_risks
    script = load_script()
    cases = script.load_cases()
    for case in cases:
        assert "expected_action" not in case["input"]
        assert not input_risks(case["input"]["body"])
    # Nonlegacy inputs have no exact caches under the synthetic unit model endpoint.
    rows = script.preflight(settings, cases[1:])
    assert all(not row["understanding_cache"] and not row["initial_vector_cache"] for row in rows)
