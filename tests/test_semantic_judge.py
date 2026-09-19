"""Deterministic substitutes test contracts/orchestration, not LLM semantic accuracy."""
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from ticketmind.agent import runtime, semantic_judge, decide
from ticketmind.agent.decide import decision_messages, decision_response_adapter
from ticketmind.agent.proposals import Escalation, Clarification, proposal_adapter, decision_adapter, model_proposal_adapter
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.agent.schemas import AgentMessage, AgentRunInput
from ticketmind.agent.semantic_judge import JudgeResult
from ticketmind.core.config import QwenSettings, ProcessingSettings, MilvusSettings
from ticketmind.knowledge.sources import load_sources

PASS = {"passed": True, "violations": []}
BAD = Escalation(next_step="escalate", reason="需要人工核查", reply="届时会由人工确认恢复范围")
GOOD = Escalation(next_step="escalate", reason="需要人工核查", reply="建议人工核查")
FAIL = {"passed": False, "violations": [{"type": "unsupported_commitment", "text": BAD.reply,
                                       "reason": "系统不能保证人工未来采取行动"}]}


def run_input(subject="s", content="b"):
    return AgentRunInput(subject=subject, messages=[AgentMessage(role="customer", content=content)])


def test_judge_payload_excludes_derived_context_without_changing_audit():
    state = {
        "subject": "连接失败",
        "messages": [AgentMessage(role="customer", content="当前使用本地代理。")],
        "tool_calls": [
            {"tool": "search_cases", "parameters": {"query": "连接失败"},
             "status": "succeeded", "result_source_ids": ["case-1"],
             "result_summary": "检索摘要", "duration_ms": 10},
            {"tool": "get_case_detail", "parameters": {"source_id": "case-1"},
             "status": "failed", "result_source_ids": [], "error": "tool_execution_failed"},
        ],
    }
    original = deepcopy(state)
    _, user_prompt = semantic_judge.judge_messages(state, GOOD)
    payload = json.loads(user_prompt)
    assert "understanding" not in payload and "body" not in payload
    assert set(payload) == {"subject", "messages", "proposal", "tool_calls", "system_capabilities"}
    assert payload["subject"] == state["subject"]
    assert payload["messages"] == [{"role": "customer", "content": "当前使用本地代理。"}]
    assert payload["proposal"] == GOOD.model_dump()
    for actual, source in zip(payload["tool_calls"], state["tool_calls"], strict=True):
        assert "result_summary" not in actual and "error" not in actual
        assert actual == {key: source[key] for key in ("tool", "parameters", "status", "result_source_ids")}
    assert state == original
    assert semantic_judge.judge_messages(state, GOOD)[1] == user_prompt


def make_runner(monkeypatch, decisions, judgments, **overrides):
    seen, judged = [], []
    class Client:
        closed = False
        def close(self):
            self.closed = True
    client = Client()
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [])
    def decision(state, timeout, usage):
        assert timeout > 0
        seen.append(dict(state))
        return decisions[min(len(seen) - 1, len(decisions) - 1)]
    def judge(state, proposal, timeout, usage):
        assert timeout > 0
        judged.append(proposal)
        response = judgments[min(len(judged) - 1, len(judgments) - 1)]
        if isinstance(response, Exception):
            raise response
        return response
    from pathlib import Path
    fixture_path = Path(__file__).resolve().parents[1] / "data/synthetic/v2/historical_cases.jsonl"
    config = ProcessingSettings(
        _env_file=None, corpus_path=fixture_path, retrieval_mode="bm25",
        decision_model="glm-5.3", judge_model="deepseek-v4.1-flash",
    )
    args = dict(decision_fn=decision, judge_fn=judge, milvus_factory=lambda _: client,
                corpus=load_sources(config.corpus_path))
    args.update(overrides)
    runner = AgentRunner(QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
                         MilvusSettings(_env_file=None, uri="http://unused.invalid"), config, **args)
    return runner, seen, judged, client


def test_rejected_then_repaired_once(monkeypatch):
    runner, seen, judged, client = make_runner(monkeypatch, [BAD, GOOD], [FAIL, PASS])
    result = runner(run_input("连接失败", "操作失败"))
    assert len(seen) == len(judged) == 2 and client.closed
    assert result.state["proposal"] == GOOD
    assert seen[1]["guardrail_feedback"]["violations"] == FAIL["violations"]
    assert seen[1]["guardrail_feedback"]["proposal"] == BAD.model_dump()
    assert set(seen[1]["guardrail_feedback"]) == {"proposal", "violations"}
    assert decision_response_adapter(seen[0]) is decision_adapter
    assert decision_response_adapter(seen[1]) is model_proposal_adapter
    _, user = decision_messages(seen[1])
    repair_payload = json.loads(user)
    assert repair_payload["guardrail_feedback"]["violations"] == FAIL["violations"]
    assert set(repair_payload) == {"subject", "messages", "evidence", "case_details", "guardrail_feedback"}
    assert [a["status"] for a in result.usage["semantic_judge"]] == ["rejected", "passed"]
    assert len(result.state["tool_calls"]) == 1  # Only initial read-only retrieval.
    assert runner.metadata["model_config"]["decision"] == "glm-5.3"
    assert runner.metadata["model_config"]["semantic_judge"] == "deepseek-v4.1-flash"


