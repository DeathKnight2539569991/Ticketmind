"""Evaluation labels are private to evaluators, with content-bound review records."""
import hashlib
import json
from pathlib import Path

from ticketmind.api.schemas.tickets import TicketCreate

ACTIONS = {"propose_resolution", "ask_clarification", "escalate"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def read_jsonl(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("评测文件为空或 case_id 重复")
    return rows


def label_hash(case, label, *, corpus_version):
    return digest({"case": case, "label": label, "corpus_version": corpus_version})


def load_dataset(directory, corpus, reviews_path=None):
    directory = Path(directory)
    cases = read_jsonl(directory / "evaluation_cases.jsonl")
    labels = {row["case_id"]: row for row in read_jsonl(directory / "evaluation_labels.jsonl")}
    if set(labels) != {row["case_id"] for row in cases}:
        raise ValueError("输入与标签必须一一对应")
    reviews = read_jsonl(reviews_path) if reviews_path else []
    reviews = {row["case_id"]: row for row in reviews}
    if set(reviews) - set(labels):
        raise ValueError("审核包含未知样本")
    groups, inputs, result = {}, set(), []
    for case in cases:
        label = labels[case["case_id"]]
        TicketCreate.model_validate(case["input"])
        if case.get("synthetic") is not True or case["split"] not in {"validation", "test"}:
            raise ValueError("新样本必须标记合成来源及 validation/test")
        group, split = case["group_id"], case["split"]
        if group in groups and groups[group] != split:
            raise ValueError("同组样本不得跨 split")
        groups[group] = split
        fingerprint = digest({k: case["input"][k].strip() for k in ("subject", "body")})
        if fingerprint in inputs:
            raise ValueError("重复输入")
        inputs.add(fingerprint)
        validate_label(label, corpus)
        review = reviews.get(case["case_id"])
        if review and (review.get("label_hash") != label_hash(case, label, corpus_version=corpus.version)
                       or not review.get("reviewer") or not review.get("reviewed_at")
                       or review.get("decision") != "approve"):
            raise ValueError("标签审核未批准、缺署名/时间或内容已变化")
        result.append({"case": case, "label": label, "label_hash": label_hash(case, label, corpus_version=corpus.version),
                       "label_status": "reviewed" if review else "pending_review",
                       "review_method": review.get("review_method", "unspecified") if review else None})
    return result


def validate_label(label, corpus):
    if label.get("label_status") != "pending_review":
        raise ValueError("草案保持 pending_review；使用独立内容绑定审核记录")
    if label.get("expected_action") not in ACTIONS:
        raise ValueError("动作标签缺失或不合法")
    for key in ("answer_available", "human_review_required", "requires_human_handoff"):
        if type(label.get(key)) is not bool:
            raise ValueError(f"{key} 必须明确为布尔值，不能将 null 当 false")
    if not label["human_review_required"]:
        raise ValueError("所有发布均需人工审核")
    if label["requires_human_handoff"] != (label["expected_action"] == "escalate"):
        raise ValueError("转人工标签与动作不一致")
    if label["expected_action"] == "propose_resolution" and not label["answer_available"]:
        raise ValueError("缺少答案不能标为解决建议")
    for key in ("relevant_source_ids", "distractor_source_ids", "necessary_questions", "forbidden_conclusions", "rule_ids"):
        value = label.get(key)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value) or len(set(value)) != len(value):
            raise ValueError(f"{key} 必须为无重复字符串列表")
    relevant, distractors = set(label["relevant_source_ids"]), set(label["distractor_source_ids"])
    if relevant & distractors or (relevant | distractors) - corpus.cases.keys():
        raise ValueError("相关/干扰来源重叠或未知来源")
    if label["answer_available"] and not relevant:
        raise ValueError("可作答标签需要来源")
    if not label.get("rationale") or not label["rule_ids"]:
        raise ValueError("标签需要判定依据与规则")


def retrieval_queries(rows):
    from ticketmind.agent.retrieve import build_retrieval_query
    return [{"case_id": row["case"]["case_id"], "query": build_retrieval_query(
        **{key: row["case"]["input"][key] for key in ("subject", "body")}),
        "group_id": row["case"]["group_id"], "split": row["case"]["split"],
        "label_status": row["label_status"], "label_hash": row["label_hash"],
        "review_method": row.get("review_method"),
        **{key: row["label"][key] for key in ("relevant_source_ids", "distractor_source_ids", "answer_available", "rationale")}}
        for row in rows]


def load_development_labels(overlay, original_cases, corpus, reviews_path=None):
    if {label["case_id"] for label in overlay} != set(original_cases):
        raise ValueError("开发overlay必须与原始33条输入一一对应")
    reviews = {r["case_id"]: r for r in read_jsonl(reviews_path)} if reviews_path else {}
    if set(reviews) - set(original_cases):
        raise ValueError("开发审核包含未知样本")
    result = {}
    for label in overlay:
        validate_label(label, corpus)
        case_id = label["case_id"]
        fingerprint = label_hash(original_cases[case_id], label, corpus_version=corpus.version)
        review = reviews.get(case_id)
        if review and (review.get("label_hash") != fingerprint or review.get("decision") != "approve"
                       or not review.get("reviewer") or not review.get("reviewed_at")):
            raise ValueError("开发标签审核与内容不一致或未批准")
        result[case_id] = {**label, "label_hash": fingerprint,
                          "label_status": "reviewed" if review else "pending_review",
                          "review_method": review.get("review_method", "unspecified") if review else None}
    return result
