import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ticketmind.agent.proposals import decision_adapter, model_proposal_adapter, proposal_adapter, validate_proposal
from ticketmind.core.config import AuthSettings
from ticketmind.main import create_app


@pytest.mark.parametrize("data", [
    {"next_step": "propose_resolution", "reason": "reason", "reply": "reply", "evidence_ids": []},
    {"next_step": "ask_clarification", "reason": "reason", "reply": "reply", "questions": []},
    {"next_step": "delete_data", "reason": "reason", "reply": "reply"},
    {"next_step": "escalate", "reason": "reason", "reply": "reply", "tool_calls": []},
])
def test_invalid_decision_shape(data):
    with pytest.raises(ValidationError):
        proposal_adapter.validate_python(data)


def test_citations_must_be_from_current_retrieval():
    proposal = proposal_adapter.validate_python({"next_step": "propose_resolution", "reason": "reason",
                                                 "reply": "reply", "evidence_ids": ["invented"]})
    with pytest.raises(ValueError, match="不存在"):
        validate_proposal(proposal, {"actual"})


def test_risk_flags_only_belong_to_escalation():
    with pytest.raises(ValidationError):
        proposal_adapter.validate_python({"next_step": "propose_resolution", "reason": "reason",
            "reply": "reply", "evidence_ids": ["actual"], "risk_flags": ["payment"]})



def test_live_resolution_schema_is_stricter_than_historical_parser():
    old_resolution = {
        "next_step": "propose_resolution",
        "reason": "historical",
        "reply": "historical reply",
        "evidence_ids": ["case-1"],
    }
    # Stored historical rows may predate verbatim quote capture.
    proposal_adapter.validate_python(old_resolution)
    # Current model output must satisfy the stronger live contract immediately.
    with pytest.raises(ValidationError):
        decision_adapter.validate_python(old_resolution)
    with pytest.raises(ValidationError):
        model_proposal_adapter.validate_python(old_resolution)


@pytest.mark.parametrize("data", [
    {"next_step": "propose_resolution", "reason": "r", "reply": "x",
     "evidence_ids": ["case-1"], "evidence_quotes": {"case-1": "123456789012"}, "questions": []},
    {"next_step": "ask_clarification", "reason": "r", "reply": "x",
     "questions": ["当前配置是什么？"], "risk_flags": ["security"]},
    {"next_step": "escalate", "reason": "r", "reply": "x", "questions": []},
])
def test_final_actions_expose_only_their_own_fields(data):
    with pytest.raises(ValidationError):
        decision_adapter.validate_python(data)

def test_all_business_routes_require_auth_before_db():
    def forbidden():
        raise AssertionError("unauthenticated request opened DB")
    client = TestClient(create_app(session_factory=forbidden, auth_settings=AuthSettings(_env_file=None)))
    for path in ("/tickets", "/tickets/00000000-0000-0000-0000-000000000001",
                 "/tickets/00000000-0000-0000-0000-000000000001/runs", "/sources/example?corpus_version=v1"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["error_code"] == "authentication_required"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_credentials_must_be_distinct():
    with pytest.raises(ValidationError):
        AuthSettings(_env_file=None, operator_token="a" * 32, reviewer_token="a" * 32)
