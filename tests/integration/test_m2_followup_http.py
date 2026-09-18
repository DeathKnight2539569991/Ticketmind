"""Real PostgreSQL/checkpointer, synthetic model and Milvus, for new follow-up entry."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_m1_api import pytestmark
from ticketmind.agent import dev_acceptance
from ticketmind.agent.proposals import Clarification, Resolution
from ticketmind.agent.run_cache import calculate_request_fingerprint
from ticketmind.agent.runtime import RunOutput
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import QwenSettings, ProcessingSettings
from ticketmind.knowledge.sources import load_sources
from ticketmind.knowledge.corpus import build_case_text


def load_script(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("followup_script", scripts / "check_m2_followup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Legacy paid-call ledgers have no Judge category. This test supplies an
    # explicit offline adapter; running the legacy script itself stays closed.
    original_init = module.FollowupRunner.__init__
    def offline_init(self, *args, **kwargs):
        kwargs["judge_fn"] = lambda *args: {"passed": True, "violations": []}
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(module.FollowupRunner, "__init__", offline_init)
    return module


@pytest.mark.parametrize("execute,bad_output", [(False, None), (True, None),
    (True, '{"next_step":"ask_clarification","reason":"unit","reply":"当前设置？","questions":["当前设置？","当前设置？"]}'),
    (True, '{"next_step":'),
    (True, '{"reason":"需要更多证据","query":"只读查询超时","missing_evidence":"适用案例"}'),
    (True, '[{"next_step":"search_cases","reason":"需要更多证据","query":"只读查询超时","missing_evidence":"适用案例"}]')])
def test_followup_snapshot_and_model_selected_research(tmp_path, monkeypatch, execute, bad_output):
    script = load_script(monkeypatch)
    corpus = load_sources(ProcessingSettings().corpus_path)
    old = Clarification(next_step="ask_clarification", reason="合成追问", reply="当前代理配置和超时设置是什么？",
                        questions=["当前代理配置和超时设置是什么？"])
    understanding = TicketUnderstanding(summary="合成理解", error_codes=[], environment=[])
    class BaseRunner:
        def __init__(self, *args, **kwargs):
            pass
        metadata = {"agent_version": "synthetic", "corpus_version": corpus.version,
                    "retrieval_mode": "dense", "model_config": {"synthetic": True}}
        def __call__(self, snapshot):
            return RunOutput({"proposal": old, "understanding": understanding}, [], {"synthetic": True})
    monkeypatch.setattr(script, "HistoricalClarificationRunner", BaseRunner)
    dev_acceptance.write_json(tmp_path / "reviews.json", {"clarification": {
        "proposal_hash": calculate_request_fingerprint(old.model_dump()), "request": {"decision": "approve"}}})
    vectors, decisions = [], []
    def embed(text):
        vectors.append(text)
        return [1.0] * 1024
    monkeypatch.setattr(dev_acceptance, "build_budgeted_embeddings", lambda *args: SimpleNamespace(embed_query=embed))
    def understand(**kwargs):
        assert kwargs["settings"].model == "qwen3.7-flash"
        return understanding
    monkeypatch.setattr(dev_acceptance, "understand_ticket", understand)
    class Milvus:
        def search(self, **kwargs):
            return [[{"entity": {"source_id": source, "text": build_case_text(corpus.cases[source])}, "distance": 0.7}
                     for source in ("SYN-HIST-V2-006", "SYN-HIST-V2-007")]]
        def close(self):
            pass
    monkeypatch.setattr(script, "build_milvus_client", lambda settings: Milvus())
    resolution = Resolution(next_step="propose_resolution", reason="合成建议", reply="只读核对本次等待上限。",
                            evidence_ids=["SYN-HIST-V2-006"],
                            evidence_quotes={"SYN-HIST-V2-006": corpus.cases["SYN-HIST-V2-006"].resolution.summary})
    def generate(**kwargs):
        assert kwargs["settings"].model == "qwen3.8-27b"
        state = json.loads(kwargs["user_prompt"])
        decisions.append(state)
        if len(decisions) == 1:
            assert all(h["source_id"] != "SYN-HIST-V2-006" for h in state["evidence"])
            result = {"next_step": "search_cases", "query": "只读全年聚合报表 E_TIMEOUT 2 秒 4 秒",
                      "reason": "需要匹配案例", "missing_evidence": "客户端等待上限案例"}
        else:
            assert any(h["source_id"] == "SYN-HIST-V2-006" for h in state["evidence"])
            assert state["clarification_rounds"] == 1
            assert state["approved_clarifications"] == [old.reply]
            result = resolution.model_dump()
        raw = bad_output or json.dumps(result, ensure_ascii=False)
        kwargs["response_callback"]({"request_id": "synthetic", "usage": None,
            "choices": [{"content": raw, "finish_reason": "stop"}]})
        return raw
    monkeypatch.setattr(dev_acceptance, "generate_text", generate)
    qwen = QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unit", DASHSCOPE_WORKSPACE_ID="unit")
    review = {"proposal_hash": calculate_request_fingerprint(resolution.model_dump()), "request": {"decision": "approve"}}
    arguments = dict(execute=execute, ceilings=dict(zip(script.CATEGORIES, (1, 1, 1, 2))),
                     final_review=review if execute else None, directory=tmp_path, qwen=qwen,
                     decision_model="qwen3.8-27b")
    if bad_output:
        with pytest.raises(AssertionError, match="原始响应与失败状态已导出"):
            script.verify(**arguments)
        report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
        assert report["raw_decision_responses"][0]["response"]["choices"][0]["content"] == bad_output
        assert report["run"]["run_status"] == "failed" and report["proposal_hash"] is None
        assert report["run"]["usage"]["acceptance_decisions"][0]["validation"]["status"] == "rejected"
        if bad_output.startswith("["):
            assert report["run"]["usage"]["acceptance_decisions"][0]["validation"]["errors"][0]["type"] == "dict_type"
            assert len(vectors) == 1  # Initial vector only; never execute an implicitly unwrapped search.
        assert report["run"]["published_message_id"] is None and report["run"]["review"] is None
        assert not report["research_exercised"] and not report["expected_action_match"]
        assert report["ticket_before_review"]["version"] == 3
        assert len(report["ticket_before_review"]["messages"]) == 3
        assert len(decisions) == 1 and len(report["new_attempts"]) == 3
        return
    report = script.verify(**arguments)
    assert report["ticket_after_update"]["version"] == 3
    assert report["ticket_after_update"]["status"] == "open"
    assert len(report["new_attempts"]) == (5 if execute else 0)
    if execute:
        assert report["run"]["models"]["decision"] == "qwen3.8-27b"
        assert report["run"]["models"]["understanding"] == "qwen3.7-flash"
        assert report["research_exercised"] and report["required_evidence_cited"]
        assert len(vectors) == len(decisions) == 2
        assert len(report["retrieval_fixture_audit"][0]["visible_results"][0]) == 1
        assert len(report["retrieval_fixture_audit"][1]["visible_results"][0]) == 2
        assert report["business_review"]["ticket"]["version"] == 4
        assert report["business_review"]["ticket"]["status"] == "open"
    else:
        assert vectors == decisions == []
