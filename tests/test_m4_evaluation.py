import copy
import json
from pathlib import Path

import pytest

from ticketmind.evaluation.dataset import digest, label_hash, load_dataset, retrieval_queries
from ticketmind.evaluation.scoring import apply_adjudication, score_prediction, summarize
from ticketmind.knowledge.sources import load_sources


@pytest.fixture
def data(tmp_path):
    case = {"case_id": "test-1", "synthetic": True, "group_id": "one", "split": "test", "scenario_type": "missing",
            "input": {"subject": "查询超时", "body": "缺少信息", "channel": "web", "requester_role": "developer"}}
    label = {"case_id": "test-1", "label_status": "pending_review", "expected_action": "ask_clarification",
             "relevant_source_ids": ["SYN-HIST-V2-006"], "distractor_source_ids": [], "answer_available": False,
             "human_review_required": True, "requires_human_handoff": False, "necessary_questions": ["接口范围"],
             "forbidden_conclusions": ["全年超时已解决"], "rationale": "缺少范围", "rule_ids": ["R-1"]}
    def save(cases=None, labels=None):
        for name, values in [("evaluation_cases", cases or [case]), ("evaluation_labels", labels or [label])]:
            (tmp_path / (name + ".jsonl")).write_text("\n".join(json.dumps(v) for v in values), encoding="utf-8")
    save()
    fixture_path = Path(__file__).resolve().parents[1] / "data/synthetic/v2/historical_cases.jsonl"
    return tmp_path, case, label, save, load_sources(fixture_path)


@pytest.mark.parametrize("mutation", ["null", "unknown", "overlap", "false_review", "missing_action"])
def test_invalid_or_unreviewed_labels_cannot_be_scored_as_reviewed(data, mutation):
    path, _, label, save, corpus = data
    if mutation == "null":
        label["answer_available"] = None
    elif mutation == "unknown":
        label["relevant_source_ids"] = ["invented"]
    elif mutation == "overlap":
        label["distractor_source_ids"] = label["relevant_source_ids"]
    elif mutation == "false_review":
        label["label_status"] = "reviewed"
    else:
        label["expected_action"] = None
    save()
    with pytest.raises(ValueError):
        load_dataset(path, corpus)


def test_review_hash_invalidates_on_input_or_label_change(data):
    path, case, label, save, corpus = data
    review = {"case_id": case["case_id"], "label_hash": label_hash(case, label, corpus_version=corpus.version), "reviewer": "human-test",
              "reviewed_at": "2026-09-15T00:00:00Z", "decision": "approve"}
    target = path / "reviews.jsonl"
    target.write_text(json.dumps(review), encoding="utf-8")
    assert load_dataset(path, corpus, target)[0]["label_status"] == "reviewed"
    case["input"]["body"] = "条件改变"
    save()
    with pytest.raises(ValueError, match="内容已变化"):
        load_dataset(path, corpus, target)


def test_group_cannot_cross_splits_and_labels_never_enter_retrieval_query(data):
    path, case, label, save, corpus = data
    rows = load_dataset(path, corpus)
    assert "全年超时已解决" not in retrieval_queries(rows)[0]["query"]
    second = copy.deepcopy(case)
    second.update(case_id="test-2", split="validation")
    second["input"]["body"] = "另一个条件"
    save([case, second], [label, {**label, "case_id": "test-2"}])
    with pytest.raises(ValueError, match="同组"):
        load_dataset(path, corpus)


def test_raw_failure_not_hidden_by_final_guard_and_empty_denominators(data):
    path, case, _, _, corpus = data
    row = load_dataset(path, corpus)[0]
    prediction = {"case_id": case["case_id"], "input_hash": digest(case["input"]), "status": "succeeded",
                  "raw_proposal": {"next_step": "propose_resolution", "evidence_ids": ["invented"]},
                  "final_proposal": {"next_step": "ask_clarification"}, "retrieval_evidence": []}
    scored = score_prediction(row, prediction, corpus)
    assert scored["raw"]["unsupported_resolution"] and not scored["raw"]["action_match"]
    assert scored["final"]["action_match"] and scored["raw"]["valid_citation_count"] == 0
    summary = summarize([scored])["test"]
    assert summary["reviewed"]["raw"]["action_match"] == {"numerator": 0, "denominator": 0, "value": None}
    assert summary["pending_review"]["raw"]["action_match"]["denominator"] == 1
    assert summary["pending_review"]["raw"]["necessary_question_coverage"]["value"] is None
    review = {"case_id": case["case_id"], "label_hash": scored["label_hash"], "prediction_hash": scored["prediction_hash"],
              "reviewer": "human-test", "reviewed_at": "2026-09-15", "rationale": "原始输出缺问题且无依据建议",
              "raw_question_coverage": [False], "raw_forbidden_conclusion_violations": ["全年超时已解决"]}
    adjudicated = apply_adjudication(scored, review)
    assert summarize([adjudicated])["test"]["pending_review"]["raw"]["necessary_question_coverage"]["value"] == 0
    with pytest.raises(ValueError):
        apply_adjudication(scored, {**review, "prediction_hash": "changed"})


def test_missing_prediction_is_not_a_success(data):
    path, case, _, _, corpus = data
    score = score_prediction(load_dataset(path, corpus)[0], {"case_id": case["case_id"],
        "input_hash": digest(case["input"]), "status": "failed"}, corpus)
    result = summarize([score])["test"]["pending_review"]
    assert result["failed"] == 1 and result["raw"]["action_match"]["value"] == 0
