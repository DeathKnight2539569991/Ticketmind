"""Customer continuation plus controlled first-search miss. Default zero paid calls.

--prepare replays the already-approved clarification through real HTTP/DB and
appends the fixed synthetic customer update, then checks exact future cache keys.
--execute additionally runs the real Agent, with explicitly bounded new calls.
"""
import argparse
import json
import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from check_m2_acceptance import ROOT, OUTPUT, load_cases, apply_human_review
from ticketmind.agent.decide import DECISION_PROTOCOL
from ticketmind.agent.dev_acceptance import AcceptanceAdapters, AttemptLedger, CATEGORIES, acceptance_lock, write_json
from ticketmind.agent.dev_cache import CachedUnderstanding, CachedQueryEmbeddings
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.run_cache import understanding_fingerprint, query_fingerprint, calculate_request_fingerprint
from ticketmind.agent.runtime import AgentRunner
from ticketmind.agent.runtime import RunOutput
from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import QwenSettings, MilvusSettings, ProcessingSettings, AuthSettings
from ticketmind.db.testing import isolated_database
from ticketmind.main import create_app
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.tickets.models import ProcessingResult

ZERO = {c: 0 for c in CATEGORIES}
# Original 7 attempts remain in the SAME ledger. This new scenario adds <=6.
FOLLOWUP_CEILINGS = dict(zip(CATEGORIES, (3, 3, 1, 6)))
RECHECK_CEILINGS = dict(zip(CATEGORIES, (3, 3, 1, 8)))  # 11 recorded attempts + <=4 new.
BOUNDARY_CEILINGS = dict(zip(CATEGORIES, (3, 3, 1, 10)))  # 13 recorded attempts + <=4 new; separate approval.
MODEL_COMPARISON_CEILINGS = dict(zip(CATEGORIES, (3, 3, 1, 12)))  # 15 attempts + <=4, qwen3.8-27b comparison.
MAX_COMPARISON_CEILINGS = dict(zip(CATEGORIES, (3, 3, 1, 13)))  # 16 attempts + <=4, qwen3.8-max-0902 comparison.
GLM_COMPARISON_CEILINGS = dict(zip(CATEGORIES, (3, 3, 1, 14)))  # 17 attempts + <=4, GLM-5.2; failed alias counts too.


def load_case():
    return json.loads((ROOT / "docs/m2-followup-case.json").read_text(encoding="utf-8"))


class FirstSearchMiss:
    def __init__(self, client, source_id, audit):
        self.client, self.source_id, self.audit = client, source_id, audit
        self.first = True

    def search(self, **kwargs):
        actual = self.client.search(**kwargs)
        # Preserve the true Milvus response outside model input. Never edit a collection.
        visible = [[hit for hit in group if not self.first or hit["entity"]["source_id"] != self.source_id]
                   for group in actual]
        self.audit.append({"fixture_applied": self.first, "actual_results": actual,
                           "visible_results": visible})
        self.first = False
        return visible

    def close(self):
        self.client.close()


class FollowupRunner(AgentRunner):
    def __init__(self, *args, decision_model=None, **kwargs):
        kwargs.setdefault("corpus", load_sources(args[2].corpus_path))
        super().__init__(*args, **kwargs)
        self.decision_model = decision_model

    @property
    def metadata(self):
        metadata = super().metadata
        metadata["model_config"]["acceptance_fixture"] = load_case()["initial_search_fixture"]
        if self.decision_model:
            metadata["model_config"]["decision"] = self.decision_model
        return metadata


class HistoricalClarificationRunner:
    """Replay the approved historical output explicitly, never relabel its prompt.

After a protocol change, current decision fingerprints intentionally differ.
The fixed original report is historical evidence, not a current-model cache hit.
"""
    def __init__(self, path):
        self.report = json.loads(path.read_text(encoding="utf-8"))
        run = self.report["run"]
        self.metadata = {"agent_version": run["agent_version"], "corpus_version": run["corpus_version"],
            "retrieval_mode": run["retrieval_mode"], "model_config": {**run["models"], "historical_report_replay": True}}

    def __call__(self, snapshot):
        if any(snapshot[key] != self.report["input_snapshot"][key] for key in ("subject", "body")):
            raise ValueError("旧审核记录与当前初始输入不一致")
        run = self.report["run"]
        if run["proposal"]["next_step"] != "ask_clarification":
            raise ValueError("必须从已批准的旧追问接续")
        return RunOutput({"proposal": proposal_adapter.validate_python(run["proposal"]),
            "understanding": TicketUnderstanding.model_validate(run["extracted_information"]),
            "tool_calls": run["tool_calls"]}, run["retrieval_evidence"],
            {"historical_report_replay": True, "original_usage": run["usage"]})


