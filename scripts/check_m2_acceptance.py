"""M2 acceptance. Default: offline preflight. Real calls require explicit ceilings.

Expected labels stay outside AgentRunner. Review files bind a human decision to
an exact proposal hash. Temporary business records are exported before cleanup.
"""
import argparse
import json
import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from ticketmind.agent.dev_acceptance import (AcceptanceAdapters, AttemptLedger, CATEGORIES,
                                            acceptance_lock, write_json)
from ticketmind.agent.dev_cache import CachedQueryEmbeddings
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.run_cache import calculate_request_fingerprint, query_fingerprint
from ticketmind.agent.runtime import AgentRunner
from ticketmind.agent.schemas import AgentMessage
from ticketmind.api.schemas.runs import review_create_adapter
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.testing import isolated_database
from ticketmind.main import create_app
from ticketmind.knowledge.sources import load_sources
from ticketmind.knowledge.seed import seed_knowledge
from ticketmind.tickets.models import ProcessingResult

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/cache/m2/acceptance"
LEGACY = ROOT / "data/cache/graph/api_timeout"


def load_cases():
    return json.loads((ROOT / "docs/m2-acceptance-cases.json").read_text(encoding="utf-8"))["cases"]


def preflight(settings, cases):
    rows = []
    for case in cases:
        subject, body = case["input"]["subject"], case["input"]["body"]
        messages = [AgentMessage(role="customer", content=body)]
        query = build_retrieval_query(subject=subject, messages=messages)
        qf = query_fingerprint(settings=settings, query=query)
        adapter = AcceptanceAdapters(
            settings,
            OUTPUT,
            None,
            legacy_directory=LEGACY if case["id"] == "clarification" else None,
        )
        q_path = LEGACY / "query.json" if case["id"] == "clarification" else adapter.path("query", qf)
        vector = CachedQueryEmbeddings(settings, q_path, factory=None).read(query)
        if case["id"] == "clarification" and vector is None:
            raise RuntimeError("必须保留并复用匹配的 M0 查询向量缓存；本方案不授权补调该输入")
        rows.append(
            {
                "case": case["id"],
                "initial_vector_cache": bool(vector),
                "query_fingerprint": qf,
                "decision_cache": "requires_actual_evidence_and_loop_state",
            }
        )
    return rows


def apply_human_review(client, ticket_id, result, review, auth):
    if review["proposal_hash"] != calculate_request_fingerprint(result["proposal"]):
        raise ValueError("人工审核不对应本次原始提案")
    ticket = client.get(f"/tickets/{ticket_id}").json()
    before_version, before_messages = ticket["version"], len(ticket["messages"])
    payload = review_create_adapter.validate_python({"expected_version": ticket["version"], **review["request"]}).model_dump(mode="json")
    headers = {"Authorization": "Bearer " + auth.reviewer_token.get_secret_value(), "Idempotency-Key": uuid4().hex}
    url = f'/tickets/{ticket_id}/runs/{result["id"]}/review'
    approved = client.post(url, json=payload, headers=headers)
    assert approved.status_code == 201, approved.text
    applied = approved.json()
    assert applied["run_status"] == "completed", applied
    repeated = client.post(url, json=payload, headers=headers)
    assert repeated.status_code == 200 and repeated.json() == applied
    ticket = client.get(f"/tickets/{ticket_id}").json()
    action = ("escalate" if payload["decision"] == "escalate" else
              payload["final_action"] if payload["decision"] == "edit" else
              result["proposal"]["next_step"])
    expected = {"resolve": "open", "propose_resolution": "open",
                "ask_clarification": "awaiting_customer", "escalate": "escalated"}[action]
    assert ticket["status"] == expected and ticket["version"] == before_version + 1
    assert len(ticket["messages"]) == before_messages + 1
    assert applied["proposal"] == result["proposal"]
    assert "reason" not in applied and "final_reply" not in applied
    assert ticket["messages"][-1]["id"] == applied["published_message_id"]
    return {"run": applied, "ticket": ticket, "idempotent_review": True}


