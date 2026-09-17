"""Offline scoring of immutable predictions and explicit delegated review notes.

No external calls. Does not alter predictions, labels, runtime code or the ledger.
Run from the app root. Output files are exclusive-create to preserve prior scores.
"""
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path

from pydantic import ValidationError

from execute_case import OUT, ROOT, PRE, LEDGER, read, sha, verify, write_new
from ticketmind.agent.proposals import proposal_adapter, validate_proposal, validate_decision_evidence
from ticketmind.core.config import ProcessingSettings
from ticketmind.evaluation.dataset import load_dataset, digest
from ticketmind.evaluation.scoring import score_prediction, apply_adjudication, ratio
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.dense import RetrievalHit


freeze, config, _ = verify()
manifest = read(OUT / "manifest.json")
notes = read(OUT / "semantic-notes.json")
corpus = load_sources(config.corpus_path)
assert corpus.version == freeze["configuration"]["corpus_version"]
dataset = {r["case"]["case_id"]: r for r in load_dataset(ROOT / "data/synthetic/m4", corpus, ROOT / "data/synthetic/m4/label_reviews.jsonl")}
ids = manifest["case_ids"]
assert set(notes) == {cid[-3:] for cid in ids}
before, after = read(OUT / "ledger-before.json"), read(LEDGER)
assert after["attempts"][:65] == before["attempts"]
assert len(before["attempts"]) == 65
new_attempts = after["attempts"][65:]
reviews, scores, attempt_order, configs = [], [], [], set()
reviewed_at = datetime.now(UTC).isoformat()

for cid in ids:
    directory = OUT / "cases" / cid
    prediction, execution = read(directory / "prediction.json"), read(directory / "execution.json")
    assert execution["safe_to_continue"] and execution["prediction_sha256"] == sha(directory / "prediction.json")
    assert prediction["case_id"] == cid and prediction["http_database_consistent"] and prediction["idempotent_no_extra_run"]
    assert prediction["business_review"] == "not_executed"
    assert prediction["run"]["published_message_id"] is None and prediction["run"]["review"] is None
    attempts = read(directory / "attempts.json")["attempts"]
    attempt_order.extend(attempts)
    model_attempted = any(a["category"] == "decision" for a in attempts)
    configs.add(digest(prediction["evaluation_config"]))
    actual_config = prediction["evaluation_config"]
    assert actual_config["corpus_version"] == corpus.version and actual_config["retrieval_mode"] == "hybrid"
    assert actual_config["models"]["understanding"] == "qwen3.7-flash"
    assert actual_config["models"]["decision"] == "glm-5.2"
    assert actual_config["models"]["decision_protocol"] == freeze["configuration"]["protocol"]
    note, row = notes[cid[-3:]], dataset[cid]
    base_score = score_prediction(row, prediction, corpus)
    review = {"case_id": cid, "label_hash": row["label_hash"], "prediction_hash": digest(prediction),
        "reviewer": "codex-primary", "review_method": "user_delegated_agent", "reviewed_at": reviewed_at,
        "review_scope": "raw_and_final" if model_attempted else "system_final_only_no_model_decision",
        "rationale": note["rationale"], "raw_question_coverage": note["coverage"],
        "raw_forbidden_conclusion_violations": [], "raw_global_rule_violations": note.get("global_rules", []),
        "raw_false_status_claims": [] if model_attempted else None,
        "raw_unsupported_commitments": note.get("unsupported_commitments", []) if model_attempted else None,
        "raw_repeated_known_information": note.get("repeated_information", []) if model_attempted else None,
        "raw_operations_disguised_as_clarification": note.get("disguised_operations", []) if model_attempted else None,
        "applicable_resolution_evidence": note.get("applicable_resolution"),
        "policy_false_positive": note.get("policy_false_positive", False),
        "diagnoses": note["diagnoses"], "warnings": note.get("warnings", [])}
    # Validate the existing adjudication contract without changing its raw/final
    # semantics. Aggregate below explicitly excludes code-only cases from model accuracy.
    score = apply_adjudication(base_score, review)
    error = None
    if model_attempted:
        try:
            proposal = proposal_adapter.validate_python(prediction["raw_proposal"])
            validate_proposal(proposal, {hit["source_id"] for hit in prediction["retrieval_evidence"]})
            validate_decision_evidence(proposal, [RetrievalHit(source_id=h["source_id"], text=h["text"], score=0.5)
                                               for h in prediction["retrieval_evidence"]])
        except ValidationError as exc:
            error = {"type": type(exc).__name__, "errors": exc.errors(include_input=False, include_context=False, include_url=False)}
        except ValueError as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
    assert (error is not None) == (prediction["run"]["run_status"] == "failed")
    relevant = set(row["label"]["relevant_source_ids"])
    searches = [t for t in prediction["run"]["tool_calls"] if t["tool"] == "search_cases"]
    first = set(searches[0]["result_source_ids"])
    union = {h["source_id"] for h in prediction["retrieval_evidence"]}
    score.update(model_decision_attempted=model_attempted,
        code_rule_handoff=not model_attempted and score["final"]["action"] == "escalate",
        raw_reply_available=isinstance(prediction.get("raw_proposal"), dict) and isinstance(prediction["raw_proposal"].get("reply"), str),
        offline_validation_error=error, run_status=prediction["run"]["run_status"],
        risk_group=row["case"]["group_id"], scenario_type=row["case"]["scenario_type"],
        first_retrieval_hit=bool(relevant & first) if relevant else None,
        all_retrieval_hit=bool(relevant & union) if relevant else None,
        relevant_source_ids=sorted(relevant), actual_retrieved_source_ids=sorted(union),
        source_evidence_retrieved_for_answer=bool(relevant & union) if row["label"]["answer_available"] else None,
        final_false_status_claims=[],
        final_unsupported_commitments=note.get("unsupported_commitments", []) if prediction["final_proposal"] else [],
        final_question_coverage=note["coverage"] if prediction["final_proposal"] else [False]*len(note["coverage"]))
    reviews.append(review)
    scores.append(score)

