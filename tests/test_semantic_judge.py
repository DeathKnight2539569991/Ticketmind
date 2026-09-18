"""Deterministic substitutes test contracts/orchestration, not LLM semantic accuracy."""
import json

import pytest
from pydantic import ValidationError

from ticketmind.agent import runtime, semantic_judge, decide
from ticketmind.agent.decide import decision_messages
from ticketmind.agent.proposals import Escalation, Clarification
from ticketmind.agent.runtime import AgentRunner, RunFailure
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.agent.semantic_judge import JudgeResult
from ticketmind.core.config import QwenSettings, ProcessingSettings, MilvusSettings
from ticketmind.knowledge.sources import load_sources

PASS = {"passed": True, "violations": []}
BAD = Escalation(next_step="escalate", reason="需要人工核查", reply="届时会由人工确认恢复范围")
GOOD = Escalation(next_step="escalate", reason="需要人工核查", reply="建议人工核查")
FAIL = {"passed": False, "violations": [{"type": "unsupported_commitment", "text": BAD.reply,
                                       "reason": "系统不能保证人工未来采取行动"}]}


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
    config = ProcessingSettings(_env_file=None, retrieval_mode="bm25")
    args = dict(understanding_fn=lambda **kwargs: TicketUnderstanding(summary="unit", error_codes=[], environment=[]),
                decision_fn=decision, judge_fn=judge, milvus_factory=lambda _: client,
                corpus=load_sources(config.corpus_path))
    args.update(overrides)
    runner = AgentRunner(QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
                         MilvusSettings(_env_file=None, uri="http://unused.invalid"), config, **args)
    return runner, seen, judged, client


def test_rejected_then_repaired_once(monkeypatch):
    runner, seen, judged, client = make_runner(monkeypatch, [BAD, GOOD], [FAIL, PASS])
    result = runner({"subject": "连接失败", "body": "操作失败"})
    assert len(seen) == len(judged) == 2 and client.closed
    assert result.state["proposal"] == GOOD
    assert seen[1]["guardrail_feedback"]["violations"] == FAIL["violations"]
    assert seen[1]["guardrail_feedback"]["proposal"] == BAD.model_dump()
    _, user = decision_messages(seen[1])
    assert json.loads(user)["guardrail_feedback"]["violations"] == FAIL["violations"]
    assert [a["status"] for a in result.usage["semantic_judge"]] == ["rejected", "passed"]
    assert len(result.state["tool_calls"]) == 1  # Only initial read-only retrieval.
    assert runner.metadata["model_config"]["decision"] == "glm-5.3"
    assert runner.metadata["model_config"]["semantic_judge"] == "deepseek-v4.1-flash"


def test_second_failure_safely_exits(monkeypatch):
    runner, seen, judged, client = make_runner(monkeypatch, [BAD], [FAIL])
    with pytest.raises(RunFailure) as error:
        runner({"subject": "s", "body": "b"})
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
        runner({"subject": "s", "body": "b"})
    assert len(seen) == len(judged) == 1 and client.closed
    assert error.value.__cause__.code == "semantic_judge_error"
    assert "secret" not in str(error.value.__cause__)


@pytest.mark.parametrize("repair", [
    {"next_step": "search_cases", "query": "q", "missing_evidence": "x", "reason": "x"},
    Escalation(next_step="escalate", reply="建议人工核查", reason="x", evidence_ids=["invented"]),
    Clarification(next_step="ask_clarification", reply="当前配置？", reason="x", questions=["当前配置？"], risk_flags=["security"]),
])
def test_repair_cannot_bypass_deterministic_rules_or_execute_tools(monkeypatch, repair):
    runner, seen, judged, _ = make_runner(monkeypatch, [BAD, repair], [FAIL])
    with pytest.raises(RunFailure) as error:
        runner({"subject": "s", "body": "b"})
    assert len(seen) == 2 and len(judged) == 1
    assert error.value.__cause__.code == "guardrail_repair_failed"
    assert len(error.value.partial["tool_calls"]) == 1


def test_invalid_evidence_fails_before_judge(monkeypatch):
    invalid = {"next_step": "propose_resolution", "reason": "x", "reply": "x", "evidence_ids": ["unknown"]}
    runner, _, judged, _ = make_runner(monkeypatch, [invalid], [PASS])
    with pytest.raises(RunFailure):
        runner({"subject": "s", "body": "b"})
    assert not judged


def test_input_risk_escalation_is_judged(monkeypatch):
    runner, seen, judged, _ = make_runner(monkeypatch, [GOOD], [PASS])
    result = runner({"subject": "密钥泄露", "body": "请处理"})
    assert not seen and len(judged) == 1
    assert result.state["proposal"].risk_flags == ["security"]