def test_second_failure_safely_exits(monkeypatch):
    runner, seen, judged, client = make_runner(monkeypatch, [BAD], [FAIL])
    with pytest.raises(RunFailure) as error:
        runner(run_input())
    assert len(seen) == len(judged) == 2 and client.closed
    failure = error.value
    assert failure.stage == "semantic_guardrail" and "proposal" not in failure.partial
    assert failure.__cause__.code == "semantic_guardrail_failure"
    assert failure.usage["guardrail_failure"]["code"] == "semantic_guardrail_failure"
    assert [a["result"] for a in failure.usage["semantic_judge"]] == [FAIL, FAIL]


@pytest.mark.parametrize("response", [TimeoutError("secret"), ValueError("bad JSON"),
    {"passed": True, "violations": FAIL["violations"]}, {"passed": "true", "violations": []},
    {"passed": False, "violations": [{"type": "unsupported_commitment", "text": "不存在的原文", "reason": "x"}]}])
def test_judge_errors_fail_closed_without_repair(monkeypatch, response):
    runner, seen, judged, client = make_runner(monkeypatch, [BAD], [response])
    with pytest.raises(RunFailure) as error:
        runner(run_input())
    assert len(seen) == len(judged) == 1 and client.closed
    assert error.value.__cause__.code == "semantic_judge_error"
    assert "secret" not in str(error.value.__cause__)


@pytest.mark.parametrize("repair", [
    {"next_step": "search_cases", "query": "q", "reason": "x"},
    Escalation(next_step="escalate", reply="建议人工核查", reason="x", evidence_ids=["invented"]),
    {"next_step": "ask_clarification", "reply": "当前配置？", "reason": "x",
     "risk_flags": ["security"]},
])
def test_repair_cannot_bypass_deterministic_rules_or_execute_tools(monkeypatch, repair):
    runner, seen, judged, _ = make_runner(monkeypatch, [BAD, repair], [FAIL])
    with pytest.raises(RunFailure) as error:
        runner(run_input())
    assert len(seen) == 2 and len(judged) == 1
    assert error.value.__cause__.code == "guardrail_repair_failed"
    assert len(error.value.partial["tool_calls"]) == 1


def test_invalid_evidence_fails_before_judge(monkeypatch):
    invalid = {"next_step": "propose_resolution", "reason": "x", "reply": "x", "evidence_ids": ["unknown"]}
    runner, _, judged, _ = make_runner(monkeypatch, [invalid], [PASS])
    with pytest.raises(RunFailure):
        runner(run_input())
    assert not judged


def test_input_risk_escalation_is_judged(monkeypatch):
    runner, seen, judged, _ = make_runner(monkeypatch, [GOOD], [PASS])
    result = runner(run_input("密钥泄露", "请处理"))
    assert not seen and len(judged) == 1
    assert result.state["proposal"].risk_flags == ["security"]


def test_injected_cache_never_silently_adds_paid_judge_call(monkeypatch):
    runner, _, _, _ = make_runner(monkeypatch, [GOOD], [PASS], judge_fn=None)
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected paid judge call")
    monkeypatch.setattr(runtime, "judge_proposal", forbidden)
    with pytest.raises(RunFailure) as error:
        runner(run_input())
    assert error.value.__cause__.code == "semantic_judge_error"


def test_real_adapters_use_distinct_models_and_record_usage(monkeypatch):
    calls = []
    def decision_response(**kwargs):
        calls.append(kwargs)
        kwargs["usage_callback"]({"total_tokens": 12})
        # ModelEscalation intentionally has no runtime-only risk_flags field.
        return json.dumps({"next_step": GOOD.next_step, "reason": GOOD.reason,
                           "reply": GOOD.reply, "evidence_ids": GOOD.evidence_ids})
    def judge_response(**kwargs):
        calls.append(kwargs)
        kwargs["usage_callback"]({"total_tokens": 8})
        return json.dumps({"violations": []})
    monkeypatch.setattr(decide, "generate_text", decision_response)
    monkeypatch.setattr(semantic_judge, "generate_text", judge_response)
    runner, _, _, _ = make_runner(monkeypatch, [], [], decision_fn=None, judge_fn=None)
    result = runner(run_input())
    assert [c["settings"].model for c in calls] == ["glm-5.3", "deepseek-v4.1-flash"]
    assert calls[0]["generation_options"]["extra_body"]["enable_thinking"] is True
    assert calls[0]["generation_options"]["max_tokens"] == 4096
    assert calls[1]["generation_options"]["extra_body"]["enable_thinking"] is False
    assert result.usage["semantic_judge"][0]["usage"]["total_tokens"] == 8