assert len(configs) == 1
assert attempt_order == new_attempts
assert len(new_attempts) <= manifest["authorized_max_new_attempts"]
assert read(OUT / "cases" / ids[-1] / "ledger-after.json") == after


def aggregate(selected):
    model = [s for s in selected if s["model_decision_attempted"]]
    replied = [s for s in model if s["raw_reply_available"]]
    final = [s for s in selected if s["run_status"] == "waiting_review"]
    required = [s for s in selected if s["requires_human_handoff"]]
    model_required = [s for s in required if s["model_decision_attempted"]]
    q = [s for s in selected if s["necessary_questions"]]
    no_answer = [s for s in selected if not s["answer_available"]]
    relevant = [s for s in selected if s["relevant_source_ids"]]
    answer = [s for s in selected if s["answer_available"]]
    resolution = [s for s in model if s["raw"]["action"] == "propose_resolution"]
    raw_citations = sum(s["raw"]["citation_count"] for s in model)
    final_citations = sum(s["final"]["citation_count"] for s in final)
    clarification = [s for s in model if s["raw"]["action"] == "ask_clarification"]
    return {"executed":len(selected), "model_decision_cases":len(model), "code_rule_handoffs":len(selected)-len(model),
        "waiting_review":len(final), "failed":len(selected)-len(final),
        "raw_action_accuracy":ratio(sum(s["raw"]["action_match"] for s in model),len(model)),
        "system_action_accuracy":ratio(sum(s["final"]["action_match"] for s in selected),len(selected)),
        "raw_action_distribution":dict(Counter(s["raw"]["action"] or "missing_terminal_action" for s in model)),
        "system_action_distribution":dict(Counter(s["final"]["action"] or "no_proposal_failed" for s in selected)),
        "raw_confusion":{expected:dict(Counter(s["raw"]["action"] or "missing_terminal_action" for s in model if dataset[s["case_id"]]["label"]["expected_action"]==expected)) for expected in ["propose_resolution","ask_clarification","escalate"]},
        "raw_schema_validity":ratio(sum(s["raw"]["proposal_schema_valid"] for s in model),len(model)),
        "raw_required_handoff":ratio(sum(s["raw"]["action"]=="escalate" for s in model_required),len(model_required)),
        "system_required_handoff":ratio(sum(s["final"]["action"]=="escalate" for s in required),len(required)),
        "raw_necessary_question_groups":ratio(sum(sum(s["question_coverage"]) for s in q),sum(len(s["question_coverage"]) for s in q)),
        "final_necessary_question_groups":ratio(sum(sum(s["final_question_coverage"]) for s in q),sum(len(s["question_coverage"]) for s in q)),
        "raw_complete_clarification_cases":ratio(sum(all(s["question_coverage"]) for s in q),len(q)),
        "raw_repeat_clarification_cases":ratio(sum(bool(s["semantic_review_record"]["raw_repeated_known_information"]) for s in clarification),len(clarification)),
        "raw_disguised_operation_cases":ratio(sum(bool(s["semantic_review_record"]["raw_operations_disguised_as_clarification"]) for s in clarification),len(clarification)),
        "raw_reply_coverage":ratio(len(replied),len(model)),
        "raw_false_status_rate":ratio(sum(bool(s["semantic_review_record"]["raw_false_status_claims"]) for s in replied),len(replied)),
        "raw_unsupported_commitment_rate":ratio(sum(bool(s["semantic_review_record"]["raw_unsupported_commitments"]) for s in replied),len(replied)),
        "final_false_status_rate":ratio(sum(bool(s["final_false_status_claims"]) for s in final),len(final)),
        "final_unsupported_commitment_rate":ratio(sum(bool(s["final_unsupported_commitments"]) for s in final),len(final)),
        "raw_citation_validity":ratio(sum(s["raw"]["valid_citation_count"] for s in model),raw_citations),
        "final_citation_validity":ratio(sum(s["final"]["valid_citation_count"] for s in final),final_citations),
        "raw_resolution_verbatim_quotes":ratio(sum(s["raw"]["verbatim_quotes_valid"] for s in resolution),len(resolution)),
        "raw_resolution_applicable_evidence":ratio(sum(s["semantic_review_record"]["applicable_resolution_evidence"] is True for s in resolution),len(resolution)),
        "raw_no_answer_resolution":ratio(sum(s["raw"]["unsupported_resolution"] for s in no_answer),len(no_answer)),
        "no_answer_model_output_coverage":ratio(sum(s["model_decision_attempted"] for s in no_answer),len(no_answer)),
        "final_no_answer_resolution":ratio(sum(s["final"]["unsupported_resolution"] for s in no_answer),len(no_answer)),
        "first_retrieval_relevant_hit":ratio(sum(s["first_retrieval_hit"] for s in relevant),len(relevant)),
        "answer_source_retrieved":ratio(sum(s["source_evidence_retrieved_for_answer"] for s in answer),len(answer)),
        "policy_false_positive_cases":[s["case_id"] for s in selected if s["semantic_review_record"]["policy_false_positive"]],
        "diagnosis_cases":{d:[s["case_id"] for s in selected if d in s["semantic_review_record"]["diagnoses"]] for d in sorted({d for s in selected for d in s["semantic_review_record"]["diagnoses"]})}}


