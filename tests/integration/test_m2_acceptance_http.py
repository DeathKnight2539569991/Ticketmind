"""New acceptance entry: real PostgreSQL/checkpointer, synthetic model AND Milvus."""
from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pytest

from test_m1_api import pytestmark
from ticketmind.agent import dev_acceptance, runtime
from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.run_cache import calculate_request_fingerprint
from ticketmind.core.config import QwenSettings, ProcessingSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources


def load_script():
    path = Path(__file__).resolve().parents[2] / "scripts/check_m2_acceptance.py"
    spec = importlib.util.spec_from_file_location("m2_acceptance_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ledger(path, **limits):
    return dev_acceptance.AttemptLedger(path / "attempts.json",
                                       {c: limits.get(c, 0) for c in dev_acceptance.CATEGORIES})


@pytest.mark.parametrize("case_index, review_decision, status", [
    (0, "approve", "awaiting_customer"), (1, "edit", "open"), (2, "approve", "escalated")])
def test_acceptance_exports_raw_and_reviewed_results(tmp_path, monkeypatch, case_index, review_decision, status):
    script = load_script()
    original_runner = script.AgentRunner
    monkeypatch.setattr(script, "AgentRunner", lambda *args, **kwargs: original_runner(
        *args, **kwargs, judge_fn=lambda *args: {"passed": True, "violations": []}))
    case = script.load_cases()[case_index]
    case = {**case, "id": "synthetic-" + case["id"]}  # Don't use real M0 cache in a synthetic test.
    config = ProcessingSettings(_env_file=None)
    source = load_sources(config.corpus_path).cases["SYN-HIST-V2-002"]
    proposal_data = {"next_step": case["expected_action"], "reason": "合成验证",
        "reply": "当前设置是什么？" if case_index == 0 else "请核对当前预览设置。",
        "evidence_ids": [source.source_id]}
    proposal = proposal_adapter.validate_python(proposal_data)
    if case_index == 1:
        proposal.evidence_quotes = {source.source_id: source.resolution.summary}
    class Milvus:
        def search(self, **kwargs):
            return [[{"entity": {"source_id": source.source_id, "text": build_case_text(source)}, "distance": 0.5}]]
        def close(self):
            pass
    monkeypatch.setattr(runtime, "build_milvus_client", lambda settings: Milvus())
    monkeypatch.setattr(dev_acceptance, "build_budgeted_embeddings", lambda *args:
        SimpleNamespace(embed_query=lambda text: [1.0] * 1024))
    def generate(**kwargs):
        kwargs["response_callback"]({"request_id": "synthetic-id", "usage": {"total_tokens": 1},
            "choices": [{"content": proposal.model_dump_json(), "finish_reason": "stop"}]})
        return proposal.model_dump_json()
    monkeypatch.setattr(dev_acceptance, "generate_text", generate)
    review = {"proposal_hash": calculate_request_fingerprint(proposal.model_dump()),
              "request": {"decision": review_decision}}
    if review_decision == "edit":
        review["request"].update(edited_reply="人工修改后的预览核对建议。", comment="合成人工审核")
    qwen = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit-only", DASHSCOPE_WORKSPACE_ID="unit-only")
    budget = ledger(tmp_path, initial_embedding=1, decision=1)
    report = script.execute_case(case, qwen, config, budget, tmp_path, review)
    assert report["model_action_match"] and report["expected_action_match"]
    assert report["model_quality"] == "pending_human_review"
    assert report["business_review"]["ticket"]["status"] == status
    assert len(budget.data["attempts"]) == 2
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1
    # A second isolated workflow can replay the same raw proposal at zero new budget.
    replay = ledger(tmp_path)
    repeated = script.execute_case(case, qwen, config, replay, tmp_path, review)
    assert repeated["business_review"]["run"]["run_status"] == "completed"
    assert len(replay.data["attempts"]) == 2 and len(replay.cache_hits) == 2