def execute_case(case, qwen, config, ledger, directory, review=None):
    adapters = AcceptanceAdapters(qwen, directory, ledger,
        legacy_directory=LEGACY if case["id"] == "clarification" else None)
    decisions_adapter = AcceptanceAdapters(qwen.model_copy(update={"model": config.decision_model}), directory, ledger)
    judges = AcceptanceAdapters(qwen.model_copy(update={"model": config.judge_model}), directory, ledger)
    runner = AgentRunner(qwen, MilvusSettings(), config,
                         embedding_factory=adapters.embeddings, decision_fn=decisions_adapter.decision,
                         judge_fn=judges.judge,
                         corpus=load_sources(config.corpus_path))
    auth = AuthSettings(_env_file=None, operator_token=secrets.token_urlsafe(32), reviewer_token=secrets.token_urlsafe(32))
    report = {"case": case, "verification": "asgi_http_real_postgresql_checkpointer_milvus_model_or_exact_cache",
              "model_quality": "pending_human_review", "business_review": "not_executed"}
    try:
        with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (_, factory, schema):
            seed_knowledge(factory, config.corpus_path)
            report["temporary_schema"] = schema
            app = create_app(session_factory=factory, runner=runner, auth_settings=auth, processing_settings=config)
            with TestClient(app) as client:
                client.headers["Authorization"] = "Bearer " + auth.operator_token.get_secret_value()
                created = client.post("/tickets", json=case["input"], headers={"Idempotency-Key": uuid4().hex})
                assert created.status_code == 201, created.text
                ticket_id = created.json()["id"]
                ticket = client.get(f"/tickets/{ticket_id}").json()
                response = client.post(f"/tickets/{ticket_id}/runs", json={"expected_version": ticket["version"],
                    "trigger_message_id": ticket["messages"][-1]["id"]}, headers={"Idempotency-Key": uuid4().hex})
                assert response.status_code == 201, response.text
                result = response.json()
                report["run"] = result
                report["ticket_before_review"] = client.get(f"/tickets/{ticket_id}").json()
                with factory() as session:
                    saved = session.get(ProcessingResult, UUID(result["id"]))
                    assert saved.proposal == result["proposal"] and saved.retrieval_evidence == result["retrieval_evidence"]
                    report["input_snapshot"] = saved.input_snapshot
                if result["run_status"] == "waiting_review":
                    assert report["ticket_before_review"]["status"] == "open"
                    assert len(report["ticket_before_review"]["messages"]) == 1
                    report["proposal_hash"] = calculate_request_fingerprint(result["proposal"])
                    report["expected_action_match"] = result["proposal"]["next_step"] == case["expected_action"]
                    decisions = result["usage"].get("acceptance_decisions", [])
                    report["model_final_action"] = None
                    if decisions:
                        path = decisions_adapter.path("decision", decisions[-1]["fingerprint"])
                        raw = json.loads(path.read_text(encoding="utf-8"))["response"]["choices"][0]["content"]
                        report["model_final_action"] = json.loads(raw).get("next_step")
                    report["model_action_match"] = report["model_final_action"] == case["expected_action"]
                    # Matching an action/valid JSON is not a quality verdict.
                    if review:
                        report["business_review"] = apply_human_review(client, ticket_id, result, review, auth)
    except Exception as exc:
        report["error_type"] = type(exc).__name__  # Never dump transport/config exceptions with credentials.
        raise
    finally:
        report["cumulative_attempts"] = ledger.data["attempts"]
        report["session_cache_hits"] = ledger.cache_hits
        write_json(directory / "reports" / f'{case["id"]}-{uuid4().hex}.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="访问真实数据库/Milvus；默认新增调用上限仍为 0")
    parser.add_argument("--case", choices=[c["id"] for c in load_cases()], action="append")
    parser.add_argument("--reviews", type=Path, help="已人工审阅的 case -> proposal_hash/request JSON；默认不发布")
    parser.add_argument("--decision-model", default="qwen3.8-flash")
    parser.add_argument("--judge-model", default="deepseek-v4.1-flash")
    for category in CATEGORIES:
        parser.add_argument("--" + category.replace("_", "-") + "-ceiling", type=int, default=0)
    parser.add_argument("--judge-ceiling", type=int, default=0,
                        help="独立 Judge 新调用累计上限；旧 M2 额度不包含 Judge，默认 0")
    args = parser.parse_args()
    ceilings = {c: getattr(args, c + "_ceiling") for c in CATEGORIES}
    ceilings["judge"] = args.judge_ceiling
    maximum = {**dict(zip(CATEGORIES, (2, 3, 9))), "judge": 6}
    if any(v < 0 or v > maximum[c] for c, v in ceilings.items()):
        parser.error("上限须在方案范围内：首次向量 2、重检索向量 3、决策 9、Judge 6")
    cases = [c for c in load_cases() if not args.case or c["id"] in args.case]
    qwen = QwenSettings()
    config = ProcessingSettings(decision_model=args.decision_model, judge_model=args.judge_model)
    print(json.dumps({"preflight": preflight(qwen, cases), "new_call_ceilings": ceilings,
                      "decision_model": config.decision_model, "judge_model": config.judge_model}, ensure_ascii=False))
    if not args.execute:
        return
    reviews = json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else {}
    with acceptance_lock(OUTPUT):
        ledger = AttemptLedger(OUTPUT / "attempts.json", ceilings)
        for case in cases:
            report = execute_case(case, qwen, config, ledger, OUTPUT, reviews.get(case["id"]))
            business = report["business_review"]
            current_run = business["run"] if isinstance(business, dict) else report["run"]
            print(json.dumps({"case": case["id"], "run_status": current_run["run_status"],
                              "review_applied": isinstance(business, dict),
                              "action_match": report.get("expected_action_match"),
                              "quality": report["model_quality"]}, ensure_ascii=False))
            if (report["run"]["run_status"] != "waiting_review" or not report.get("expected_action_match")
                    or not report.get("model_action_match")):
                raise RuntimeError("验收失败或动作偏离预期；报告已保存，停止后续案例，不自动重试")


if __name__ == "__main__":
    main()