def test_glm53_generation_options_are_in_cache_fingerprint(monkeypatch):
    from ticketmind.agent import dev_decision_cache
    captured = []
    monkeypatch.setattr(dev_decision_cache, "calculate_request_fingerprint", lambda request: captured.append(request))
    settings = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused")
    state = {"subject": "s", "messages": [AgentMessage(role="customer", content="b")],
             "retrieval_hits": [], "tool_calls": [], "clarification_rounds": 0}
    for model in ("glm-5.3", "glm-5.2"):
        dev_decision_cache.decision_fingerprint(settings.model_copy(update={"model": model}), state)
    assert captured[0]["extra_body"] == {"enable_thinking": True} and captured[0]["max_tokens"] == 4096
    assert captured[1]["extra_body"] == {"enable_thinking": False} and captured[1]["max_tokens"] == 1600


@pytest.mark.parametrize("decision,judge", [("glm-5.2", "glm5.2"), ("QWEN3.7-FLASH", "qwen3.7-flash")])
def test_models_must_differ(decision, judge):
    with pytest.raises(ValidationError, match="不同模型"):
        ProcessingSettings(_env_file=None, decision_model=decision, judge_model=judge)


def test_judge_model_schema_derives_passed_instead_of_asking_model_for_it():
    schema = JudgeResult.model_json_schema()
    assert "passed" not in schema["properties"]
    passed = JudgeResult.model_validate({"violations": []})
    failed = JudgeResult.model_validate({"violations": FAIL["violations"]})
    assert passed.passed is True and failed.passed is False
    assert passed.model_dump() == {"violations": [], "passed": True}


@pytest.mark.parametrize("data", [
    {"passed": True, "violations": []},
    {"violations": [], "expected_action": "escalate"},
    {"violations": [{"type": "unknown_rule", "text": "x", "reason": "x"}]},
])
def test_strict_judge_model_protocol(data):
    with pytest.raises(ValidationError):
        JudgeResult.model_validate(data)


def test_legacy_internal_judge_shape_is_only_accepted_when_consistent():
    assert semantic_judge.validate_judgment(PASS, GOOD).passed
    with pytest.raises(ValueError, match="不一致"):
        semantic_judge.validate_judgment({"passed": True, "violations": FAIL["violations"]}, BAD)


def test_repair_fits_three_step_budget_after_understanding_removal(monkeypatch):
    runner, seen, judged, _ = make_runner(monkeypatch, [BAD], [FAIL])
    runner.config = runner.config.model_copy(update={"max_agent_steps": 3})
    with pytest.raises(RunFailure) as error:
        runner(run_input())
    assert len(seen) == len(judged) == 2
    assert error.value.__cause__.code == "semantic_guardrail_failure"


@pytest.mark.parametrize("kind,reply", [
    ("operation_in_clarification", "请停用代理后重试"),
    ("repeated_known_fact", "您是否使用代理？"),
    ("false_status_claim", "我们已通知团队"),
])
def test_each_violation_blocks_the_proposal(monkeypatch, kind, reply):
    value = Clarification(next_step="ask_clarification", reply=reply, reason="核对")
    report = {"passed": False, "violations": [{"type": kind, "text": reply, "reason": "离线预设违规"}]}
    runner, seen, judged, _ = make_runner(monkeypatch, [value], [report])
    with pytest.raises(RunFailure) as error:
        runner(run_input("连接失败", "已经使用代理"))
    assert len(seen) == len(judged) == 2 and "proposal" not in error.value.partial
    assert error.value.__cause__.code == "semantic_guardrail_failure"


def test_resolution_source_id_in_retrieval_reaches_judge_without_quotes(monkeypatch):
    from ticketmind.knowledge.corpus import build_case_text
    from ticketmind.retrieval.dense import RetrievalHit
    from pathlib import Path
    corpus = load_sources(Path(__file__).resolve().parents[1] / "data/synthetic/v2/historical_cases.jsonl")
    case = next(iter(corpus.cases.values()))
    hit = RetrievalHit(source_id=case.source_id, text=build_case_text(case), score=0.5)
    proposal = {"next_step": "propose_resolution", "reply": "核对配置", "reason": "核对",
                "evidence_ids": [case.source_id]}
    runner, _, judged, _ = make_runner(monkeypatch, [proposal], [PASS])
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [hit])
    result = runner(run_input())
    assert result.state["proposal"].next_step == "propose_resolution"
    assert len(judged) == 1
    assert "evidence_quotes" not in result.state["proposal"].model_dump()


def test_resolution_unknown_source_still_rejected_before_judge(monkeypatch):
    proposal = {"next_step": "propose_resolution", "reply": "核对配置", "reason": "核对",
                "evidence_ids": ["invented"]}
    runner, _, judged, _ = make_runner(monkeypatch, [proposal], [PASS])
    with pytest.raises(RunFailure):
        runner(run_input())
    assert not judged
