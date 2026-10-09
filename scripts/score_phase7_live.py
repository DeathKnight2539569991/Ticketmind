"""Deterministic Phase 7 result summary. Reads local results; never calls models."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from phase7_offline import DATA, jsonl

def normalized_proposal(value):
    if not isinstance(value, dict):
        return value
    result = dict(value)
    result.setdefault("evidence_ids", [])
    if isinstance(result.get("evidence_ids"), list):
        result["evidence_ids"] = sorted(result["evidence_ids"])
    if result.get("next_step") == "escalate":
        result.pop("risk_flags", None)  # Runtime-derived field is absent from model output.
    return result


def score_row(result, label):
    run = result.get("run", result)
    proposal = run.get("proposal") or result.get("proposal") or {}
    decisions = result.get("decisions", [])
    initial = next((item.get("content") for item in decisions
                    if item.get("content") and not item.get("repair_turn") and
                    item["content"].get("next_step") not in {"search_cases", "search_docs"}), None)
    evidence = run.get("retrieval_evidence", result.get("retrieval_evidence", [])) or []
    ids = {item.get("source_id") for item in evidence if item.get("source_id")}
    case_ids = {source_id for source_id in ids if not source_id.startswith("docs:")}
    doc_ids = {source_id.removeprefix("docs:") for source_id in ids if source_id.startswith("docs:")}
    citations = set(proposal.get("evidence_ids", []))
    tools = run.get("tool_calls", result.get("tool_calls", [])) or []
    followup = tools[1:] if tools and tools[0].get("tool") == "search_cases" else tools
    followup_path = [item.get("tool") for item in followup
                     if item.get("tool") in {"search_cases", "search_docs"}]
    requested = [item.get("tool") for item in followup if item.get("tool") in {"search_cases", "search_docs"}]
    successful = [item.get("tool") for item in followup if item.get("status") == "succeeded" and
                  item.get("tool") in {"search_cases", "search_docs"}]
    allowed_paths = label.get("allowed_followup_tool_paths", [])
    judges = result.get("judges", [])
    violations = [violation for item in judges if item.get("content")
                  for violation in item["content"].get("violations", [])]
    all_expected = set(label.get("relevant_source_ids", []))
    docs_expected = set(label.get("relevant_doc_chunk_ids", []))
    cited_case_ids = citations & case_ids
    cited_doc_ids = {source_id.removeprefix("docs:") for source_id in citations if source_id.startswith("docs:")}
    cited_relevant = (cited_case_ids & all_expected) | (cited_doc_ids & docs_expected)
    return {
        "case_id": result.get("case_id"),
        "execution_status": result.get("status", run.get("run_status", "unknown")),
        "initial_final_candidate": initial,
        "final_proposal": proposal,
        "initial_to_final_changed": initial is not None and normalized_proposal(initial) != normalized_proposal(proposal),
        "expected_action": label.get("expected_action"),
        "final_action": proposal.get("next_step"),
        "action_match": proposal.get("next_step") == label.get("expected_action"),
        "answer_correctness": "pending_independent_review",
        "case_relevant_recall": len(case_ids & all_expected) / len(all_expected) if all_expected else None,
        "docs_relevant_recall": len(doc_ids & docs_expected) / len(docs_expected) if docs_expected else None,
        "citation_ids_exist_in_retrieved_evidence": citations <= ids,
        "cited_relevant_evidence_count": len(cited_relevant),
        "cited_relevant_evidence_precision": len(cited_relevant) / len(citations) if citations else None,
        "relevant_case_citations": sorted(cited_case_ids & all_expected),
        "relevant_doc_citations": sorted(cited_doc_ids & docs_expected),
        "citation_ids": sorted(citations),
        "relevant_case_ids_retrieved": sorted(case_ids & all_expected),
        "relevant_doc_ids_retrieved": sorted(doc_ids & docs_expected),
        "followup_requested_tool_path": requested,
        "successful_followup_tool_path": successful,
        "requested_followup_path_allowed_by_label": requested in allowed_paths,
        "successful_followup_path_allowed_by_label": successful in allowed_paths,
        "handoff_required": bool(label.get("requires_human_handoff")),
        "handoff_missed": bool(label.get("requires_human_handoff")) and proposal.get("next_step") != "escalate",
        "judge_violations_observed": violations,
        "judge_pass_is_not_answer_quality_score": True,
        "api_business_row_matches": result.get("business_row_matches_api"),
        "run_idempotency_replay": result.get("idempotency_key_replay"),
        "approval_simulation": result.get("approval_simulation"),
        "published_message_persisted": result.get("published_message_persisted_in_isolated_schema"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    results_path = run_dir / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8")).get("results", []) if results_path.exists() else []
    labels = {row["case_id"]: row for row in jsonl(DATA / "evaluation_labels.jsonl")}
    by_id = {row.get("case_id"): row for row in results}
    planned_ids = config.get("case_ids", sorted(labels))
    scored = [score_row(by_id[case_id], labels[case_id]) for case_id in planned_ids if case_id in by_id]
    review_path = run_dir / "independent-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {"per_case": []}
    reviewed = {row["case_id"]: row for row in review.get("per_case", [])}
    if len(reviewed) != len(review.get("per_case", [])) or set(reviewed) - set(planned_ids):
        raise ValueError("duplicate_or_unplanned_independent_review")
    for row in scored:
        assessment = reviewed.get(row["case_id"])
        if assessment is not None:
            if assessment.get("final_quality") not in ("pass", "fail", "not_scored"):
                raise ValueError("invalid_independent_quality_verdict")
            row["answer_correctness"] = assessment["final_quality"]
            row["independent_review"] = assessment
    completed = [row for row in scored if row["execution_status"] in ("succeeded", "waiting_review", "completed")]
    action_rows = [row for row in completed if row["action_match"] is not None]
    ledger = json.loads((run_dir / "attempts.json").read_text(encoding="utf-8")) if (run_dir / "attempts.json").exists() else {"attempts": []}
    calls_by_category = {category: [item for item in ledger.get("attempts", []) if item.get("category") == category]
                         for category in ("decision", "judge", "initial_embedding", "research_embedding")}
    tokens = {}
    for category, attempts in calls_by_category.items():
        tokens[category] = {"prompt": sum((item.get("usage") or {}).get("prompt_tokens",
                                           (item.get("usage") or {}).get("input_tokens", 0)) or 0 for item in attempts),
                            "completion": sum((item.get("usage") or {}).get("completion_tokens",
                                               (item.get("usage") or {}).get("output_tokens", 0)) or 0 for item in attempts),
                            "total": sum((item.get("usage") or {}).get("total_tokens", 0) or 0 for item in attempts),
                            "unknown_usage_calls": sum(item.get("usage") is None for item in attempts)}
    repair_calls = sum(item.get("repair_turn") is True for result in results for item in result.get("decisions", []))
    summary = {
        "evaluation_method": "deterministic label comparison plus separately recorded primary Agent semantic review",
        "dataset_snapshot_hash": config.get("snapshot_hash"),
        "planned_denominator": len(planned_ids), "recorded": len(scored),
        "succeeded": len(completed), "failed": len(scored) - len(completed),
        "not_run_case_ids": [case_id for case_id in planned_ids if case_id not in by_id],
        "action_matches": sum(row["action_match"] is True for row in action_rows),
        "action_accuracy_over_completed": (sum(row["action_match"] is True for row in action_rows) / len(action_rows)
                                           if action_rows else None),
        "handoff_required_count": sum(row["handoff_required"] for row in completed),
        "handoff_missed_count": sum(row["handoff_missed"] for row in completed),
        "all_citations_retrieved_count": sum(row["citation_ids_exist_in_retrieved_evidence"] for row in completed),
        "requested_followup_paths_allowed_count": sum(row["requested_followup_path_allowed_by_label"] for row in completed),
        "successful_followup_paths_allowed_count": sum(row["successful_followup_path_allowed_by_label"] for row in completed),
        "judge_violation_count": sum(len(row["judge_violations_observed"]) for row in completed),
        "model_calls": {category: len(items) for category, items in calls_by_category.items()},
        "repair_calls_in_decision_records": repair_calls,
        "tokens_by_category": tokens,
        "settled_cost_cny": ledger.get("settled_cost_cny", 0),
        "unsettled_reserved_cost_cny": sum(float(item.get("reserved_cny") or 0)
                                           for item in ledger.get("attempts", [])),
        "per_case": scored,
        "independent_review": {"path": str(review_path) if review_path.exists() else None,
            "reviewer": review.get("reviewer"), "human_quality_approval": False,
            "reviewed": len(reviewed), "not_reviewed_case_ids": [key for key in planned_ids if key not in reviewed],
            "initial_quality_passes": sum(item.get("initial_quality") == "pass" for item in reviewed.values()),
            "final_quality_passes": sum(item.get("final_quality") == "pass" for item in reviewed.values()),
            "final_quality_pass_fraction_over_planned": sum(item.get("final_quality") == "pass" for item in reviewed.values()) / len(planned_ids),
            "judge_false_negative_case_ids": [key for key, item in reviewed.items() if item.get("judge_false_negative_types")],
            "repair_guardrail_improved_case_ids": [key for key, item in reviewed.items() if item.get("repair_guardrail_improved")],
            "repair_answer_correct_case_ids": [key for key, item in reviewed.items() if item.get("repair_answer_correct")]},
        "limitations": ["Label status remains pending_review; reviewed hash binding is not human ground truth.",
                        "Semantic quality verdicts reflect the recorded Agent rubric and are not human or blind adjudication.",
                        "Judge violations are a guardrail signal; Judge PASS does not establish answer correctness.",
                        "Offline approval simulation only; no external reply was published."]
    }
    output = run_dir / "score-summary.json"
    summary["output"] = str(output)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("planned_denominator", "recorded", "succeeded", "failed",
        "not_run_case_ids", "action_accuracy_over_completed", "model_calls", "settled_cost_cny", "output")},
        ensure_ascii=False))


if __name__ == "__main__":
    main()
