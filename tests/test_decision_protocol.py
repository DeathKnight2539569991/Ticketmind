import json

from ticketmind.agent.decide import DECISION_PROTOCOL, decision_messages, decision_response_adapter
from ticketmind.agent.schemas import AgentMessage
from ticketmind.retrieval.schemas import DocEvidenceHit


def test_decision_prompt_separates_cases_docs_and_rejected_observations():
    doc = DocEvidenceHit(source_id="doc-a1b2c3", doc_id="doc-1", chunk_id="chunk-2",
                        title="API 时间参数", section="查询参数", text="时间范围使用 ISO 8601 时区。",
                        score=0.91, docs_version="docs-v3", content_hash="sha256:abc",
                        synthetic=False, mode="hybrid", rank=1)
    state = {
        "subject": "统计区间问题",
        "messages": [AgentMessage(role="customer", content="使用时区参数查询")],
        "retrieval_hits": [], "docs_hits": [doc],
        "tool_calls": [{"tool": "search_docs", "parameters": {"query": "时间参数"},
                        "status": "rejected", "error": "docs_search_limit"}],
    }
    system, user = decision_messages(state)
    payload = json.loads(user)
    assert DECISION_PROTOCOL.endswith("v3")
    assert payload["cases"] == []
    assert payload["docs"] == [doc.model_dump()]
    assert "case_details" not in payload
    assert "status=`rejected`" in system and "没有产生新证据" in system


def test_repair_decision_remains_final_only():
    state = {"subject": "s", "messages": [], "retrieval_hits": [],
             "guardrail_feedback": {"violations": []}}
    system, user = decision_messages(state)
    adapter = decision_response_adapter(state)
    assert set(adapter.json_schema()["discriminator"]["mapping"]) == {
        "propose_resolution", "ask_clarification", "escalate"
    }
    assert "tool_calls" not in json.loads(user)
