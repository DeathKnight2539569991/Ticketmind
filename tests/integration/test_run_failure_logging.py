"""Real isolated PG/HTTP; synthetic nested failures must not leak into logs."""
import json
from uuid import UUID

import pytest

from test_m1_api import create, database, pytestmark, run, setup
from ticketmind.agent.proposals import decision_adapter
from ticketmind.agent.runtime import RunFailure
from ticketmind.tickets.models import ProcessingResult
from ticketmind.tickets import processing


@pytest.mark.parametrize("error_kind", ["validation", "provider"])
def test_run_failure_logs_only_structural_diagnostics(setup, monkeypatch, caplog, error_kind):
    client, runner, factory = setup
    marker = "SYNTHETIC_PRIVATE_MODEL_MARKER"
    # Alembic fileConfig in the isolated DB fixture can disable existing
    # application loggers and replace pytest's root capture handler.
    monkeypatch.setattr(processing.logger, "disabled", False)
    monkeypatch.setattr(processing.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(processing.logger, "propagate", False)

    def fail(self, *args, **kwargs):
        try:
            if error_kind == "validation":
                decision_adapter.validate_json(json.dumps({"next_step": "search_cases", "reason": "test",
                                                          "query": marker * 100}))
            raise RuntimeError(marker)
        except Exception as exc:
            raise RunFailure("decision", {"tool_calls": [{"tool": "search_cases", "status": "succeeded"}]},
                             [{"source_id": "synthetic-source"}], {"decisions": [{"total_tokens": 12}]}) from exc

    monkeypatch.setattr(type(runner), "__call__", fail)
    ticket = create(client)
    response = run(client, ticket)
    assert response.status_code == 201
    result = response.json()
    assert result["run_status"] == "failed" and result["error_code"] == "agent_execution_failed"
    assert marker not in response.text and marker not in caplog.text
    record = next(record for record in caplog.records if record.name == "ticketmind.tickets.processing")
    assert record.exc_info is None and record.stack_info is None
    expected_type = "ValidationError" if error_kind == "validation" else "RuntimeError"
    assert f"run_id={result['id']} stage=decision code=agent_execution_failed error={expected_type}" == record.getMessage()
    with factory() as session:
        saved = session.get(ProcessingResult, UUID(result["id"]))
        assert saved.usage == {"decisions": [{"total_tokens": 12}]}
        assert saved.retrieval_evidence == [{"source_id": "synthetic-source"}]
        assert saved.tool_calls == [{"tool": "search_cases", "status": "succeeded"}]