def start(client, ticket_id):
    ticket = client.get(f"/tickets/{ticket_id}").json()
    response = client.post(f"/tickets/{ticket_id}/runs", json={"expected_version": ticket["version"],
        "trigger_message_id": ticket["messages"][-1]["id"]}, headers={"Idempotency-Key": uuid4().hex})
    assert response.status_code == 201, response.text
    return response.json()


def verify(*, execute=False, ceilings=None, final_review=None, directory=OUTPUT, qwen=None, decision_model=None):
    qwen, config, case = qwen or QwenSettings(), ProcessingSettings(), load_case()
    directory = Path(directory)
    report = {"case": case, "decision_protocol": DECISION_PROTOCOL,
              "verification": "real_asgi_postgresql_milvus_controlled_first_search_miss",
              "business_review": "not_executed", "retrieval_fixture_audit": []}
    ledger = AttemptLedger(directory / "attempts.json", ceilings or ZERO)
    initial_count = len(ledger.data["attempts"])
    # Preserve the already-approved historical proposal across prompt changes.
    base_path = ROOT / case["approved_base_report"]
    base_runner = HistoricalClarificationRunner(base_path)
    report["base_replay"] = {"path": str(base_path), "kind": "historical_model_and_evidence_snapshot", "new_calls": 0}
    auth = AuthSettings(_env_file=None, operator_token=secrets.token_urlsafe(32), reviewer_token=secrets.token_urlsafe(32))
    try:
        with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (_, factory, schema):
            report["temporary_schema"] = schema
            app = create_app(session_factory=factory, runner=base_runner, auth_settings=auth, processing_settings=config)
            with TestClient(app) as client:
                client.headers["Authorization"] = "Bearer " + auth.operator_token.get_secret_value()
                created = client.post("/tickets", json=load_cases()[0]["input"], headers={"Idempotency-Key": uuid4().hex})
                assert created.status_code == 201
                ticket_id = created.json()["id"]
                original = start(client, ticket_id)
                assert original["run_status"] == "waiting_review", original
                approved = json.loads((directory / "reviews.json").read_text(encoding="utf-8"))["clarification"]
                report["original_review"] = apply_human_review(client, ticket_id, original, approved, auth)
                assert report["original_review"]["ticket"]["status"] == "awaiting_customer"
                response = client.post(f"/tickets/{ticket_id}/messages", json={"expected_version": 2,
                    "kind": "customer_update", "body": case["customer_update"]}, headers={"Idempotency-Key": uuid4().hex})
                assert response.status_code == 201
                ticket = client.get(f"/tickets/{ticket_id}").json()
                assert ticket["version"] == 3 and ticket["status"] == "open" and len(ticket["messages"]) == 3
                report["ticket_after_update"] = ticket
                body = "\n\n".join(f'[{m["sequence_number"]} {m["author_type"]}]\n{m["body"]}' for m in ticket["messages"])
                initial = {"subject": ticket["subject"], "body": body}
                adapters = AcceptanceAdapters(qwen, directory, ledger)
                decision_settings = qwen.model_copy(update={"model": decision_model}) if decision_model else qwen
                decision_adapters = AcceptanceAdapters(decision_settings, directory, ledger)
                report["models"] = {"understanding": qwen.model, "decision": decision_settings.model,
                                    "embedding": qwen.embedding_model}
                uf = understanding_fingerprint(settings=qwen, **initial)
                query = build_retrieval_query(**initial)
                qf = query_fingerprint(settings=qwen, query=query)
                report["preflight"] = {"understanding_fingerprint": uf, "query_fingerprint": qf,
                    "understanding_cache": bool(CachedUnderstanding(adapters.path("understanding", uf)).read(settings=qwen, **initial)),
                    "initial_vector_cache": bool(CachedQueryEmbeddings(qwen, adapters.path("query", qf), factory=None).read(query))}
                if not execute:
                    return report
                def milvus(settings):
                    return FirstSearchMiss(build_milvus_client(settings), case["initial_search_fixture"]["source_id"],
                                           report["retrieval_fixture_audit"])
                app.state.runner = FollowupRunner(qwen, MilvusSettings(), config,
                    understanding_fn=adapters.understanding, embedding_factory=adapters.embeddings,
                    decision_fn=decision_adapters.decision, milvus_factory=milvus,
                    decision_model=decision_settings.model)
                result = start(client, ticket_id)
                report["run"] = result
                report["ticket_before_review"] = client.get(f"/tickets/{ticket_id}").json()
                with factory() as session:
                    saved = session.get(ProcessingResult, UUID(result["id"]))
                    report["input_snapshot"] = saved.input_snapshot
                    assert saved.input_snapshot["body"] == body
                    assert saved.input_snapshot["clarification_rounds"] == 1
                    assert saved.input_snapshot["approved_clarifications"] == [original["final_reply"]]
                    assert saved.thread_id != original["thread_id"] and saved.run_sequence == 2
                raw_decisions = []
                report["raw_decision_responses"] = []
                for item in result["usage"].get("acceptance_decisions", []):
                    raw = json.loads(adapters.path("decision", item["fingerprint"]).read_text(encoding="utf-8"))
                    report["raw_decision_responses"].append(raw)
                    choices = raw["response"].get("choices", [])
                    try:
                        parsed = json.loads(choices[0].get("content") or "") if choices else None
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        raw_decisions.append(parsed)
                report["raw_decisions"] = raw_decisions
                proposal = result.get("proposal") or {}
                report["proposal_hash"] = calculate_request_fingerprint(proposal) if proposal else None
                searches = [c for c in result["tool_calls"] if c["tool"] == "search_cases" and c["status"] == "succeeded"]
                report["research_exercised"] = len(searches) == 2 and any(d.get("next_step") == "search_cases" for d in raw_decisions)
                report["expected_action_match"] = proposal.get("next_step") == case["expected_action"]
                report["required_evidence_cited"] = case["required_source_id"] in proposal.get("evidence_ids", [])
                assert result["run_status"] == "waiting_review", "运行未进入待审；原始响应与失败状态已导出"
                assert report["research_exercised"], "模型未选择并完成重检索；不伪造或重试"
                assert report["expected_action_match"] and report["required_evidence_cited"]
                if final_review:
                    report["business_review"] = apply_human_review(client, ticket_id, result, final_review, auth)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        report["new_attempts"] = ledger.data["attempts"][initial_count:]
        report["followup_cache_hits"] = ledger.cache_hits
        write_json(directory / "reports" / f'followup-{"execute" if execute else "prepare"}-{uuid4().hex}.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="真实 DB/只读 Milvus + 旧结果缓存；只准备新快照，新增调用0")
    parser.add_argument("--execute", action="store_true")
    budgets = parser.add_mutually_exclusive_group()
    budgets.add_argument("--allow-followup-budget", action="store_true", help="首次实验历史预算，累计上限3/3/1/6")
    budgets.add_argument("--allow-recheck-budget", action="store_true", help="修复后另获≤4次授权才可用，累计上限3/3/1/8")
    budgets.add_argument("--allow-boundary-budget", action="store_true", help="动作边界复验另获≤4次授权才可用，累计上限3/3/1/10")
    budgets.add_argument("--allow-model-comparison-budget", action="store_true", help="qwen3.8-27b对比授权≤4次，累计上限3/3/1/12")
    budgets.add_argument("--allow-max-comparison-budget", action="store_true", help="qwen3.8-max-0902对比授权≤4次，累计上限3/3/1/13")
    budgets.add_argument("--allow-glm-comparison-budget", action="store_true", help="GLM-5.2对比授权≤4次（含失败别名请求），累计上限3/3/1/14")
    parser.add_argument("--decision-model", help="只替换决策模型；理解与Embedding设置保持原值")
    parser.add_argument("--review", type=Path, help="对新提案摘要的具体人工审核，默认不发布新回复")
    args = parser.parse_args()
    if args.allow_model_comparison_budget and args.decision_model != "qwen3.8-27b":
        parser.error("本次对比授权仅适用于 --decision-model qwen3.8-27b")
    if args.allow_max_comparison_budget and args.decision_model != "qwen3.8-max-0902":
        parser.error("本次Max对比授权仅适用于 --decision-model qwen3.8-max-0902")
    if args.allow_glm_comparison_budget and args.decision_model not in ("glm5.2", "glm-5.2"):
        parser.error("本次GLM对比授权仅适用于 --decision-model glm-5.2（旧别名glm5.2保留历史记录）")
    if not args.prepare and not args.execute:
        print(json.dumps({"case": load_case(), "additional_maximum": {"understanding": 1,
            "initial_embedding": 1, "research_embedding": 1, "decision": 3}}, ensure_ascii=False))
        return
    review = json.loads(args.review.read_text(encoding="utf-8")) if args.review else None
    with acceptance_lock(OUTPUT):
        ceilings = (GLM_COMPARISON_CEILINGS if args.allow_glm_comparison_budget else
                    MAX_COMPARISON_CEILINGS if args.allow_max_comparison_budget else
                    MODEL_COMPARISON_CEILINGS if args.allow_model_comparison_budget else
                    BOUNDARY_CEILINGS if args.allow_boundary_budget else
                    RECHECK_CEILINGS if args.allow_recheck_budget else
                    FOLLOWUP_CEILINGS if args.allow_followup_budget else ZERO)
        report = verify(execute=args.execute, ceilings=ceilings, final_review=review, decision_model=args.decision_model)
        print(json.dumps({"preflight": report["preflight"], "new_attempts": len(report["new_attempts"]),
                          "research_exercised": report.get("research_exercised"),
                          "business_review_applied": isinstance(report["business_review"], dict)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
