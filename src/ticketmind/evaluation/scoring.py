"""Deterministic counts plus explicitly pending semantic adjudication."""
from ticketmind.evaluation.dataset import digest
from ticketmind.agent.proposals import proposal_adapter


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def score_prediction(row, prediction, corpus):
    if prediction["case_id"] != row["case"]["case_id"] or prediction["input_hash"] != digest(row["case"]["input"]):
        raise ValueError("预测与输入不匹配")
    if prediction.get("status") not in {"succeeded", "failed"}:
        raise ValueError("只能为实际已执行的成功或失败预测评分")
    label = row["label"]
    raw, final = prediction.get("raw_proposal"), prediction.get("final_proposal")
    def assess(proposal):
        try:
            proposal_adapter.validate_python(proposal)
            protocol_valid = True
        except ValueError:
            protocol_valid = False
        action = proposal.get("next_step") if isinstance(proposal, dict) else None
        ids = proposal.get("evidence_ids", []) if isinstance(proposal, dict) else []
        if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
            ids = []
        actual = {hit["source_id"]: hit["text"] for hit in prediction.get("retrieval_evidence", [])}
        return {"action": action, "action_match": action == label["expected_action"], "proposal_schema_valid": protocol_valid,
            "citation_count": len(ids), "valid_citation_count": sum(source in actual and source in corpus.cases for source in ids),
            "irrelevant_citation_count": sum(source not in label["relevant_source_ids"] for source in ids),
            "unsupported_resolution": action == "propose_resolution" and not label["answer_available"],
            "missed_handoff": label["requires_human_handoff"] and action != "escalate"}
    return {"case_id": prediction["case_id"], "split": row["case"]["split"], "label_status": row["label_status"],
            "label_hash": row["label_hash"], "prediction_hash": digest(prediction), "status": prediction["status"],
            "raw": assess(raw), "final": assess(final),
            "answer_available": label["answer_available"], "requires_human_handoff": label["requires_human_handoff"],
            "review_method": row.get("review_method"),
            "necessary_questions": label["necessary_questions"], "forbidden_conclusions": label["forbidden_conclusions"],
            "semantic_review": "pending_review", "question_coverage": None, "forbidden_conclusion_violations": None,
            "business_review": "not_executed"}


def apply_adjudication(score, review):
    """Manual semantic judgment binds both the label and the unedited prediction."""
    if (review.get("case_id") != score["case_id"] or review.get("label_hash") != score["label_hash"]
            or review.get("prediction_hash") != score["prediction_hash"]
            or not review.get("reviewer") or not review.get("reviewed_at")):
        raise ValueError("语义审核缺少署名/时间或与标签/原始预测不一致")
    coverage = review.get("raw_question_coverage")
    violations = review.get("raw_forbidden_conclusion_violations")
    if (not isinstance(coverage, list) or len(coverage) != len(score["necessary_questions"])
            or any(type(value) is not bool for value in coverage)):
        raise ValueError("每条必要问题必须明确标注原始模型是否覆盖")
    if (not isinstance(violations, list) or any(v not in score["forbidden_conclusions"] for v in violations)
            or len(set(violations)) != len(violations)):
        raise ValueError("违规结论必须来自当前清单，且不能重复")
    if not review.get("rationale"):
        raise ValueError("语义判定必须给出依据")
    global_violations = review.get("raw_global_rule_violations", [])
    if (not isinstance(global_violations, list) or any(v not in {f"G{i:02d}" for i in range(1, 9)} for v in global_violations)
            or len(set(global_violations)) != len(global_violations)):
        raise ValueError("全局违规须使用业务规则 G01—G08，无重复")
    return {**score, "semantic_review": "reviewed", "question_coverage": coverage,
            "forbidden_conclusion_violations": violations, "global_rule_violations": global_violations,
            "semantic_review_record": review}


def summarize(scores):
    result = {}
    for split in ("validation", "test"):
        result[split] = {}
        for status in ("reviewed", "pending_review"):
            selected = [r for r in scores if r["split"] == split and r["label_status"] == status]
            result[split][status] = {"executed": len(selected), "failed": sum(r["status"] == "failed" for r in selected)}
            for layer in ("raw", "final"):
                values = [r[layer] for r in selected]
                result[split][status][layer] = {
                    "action_match": ratio(sum(v["action_match"] for v in values), len(values)),
                    "proposal_schema_validity": ratio(sum(v["proposal_schema_valid"] for v in values), len(values)),
                    "citation_validity": ratio(sum(v["valid_citation_count"] for v in values), sum(v["citation_count"] for v in values)),
                    "irrelevant_citations": ratio(sum(v["irrelevant_citation_count"] for v in values), sum(v["citation_count"] for v in values)),
                    "unsupported_resolution_count": sum(v["unsupported_resolution"] for v in values),
                    "missed_handoff_count": sum(v["missed_handoff"] for v in values),
                    "unsupported_resolution_rate": ratio(sum(v["unsupported_resolution"] for v in values),
                                                         sum(not r["answer_available"] for r in selected)),
                    "missed_handoff_rate": ratio(sum(v["missed_handoff"] for v in values),
                                                sum(r["requires_human_handoff"] for r in selected)),
                    "necessary_question_coverage": ratio(0, 0), "forbidden_conclusion_violations": None}
            adjudicated = [r for r in selected if r["semantic_review"] == "reviewed"]
            result[split][status]["raw"]["necessary_question_coverage"] = ratio(
                sum(sum(r["question_coverage"]) for r in adjudicated),
                sum(len(r["question_coverage"]) for r in adjudicated))
            result[split][status]["raw"]["forbidden_conclusion_cases"] = ratio(
                sum(bool(r["forbidden_conclusion_violations"]) for r in adjudicated), len(adjudicated))
            result[split][status]["raw"]["global_rule_violation_cases"] = ratio(
                sum(bool(r["global_rule_violations"]) for r in adjudicated), len(adjudicated))
    return result
