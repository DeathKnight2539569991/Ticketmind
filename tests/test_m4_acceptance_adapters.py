"""Offline checks for the current two-model M4 acceptance boundary."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from ticketmind.agent.dev_acceptance import (
    AcceptanceAdapters, AttemptLedger, CATEGORIES, M4_CATEGORIES, judge_fingerprint,
)
from ticketmind.agent.proposals import Clarification
from ticketmind.agent.schemas import AgentMessage
from ticketmind.core.config import ProcessingSettings, QwenSettings


def settings(model="deepseek-v4.1-flash"):
    return QwenSettings(_env_file=None, DASHSCOPE_API_KEY="offline-only",
                        DASHSCOPE_WORKSPACE_ID="offline-only", model=model)


def context():
    state = {"subject": "请求超时", "messages": [AgentMessage(role="customer", content="请排查")],
             "tool_calls": [], "retrieval_hits": []}
    proposal = Clarification(next_step="ask_clarification", reason="缺少发生时间",
                             reply="请提供发生时间。", evidence_ids=[])
    return state, proposal


def ledger(path, *, judge=0, decision=0):
    limits = {category: 0 for category in M4_CATEGORIES}
    limits.update(judge=judge, decision=decision)
    return AttemptLedger(path / "attempts.json", limits)


def response(content, *, finish_reason="stop"):
    return {"request_id": "offline-response", "usage": {"total_tokens": 7},
            "choices": [{"content": content, "finish_reason": finish_reason}]}


def test_judge_request_has_independent_model_proposal_and_protocol_fingerprint(monkeypatch):
    from ticketmind.agent import dev_acceptance
    state, proposal = context()
    first = judge_fingerprint(settings(), state, proposal)
    assert first != judge_fingerprint(settings("different-judge"), state, proposal)
    assert first != judge_fingerprint(settings(), state, proposal.model_copy(update={"reply": "请提供请求ID。"}))
    assert first != judge_fingerprint(settings(), {**state, "messages": [AgentMessage(role="customer", content="不同事实")]}, proposal)
    monkeypatch.setattr(dev_acceptance, "JUDGE_PROTOCOL", "future-protocol")
    assert first != judge_fingerprint(settings(), state, proposal)


def test_judge_raw_response_is_cached_and_ledger_counts_once(tmp_path, monkeypatch):
    from ticketmind.agent import dev_acceptance
    state, proposal = context()
    calls = []
    def fake_generate_text(**kwargs):
        calls.append(kwargs)
        assert kwargs["settings"].model == "deepseek-v4.1-flash"
        assert kwargs["generation_options"]["max_tokens"] == 2000
        kwargs["response_callback"](response('{"violations":[]}'))
        return '{"violations":[]}'
    monkeypatch.setattr(dev_acceptance, "generate_text", fake_generate_text)
    first_usage = {}
    adapter = AcceptanceAdapters(settings(), tmp_path, ledger(tmp_path, judge=1))
    assert adapter.judge(state, proposal, 30, first_usage).passed
    assert len(calls) == 1
    assert len(first_usage["acceptance_judges"]) == 1
    fp = first_usage["acceptance_judges"][0]["fingerprint"]
    cached = json.loads(adapter.path("judge", fp).read_text(encoding="utf-8"))
    assert cached["response"]["choices"][0]["content"] == '{"violations":[]}'
    replay_ledger = ledger(tmp_path)
    replay = AcceptanceAdapters(settings(), tmp_path, replay_ledger)
    assert replay.judge(state, proposal, 30, {}).passed
    assert len(calls) == 1 and len(replay_ledger.data["attempts"]) == 1
    assert replay_ledger.cache_hits == [{"category": "judge", "fingerprint": fp}]


def test_judge_failure_without_raw_response_is_counted_and_not_retried(tmp_path, monkeypatch):
    from ticketmind.agent import dev_acceptance
    state, proposal = context()
    calls = []
    def fail(**kwargs):
        calls.append(1)
        raise ConnectionError("offline failure")
    monkeypatch.setattr(dev_acceptance, "generate_text", fail)
    adapter = AcceptanceAdapters(settings(), tmp_path, ledger(tmp_path, judge=1))
    with pytest.raises(ConnectionError):
        adapter.judge(state, proposal, 30, {})
    attempts = ledger(tmp_path).data["attempts"]
    assert len(attempts) == 1 and attempts[0]["category"] == "judge"
    assert attempts[0]["status"] == "failed" and attempts[0]["usage"] is None
    with pytest.raises(RuntimeError, match="禁止自动重试"):
        adapter.judge(state, proposal, 30, {})
    assert len(calls) == 1


def test_judge_zero_ceiling_blocks_before_send(tmp_path, monkeypatch):
    from ticketmind.agent import dev_acceptance
    monkeypatch.setattr(dev_acceptance, "generate_text", lambda **kwargs: pytest.fail("must not send"))
    state, proposal = context()
    with pytest.raises(RuntimeError, match="未授权"):
        AcceptanceAdapters(settings(), tmp_path, ledger(tmp_path)).judge(state, proposal, 30, {})


def test_malformed_judge_raw_output_is_replayed_as_failure_without_new_call(tmp_path, monkeypatch):
    from ticketmind.agent import dev_acceptance
    state, proposal = context()
    calls = []
    def malformed(**kwargs):
        calls.append(1)
        kwargs["response_callback"](response('{"violations":"invalid"}'))
        return '{"violations":"invalid"}'
    monkeypatch.setattr(dev_acceptance, "generate_text", malformed)
    adapter = AcceptanceAdapters(settings(), tmp_path, ledger(tmp_path, judge=1))
    with pytest.raises(ValidationError):
        adapter.judge(state, proposal, 30, {})
    replay = AcceptanceAdapters(settings(), tmp_path, ledger(tmp_path))
    with pytest.raises(ValidationError):
        replay.judge(state, proposal, 30, {})
    assert len(calls) == 1 and len(ledger(tmp_path).data["attempts"]) == 1


def test_old_three_category_ledgers_remain_readable_but_do_not_authorize_judge(tmp_path):
    assert CATEGORIES == ("initial_embedding", "research_embedding", "decision")
    old = {"attempts": [{"category": "decision", "fingerprint": "historical-request", "status": "succeeded"}]}
    (tmp_path / "attempts.json").write_text(json.dumps(old), encoding="utf-8")
    previous = AttemptLedger(tmp_path / "attempts.json", {category: 0 for category in CATEGORIES})
    assert previous.data["attempts"] == old["attempts"]
    with pytest.raises(RuntimeError, match="单独指定"):
        previous.attempt("judge", "new", lambda record: pytest.fail("must not send"))
    upgraded = ledger(tmp_path, judge=1)
    assert upgraded.data["attempts"] == old["attempts"]
    assert upgraded.attempt("judge", "new", lambda record: "offline") == "offline"
    assert len(upgraded.data["attempts"]) == 2


def test_m4_runner_wires_both_model_adapters(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    scripts = root / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("evaluate_m4_for_unit", scripts / "evaluate_m4.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CACHE", tmp_path)
    monkeypatch.setattr(module, "MilvusSettings", lambda: object())
    monkeypatch.setattr(module, "load_sources", lambda path: object())
    observed = {}
    class FakeRunner:
        def __init__(self, qwen, milvus, config, **kwargs):
            observed.update(config=config, **kwargs)
    monkeypatch.setattr(module, "AgentRunner", FakeRunner)
    config = ProcessingSettings(_env_file=None, decision_model="qwen3.8-flash",
                                judge_model="deepseek-v4.1-flash")
    runner, decisions, judges = module.build_acceptance_runner(settings("qwen3.7-flash"), config, ledger(tmp_path))
    assert isinstance(runner, FakeRunner)
    assert observed["decision_fn"] == decisions.decision
    assert observed["judge_fn"] == judges.judge
    assert decisions.settings.model == config.decision_model
    assert judges.settings.model == config.judge_model
