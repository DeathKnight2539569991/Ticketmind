"""Real HTTP/PostgreSQL/checkpointer, local model and retrieval substitutes."""
import json
from uuid import UUID, uuid4

import pytest

import test_m1_api as m1
from test_m2_reviews import review
from ticketmind.agent import decide, runtime
from ticketmind.agent.proposals import Escalation
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.tickets.models import ProcessingResult

pytestmark = m1.pytestmark
database = m1.database
setup = m1.setup


@pytest.mark.parametrize("entry", ["model_response", "injected_runner"])
def test_invalid_claim_fails_before_review_and_preserves_ticket(setup, monkeypatch, entry):
    client, synthetic, factory = setup
    invalid = Escalation(next_step="escalate", reason="synthetic-secret-do-not-expose",
                         reply="我们已将此工单转交支付支持团队处理。支付支持人员会核对实际入账状态后与您联系。")
    calls = []
    if entry == "model_response":
        class LocalClient:
            closed = False

            def close(self):
                self.closed = True

        local_client = LocalClient()

        def recorded_response(**kwargs):
            calls.append(kwargs)
            return json.dumps(invalid.model_dump(), ensure_ascii=False)

        monkeypatch.setattr(decide, "generate_text", recorded_response)
        monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [])
        client.app.state.runner = runtime.AgentRunner(
            QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
            MilvusSettings(_env_file=None, uri="http://unused.invalid"),
            ProcessingSettings(_env_file=None, retrieval_mode="bm25"),
            understanding_fn=lambda **kwargs: TicketUnderstanding(summary="local", error_codes=[], environment=[]),
            milvus_factory=lambda _: local_client, corpus=synthetic.corpus,
        )
    else:
        # Verifies ReviewWorkflow's validation also protects against a runner
        # that bypasses decide_ticket, before the review interrupt is written.
        synthetic.proposal_override = invalid

    ticket, key = m1.create(client), uuid4().hex
    response = m1.run(client, ticket, key=key)
    result = response.json()
    assert response.status_code == 201 and result["run_status"] == "failed", result
    assert result["error_code"] == "proposal_unsupported_action_claim"
    assert "未经执行" in result["error_summary"]
    assert "synthetic-secret" not in response.text and invalid.reply not in response.text
    assert result["proposal"] is None and result["published_message_id"] is None
    assert m1.run(client, ticket, key=key).json() == result
    assert review(client, ticket, result).json()["error_code"] == "run_not_reviewable"
    current = client.get(f'/tickets/{ticket["id"]}').json()
    assert current["status"] == "open" and current["version"] == 1 and len(current["messages"]) == 1
    with factory() as session:
        saved = session.get(ProcessingResult, UUID(result["id"]))
        assert saved.error_code == result["error_code"] and saved.proposal is None and saved.review is None
    graph = client.app.state.workflow.graph()
    checkpoint = graph.get_state(client.app.state.workflow.config(result["thread_id"]))
    assert "output" not in checkpoint.values
    assert not any(task.interrupts for task in checkpoint.tasks)
    if entry == "model_response":
        assert len(calls) == 1 and local_client.closed
    else:
        assert synthetic.calls == 1


def test_legal_escalation_still_needs_review_before_ticket_status_changes(setup):
    client, runner, _ = setup
    runner.proposal_override = Escalation(next_step="escalate", reason="需要核查",
                                         reply="建议转交人工团队进一步确认。")
    ticket = m1.create(client)
    result = m1.run(client, ticket).json()
    assert result["run_status"] == "waiting_review"
    assert client.get(f'/tickets/{ticket["id"]}').json()["status"] == "open"
    reviewed = review(client, ticket, result).json()
    assert reviewed["run_status"] == "completed" and reviewed["published_message_id"]
    assert client.get(f'/tickets/{ticket["id"]}').json()["status"] == "escalated"
