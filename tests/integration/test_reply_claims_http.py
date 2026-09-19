"""Real HTTP/PostgreSQL/checkpointer, explicit offline Decision/Judge responses."""
import json
from uuid import UUID, uuid4

import pytest

import test_m1_api as m1
from test_m2_reviews import review
from ticketmind.agent import decide, runtime, semantic_judge
from ticketmind.agent.proposals import Escalation
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.tickets.models import ProcessingResult

pytestmark = m1.pytestmark
database = m1.database
setup = m1.setup

BAD = Escalation(next_step="escalate", reason="需要人工核查", reply="届时会由人工确认恢复范围")
GOOD = Escalation(next_step="escalate", reason="需要核查", reply="建议人工核查")
FAIL = {"passed": False, "violations": [{"type": "unsupported_commitment", "text": BAD.reply,
                                       "reason": "不能保证外部人员未来执行动作"}]}
PASS = {"passed": True, "violations": []}


@pytest.mark.parametrize("entry", ["model_response", "injected_decision"])
@pytest.mark.parametrize("outcome", ["pass", "repair", "reject_twice", "judge_error"])
def test_semantic_guardrail_http_persistence(setup, monkeypatch, entry, outcome):
    client, synthetic, factory = setup
    decisions, judgments = [], []
    class LocalClient:
        closed = False
        def close(self):
            self.closed = True
    local_client = LocalClient()
    def next_decision():
        decisions.append(True)
        return GOOD if outcome == "pass" or (outcome == "repair" and len(decisions) == 2) else BAD
    def next_judgment():
        judgments.append(True)
        if outcome == "judge_error":
            raise TimeoutError("synthetic-secret-do-not-expose")
        return PASS if outcome == "pass" or (outcome == "repair" and len(judgments) == 2) else FAIL
    def recorded_response(**kwargs):
        assert kwargs["settings"].model == "glm-5.3"
        if len(decisions) == 1:
            assert json.loads(kwargs["user_prompt"])["guardrail_feedback"]["violations"] == FAIL["violations"]
        return next_decision().model_dump_json()
    def judge_response(**kwargs):
        assert kwargs["settings"].model == "deepseek-v4.1-flash"
        judgment = next_judgment()
        return json.dumps({"violations": judgment["violations"]}, ensure_ascii=False)
    monkeypatch.setattr(decide, "generate_text", recorded_response)
    monkeypatch.setattr(semantic_judge, "generate_text", judge_response)
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [])
    client.app.state.runner = runtime.AgentRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"),
        ProcessingSettings(_env_file=None, retrieval_mode="bm25"),
        decision_fn=(lambda *args: next_decision()) if entry == "injected_decision" else None,
        judge_fn=(lambda *args: next_judgment()) if entry == "injected_decision" else None,
        milvus_factory=lambda _: local_client, corpus=synthetic.corpus,
    )
    ticket, key = m1.create(client), uuid4().hex
    response = m1.run(client, ticket, key=key)
    result = response.json()
    success = outcome in ("pass", "repair")
    assert response.status_code == 201, result
    assert result["run_status"] == ("waiting_review" if success else "failed")
    assert result["published_message_id"] is None
    assert "synthetic-secret" not in response.text
    assert m1.run(client, ticket, key=key).json() == result
    assert len(decisions) == len(judgments) == (2 if outcome in ("repair", "reject_twice") else 1)
    assert local_client.closed
    current = client.get(f'/tickets/{ticket["id"]}').json()
    assert current["status"] == "open" and current["version"] == 1 and len(current["messages"]) == 1
    with factory() as session:
        saved = session.get(ProcessingResult, UUID(result["id"]))
        assert len(saved.usage["semantic_judge"]) == len(judgments)
        if success:
            assert saved.proposal["reply"] == GOOD.reply
        else:
            assert saved.proposal is None and saved.review is None
            assert saved.error_code == ("semantic_judge_error" if outcome == "judge_error" else "semantic_guardrail_failure")
            assert saved.usage["guardrail_failure"]["code"] == saved.error_code
            if outcome == "reject_twice":
                assert all(a["result"] == FAIL for a in saved.usage["semantic_judge"])
    checkpoint = client.app.state.workflow.graph().get_state(client.app.state.workflow.config(result["thread_id"]))
    if success:
        assert any(task.interrupts for task in checkpoint.tasks)
        reviewed = review(client, ticket, result).json()
        assert reviewed["run_status"] == "completed" and reviewed["published_message_id"]
        assert client.get(f'/tickets/{ticket["id"]}').json()["status"] == "escalated"
        assert len(judgments) == (2 if outcome == "repair" else 1)  # Resume does not rerun Judge.
    else:
        assert "output" not in checkpoint.values and not any(task.interrupts for task in checkpoint.tasks)
        assert review(client, ticket, result).json()["error_code"] == "run_not_reviewable"
