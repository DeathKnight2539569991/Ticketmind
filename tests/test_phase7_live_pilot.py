"""Offline checks for the bounded real-model pilot; never invokes providers."""
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("pilot_phase7_live", SCRIPTS / "pilot_phase7_live.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_prepare_has_no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline prepare invoked model")
    monkeypatch.setattr(pilot.AcceptanceAdapters, "decision", forbidden)
    monkeypatch.setattr(pilot.AcceptanceAdapters, "judge", forbidden)
    gate, fixtures = pilot.prepare()
    assert gate["review_status"] == "reviewed"
    assert [f[1]["expected_action"] for f in fixtures] == ["propose_resolution", "ask_clarification", "escalate"]
    for _, _, baseline, oracle in fixtures:
        for state in (baseline, oracle):
            _, user = pilot.decision_messages(state)
            assert '"expected_action"' not in user
            assert '"relevant_source_ids"' not in user
            assert '"necessary_questions"' not in user
    assert pilot.CEILINGS["initial_embedding"] == pilot.CEILINGS["research_embedding"] == 0
    assert sum(pilot.CEILINGS.values()) == 13


def test_prompt_guard_rejects_oversize_before_call():
    with pytest.raises(ValueError, match="prompt_limit"):
        pilot.prompt_guard(("界" * 10001, ""))
    pilot.prompt_guard(("x" * 30000, ""))


def test_cost_uses_busy_list_price_and_flags_missing_usage():
    cost = pilot.safe_usage_cost([
        {"category": "decision", "usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000}},
        {"category": "judge", "usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000}},
        {"category": "judge", "usage": None},
    ])
    assert cost["estimated_cny_list_price_busy_no_cache_discount"] == 13.5
    assert cost["attempts_with_unknown_usage"] == 1
    assert cost["is_provider_invoice"] is False
