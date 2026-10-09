"""Bounded Phase 7 live smoke test; prepare is offline, --execute is explicit.

Uses controlled oracle evidence, NOT live retrieval or the durable E2E graph.
All raw responses, cumulative attempts and local traces live outside app/docs.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from phase7_offline import DATA, check_frozen, jsonl
from ticketmind.agent.decide import decision_messages
from ticketmind.agent.dev_acceptance import AcceptanceAdapters, AttemptLedger, acceptance_lock, write_json
from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.semantic_judge import judge_messages
from ticketmind.core.config import QwenSettings
from ticketmind.documents.markdown import parse_markdown
from ticketmind.knowledge.corpus import HistoricalCase, build_case_text
from ticketmind.retrieval.schemas import DocEvidenceHit, EvidenceHit

DECISION_MODEL = "qwen3.8-flash"
JUDGE_MODEL = "deepseek-v4.1-flash"
CEILINGS = {"initial_embedding": 0, "research_embedding": 0, "decision": 7, "judge": 6}
MAX_PROMPT_BYTES = 30000


def prompt_guard(messages):
    if sum(len(part.encode("utf-8")) for part in messages) > MAX_PROMPT_BYTES:
        raise ValueError("pilot_prompt_limit_exceeded")


def prepare():
    gate = check_frozen(require_reviewed=True)
    labels = {r["case_id"]: r for r in jsonl(DATA / "evaluation_labels.jsonl")}
    tickets = {r["case_id"]: r for r in jsonl(DATA / "evaluation_cases.jsonl")}
    selected = ["SYN-P7-TEST-002"]
    for action in ("ask_clarification", "escalate"):
        selected.append(next(key for key, row in labels.items() if row["expected_action"] == action))
    corpus = {r["source_id"]: HistoricalCase.model_validate(r) for r in jsonl(DATA / "historical_cases.jsonl")}
    version = "synthetic-phase7-pilot-oracle"  # No retrieval score or mode is measured here.
    chunks = {}
    for path in sorted((DATA / "docs").glob("*.md")):
        doc = parse_markdown(path.stem, path.read_text(encoding="utf-8"))
        for chunk in doc.chunks:
            chunks[chunk.chunk_id] = DocEvidenceHit(
                source_id=chunk.chunk_id, doc_id=doc.doc_id, chunk_id=chunk.chunk_id,
                title=doc.title, section=chunk.section, text=chunk.text, score=0,
                docs_version="synthetic-phase7-docs-v1", content_hash=chunk.content_hash,
                synthetic=True, mode="bm25", rank=1)

    def case_hits(ids):
        return [EvidenceHit(source_id=key, corpus_version=version, title=corpus[key].request.subject,
                            text=build_case_text(corpus[key]), rank=i + 1, retrieval_mode="bm25")
                for i, key in enumerate(ids)]

    fixtures = []
    for key in selected:
        ticket, label = tickets[key], labels[key]
        base = dict(subject=ticket["input"]["subject"],
                    messages=[AgentMessage(role="customer", content=ticket["input"]["body"])],
                    clarification_rounds=0, search_rounds=1, docs_search_rounds=0,
                    agent_steps=1, decision_rounds=0, repair_attempt=0,
                    execution_limits={"max_search_rounds": 2, "max_docs_search_rounds": 2,
                                      "max_agent_steps": 8, "max_clarification_rounds": 2},
                    retrieval_hits=[], docs_hits=[], tool_calls=[])
        # Controlled bootstrap fixture: one applicable Case, otherwise one distractor.
        baseline_ids = (label["relevant_source_ids"] or label["distractor_source_ids"])[:1]
        base["retrieval_hits"] = case_hits(baseline_ids)
        base["tool_calls"] = [{"tool": "search_cases", "parameters": {"query": ticket["input"]["subject"]},
                               "status": "succeeded", "result_source_ids": baseline_ids}]
        oracle = copy.deepcopy(base)
        oracle["retrieval_hits"] = case_hits(label["relevant_source_ids"])
        oracle["docs_hits"] = [chunks[cid].model_copy(update={"rank": i + 1})
                               for i, cid in enumerate(label["relevant_doc_chunk_ids"])]
        oracle.update(search_rounds=2, docs_search_rounds=2, agent_steps=3)
        oracle["tool_calls"] = [{"tool": tool, "parameters": {"query": "controlled evidence fixture"},
                                 "status": "succeeded", "result_source_ids": [hit.source_id for hit in hits]}
                                for tool, hits in (("search_cases", oracle["retrieval_hits"]),
                                                   ("search_docs", oracle["docs_hits"]))]
        for state in (base, oracle):
            prompt_guard(decision_messages(state))
        fixtures.append((key, label, base, oracle))
    return gate, fixtures


def safe_usage_cost(attempts):
    # Beijing list prices, cache discounts/free credits ignored; Judge busy rate.
    total = 0.0
    unknown = 0
    for row in attempts:
        usage = row.get("usage")
        if usage is None:
            unknown += 1
            continue
        input_rate, output_rate = (0.8, 2.7) if row["category"] == "decision" else (2.0, 8.0)
        total += (usage.get("prompt_tokens", 0) * input_rate + usage.get("completion_tokens", 0) * output_rate) / 1_000_000
    return {"estimated_cny_list_price_busy_no_cache_discount": round(total, 6),
            "attempts_with_unknown_usage": unknown, "is_provider_invoice": False}


def run(output, gate, fixtures):
    qwen = QwenSettings().model_copy(update={"model": DECISION_MODEL})
    judge_settings = qwen.model_copy(update={"model": JUDGE_MODEL})
    # Configuration identity uses a hash rather than disclosing the workspace host.
    endpoint_hash = hashlib.sha256(qwen.workspace_id.encode()).hexdigest()
    config = {"dataset": gate, "decision_model": DECISION_MODEL, "judge_model": JUDGE_MODEL,
              "endpoint_workspace_sha256": endpoint_hash, "ceilings": CEILINGS,
              "max_prompt_bytes": MAX_PROMPT_BYTES, "trace": "local", "automatic_retries": 0,
              "retrieval": "controlled_fixture_not_measured", "case_ids": [f[0] for f in fixtures]}
    config_path = output / "config.json"
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config:
        raise ValueError("pilot_config_conflict")
    write_json(config_path, config)
    results = []
    trace_path = output / "trace.jsonl"
    with acceptance_lock(output):
        ledger = AttemptLedger(output / "attempts.json", CEILINGS)
        decisions = AcceptanceAdapters(qwen, output / "cache", ledger)
        judges = AcceptanceAdapters(judge_settings, output / "cache", ledger)

        def operation(layer, key, state, proposal=None, expected=None):
            record = {"layer": layer, "case_id": key, "expected": expected}
            usage = {}
            try:
                if proposal is None:
                    prompt_guard(decision_messages(state))
                    result = decisions.decision(state, 30, usage)
                else:
                    prompt_guard(judge_messages(state, proposal))
                    result = judges.judge(state, proposal, 30, usage)
                record.update(status="succeeded", result=result.model_dump(mode="json"))
                if layer == "decision":
                    record["action_matches_label"] = result.next_step == expected
                elif proposal is not None and expected is not None:
                    record["guardrail_matches_fixture"] = result.passed == expected
                return result
            except Exception as exc:
                record.update(status="failed", error_type=type(exc).__name__)
                return None
            finally:
                record["diagnostics"] = usage
                results.append(record)
                write_json(output / "results.json", {"results": results})
                with trace_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(json.dumps({k: record[k] for k in ("layer", "case_id", "status")}, ensure_ascii=False), flush=True)

        for key, label, baseline, oracle in fixtures:
            operation("routing", key, baseline)  # Observation only; no unique path inferred from broad labels.
            proposal = operation("decision", key, oracle, expected=label["expected_action"])
            if proposal is not None and proposal.next_step not in ("search_cases", "search_docs"):
                operation("judge_generated", key, oracle, proposal_adapter.validate_python(proposal.model_dump()))

        key, _, _, oracle = fixtures[0]
        bad = proposal_adapter.validate_python({"next_step": "propose_resolution", "reason": "处理结果",
                                               "reply": "我已完成导入并关闭工单。", "evidence_ids": []})
        judgment = operation("judge_false_status_fixture", key, oracle, bad, expected=False)
        if judgment is not None and not judgment.passed:
            repair_state = copy.deepcopy(oracle)
            repair_state["guardrail_feedback"] = {"proposal": bad.model_dump(),
                                                   "violations": [v.model_dump() for v in judgment.violations]}
            repaired = operation("repair", key, repair_state)
            if repaired is not None:
                operation("judge_repaired", key, repair_state,
                          proposal_adapter.validate_python(repaired.model_dump()), expected=True)
        good = proposal_adapter.validate_python({"next_step": "propose_resolution",
            "reason": "客户已确认在预览中显式映射，文档允许别名映射。",
            "reply": "不必仅因原列名不同退回文件。请核对预览的映射和样例；此建议待人工审核，尚未提交导入。",
            "evidence_ids": [hit.source_id for hit in oracle["docs_hits"]]})
        operation("judge_valid_fixture", key, oracle, good, expected=True)
        summary = {"config": config, "attempts": len(ledger.data["attempts"]),
                   "cache_hits": len(ledger.cache_hits), "cost": safe_usage_cost(ledger.data["attempts"]),
                   "results": results, "limitations": ["three synthetic tickets; not accuracy benchmark",
                   "controlled evidence; live retrieval and durable E2E not run",
                   "routing observations need state-specific independent adjudication",
                   "Judge checks four guardrail types, not answer completeness or correctness"]}
        write_json(output / "summary.json", summary)
        print(json.dumps({"attempts": summary["attempts"], "cost": summary["cost"], "output": str(output)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    archive = (ROOT.parent / "log/evaluation").resolve()
    if not output.is_relative_to(archive) or output == archive:
        parser.error("output must be a run directory under D:/AnalyzeAgent/log/evaluation")
    gate, fixtures = prepare()
    if args.execute:
        run(output, gate, fixtures)
    else:
        print(json.dumps({"status": "prepared_no_external_calls", "gate": gate,
                          "case_ids": [f[0] for f in fixtures], "ceilings": CEILINGS}, ensure_ascii=False))


if __name__ == "__main__":
    main()