def test_injected_cache_never_silently_adds_paid_judge_call(monkeypatch):
    runner, _, _, _ = make_runner(monkeypatch, [GOOD], [PASS], judge_fn=None)
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected paid judge call")
    monkeypatch.setattr(runtime, "judge_proposal", forbidden)
    with pytest.raises(RunFailure) as error:
        runner({"subject": "s", "body": "b"})
    assert error.value.__cause__.code == "semantic_judge_error"


def test_real_adapters_use_distinct_models_and_record_usage(monkeypatch):
    calls = []
    def decision_response(**kwargs):
        calls.append(kwargs)
        kwargs["usage_callback"]({"total_tokens": 12})
        return GOOD.model_dump_json()
    def judge_response(**kwargs):
        calls.append(kwargs)
        kwargs["usage_callback"]({"total_tokens": 8})
        return json.dumps(PASS)
    monkeypatch.setattr(decide, "generate_text", decision_response)
    monkeypatch.setattr(semantic_judge, "generate_text", judge_response)
    runner, _, _, _ = make_runner(monkeypatch, [], [], decision_fn=None, judge_fn=None)
    result = runner({"subject": "s", "body": "b"})
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
    state = {"subject": "s", "body": "b", "retrieval_hits": [],
             "understanding": TicketUnderstanding(summary="unit", error_codes=[], environment=[])}
    for model in ("glm-5.3", "glm-5.2"):
        dev_decision_cache.decision_fingerprint(settings.model_copy(update={"model": model}), state)
    assert captured[0]["extra_body"] == {"enable_thinking": True} and captured[0]["max_tokens"] == 4096
    assert captured[1]["extra_body"] == {"enable_thinking": False} and captured[1]["max_tokens"] == 1600


@pytest.mark.parametrize("decision,judge", [("glm-5.2", "glm5.2"), ("QWEN3.7-FLASH", "qwen3.7-flash")])
def test_models_must_differ(decision, judge):
    with pytest.raises(ValidationError, match="不同模型"):
        ProcessingSettings(_env_file=None, decision_model=decision, judge_model=judge)


@pytest.mark.parametrize("data", [
    {"passed": False, "violations": []}, {"passed": True, "violations": FAIL["violations"]},
    {"passed": True, "violations": [], "expected_action": "escalate"},
    {"passed": False, "violations": [{"type": "missing_questions", "text": "x", "reason": "x"}]},
])
def test_strict_judge_protocol(data):
    with pytest.raises(ValidationError):
        JudgeResult.model_validate(data)


def test_question_duplicates_remain_deterministic():
    from ticketmind.agent.proposals import validate_proposal
    proposal = Clarification(next_step="ask_clarification", reason="x", reply="不要做任何修改",
                             questions=["之前是否停用过代理", "之前是否停用过代理"])
    with pytest.raises(ValueError, match="完全重复"):
        validate_proposal(proposal, set())


def test_repair_respects_total_step_budget(monkeypatch):
    runner, seen, judged, _ = make_runner(monkeypatch, [BAD], [FAIL])
    runner.config = runner.config.model_copy(update={"max_agent_steps": 3})
    with pytest.raises(RunFailure) as error:
        runner({"subject": "s", "body": "b"})
    assert len(seen) == len(judged) == 1
    assert error.value.__cause__.code == "guardrail_step_limit"


@pytest.mark.parametrize("kind,reply", [
    ("operation_in_clarification", "请停用代理后重试"),
    ("repeated_known_fact", "您是否使用代理？"),
    ("false_status_claim", "我们已通知团队"),
])
def test_each_violation_blocks_the_proposal(monkeypatch, kind, reply):
    value = Clarification(next_step="ask_clarification", reply=reply, reason="核对", questions=[reply])
    report = {"passed": False, "violations": [{"type": kind, "text": reply, "reason": "离线预设违规"}]}
    runner, seen, judged, _ = make_runner(monkeypatch, [value], [report])
    with pytest.raises(RunFailure) as error:
        runner({"subject": "连接失败", "body": "已经使用代理"})
    assert len(seen) == len(judged) == 2 and "proposal" not in error.value.partial
    assert error.value.__cause__.code == "semantic_guardrail_failure"


def test_fabricated_quote_on_real_source_fails_before_judge(monkeypatch):
    from ticketmind.knowledge.corpus import build_case_text
    from ticketmind.retrieval.dense import RetrievalHit
    corpus = load_sources(ProcessingSettings(_env_file=None).corpus_path)
    case = next(iter(corpus.cases.values()))
    hit = RetrievalHit(source_id=case.source_id, text=build_case_text(case), score=0.5)
    proposal = {"next_step": "propose_resolution", "reply": "核对配置", "reason": "核对",
                "evidence_ids": [case.source_id], "evidence_quotes": {case.source_id: "这是实际来源中完全不存在的伪造引用原文"}}
    runner, _, judged, _ = make_runner(monkeypatch, [proposal], [PASS])
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [hit])
    with pytest.raises(RunFailure) as error:
        runner({"subject": "s", "body": "b"})
    assert not judged and "原文" in str(error.value.__cause__)