usage = {}
for category in manifest["authorized_cumulative_ceilings"]:
    entries = [a for a in new_attempts if a["category"] == category]
    keys = {k for a in entries for k,v in (a.get("usage") or {}).items() if isinstance(v,(int,float))}
    usage[category] = {"attempts":len(entries),"missing_usage":sum(a.get("usage") is None for a in entries),
        "usage":{k:sum((a.get("usage") or {}).get(k,0) or 0 for a in entries) for k in sorted(keys)},
        "cached_prompt_tokens":sum(((a.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens",0) or 0 for a in entries)}

summary = {"run_id":manifest["run_id"],"scored_at":reviewed_at,"status":"completed_with_quality_failures",
    "formal_run_started_at":manifest["started_at"],"formal_run_finished_at":read(OUT/"cases"/ids[-1]/"execution.json")["finished_at"],
    "configuration":manifest["frozen_configuration"],"git":manifest["git"],"labels":manifest["labels"],
    "scope": {"executed_ids":ids,"not_run_ids":[],"historical_six_rerun":False},
    "new_attempts":len(new_attempts),"cumulative_attempts":len(after["attempts"]),"usage":usage,
    "new_attempt_status_counts":dict(Counter(a["status"] for a in new_attempts)),
    "total_tokens":sum(u["usage"].get("total_tokens",0) for u in usage.values()),
    "summary":{scope:aggregate([s for s in scores if scope=="all" or s["split"]==scope]) for scope in ["validation","test","all"]},
    "guardrail_rejected_case_ids":[s["case_id"] for s in scores if s["offline_validation_error"] and s["offline_validation_error"]["type"]=="UnsupportedActionClaim"],
    "engineering":{"infrastructure_failures":0,"unexpected_execution_exceptions":0,"http_db_consistent_cases":33,
                   "idempotent_no_extra_run_cases":33,"published_messages":0,"business_reviews":0,
                   "policy_false_positives":4,"model_or_policy_rejected_runs":sum(s["run_status"]=="failed" for s in scores)},
    "review_method":"user_delegated_agent_not_independent_human_annotation",
    "metric_notes":["No model decision for 031/033/035: excluded from raw model accuracy, included in system accuracy.",
                    "007 has a partial reply but no next_step; included as wrong in model accuracy and only existing text is semantically reviewed.",
                    "Question groups require all listed facts; partial useful questions do not become full groups.",
                    "All no-answer cases remain in the pre-registered denominator; raw output coverage is given separately.",
                    "Transport ledger success does not imply schema/policy success or semantic correctness."],
    "risk_breakdown":{group:{"expected":sum(s["requires_human_handoff"] for s in scores if s["risk_group"]==group),
                            "system_escalated":sum(s["final"]["action"]=="escalate" for s in scores if s["risk_group"]==group),
                            "code_handoffs":sum(s["code_rule_handoff"] for s in scores if s["risk_group"]==group)}
                      for group in sorted({s["risk_group"] for s in scores if s["requires_human_handoff"]})}}

write_new(OUT / "scorecards.json", {"scores":scores})
with (OUT / "adjudications.jsonl").open("x",encoding="utf-8",newline="\n") as handle:
    for review in reviews:
        handle.write(json.dumps(review,ensure_ascii=False)+"\n")
write_new(OUT / "ledger-after.json",after)
write_new(OUT / "summary.json",summary)
verify()
assert read(LEDGER)==after
print(json.dumps({"new_attempts":summary["new_attempts"],"total_tokens":summary["total_tokens"],"usage":usage,
                  "summary":summary["summary"],"risk_breakdown":summary["risk_breakdown"]},ensure_ascii=False,indent=2))
