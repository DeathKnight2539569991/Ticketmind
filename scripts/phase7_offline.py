"""Offline-only validation and freezing for the Phase 7 synthetic candidate set."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))

from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.schemas import AgentMessage
from ticketmind.api.schemas.tickets import TicketCreate
from ticketmind.documents.markdown import parse_markdown
from ticketmind.evaluation.dataset import label_hash
from ticketmind.knowledge.corpus import HistoricalCase
from ticketmind.knowledge.sources import load_sources

DATA = ROOT / "data/synthetic/phase7_v1"
OLD = ROOT / "data/synthetic/v2"
RULES = ROOT / "docs/phase7-data-contract.md"
DOCS_VERSION = "synthetic-phase7-docs-v1"
ACTION_IDS = {"propose_resolution", "ask_clarification", "escalate"}
LABEL_FIELDS = {
    "expected_action", "relevant_source_ids", "distractor_source_ids", "answer_available",
    "human_review_required", "requires_human_handoff", "necessary_questions",
    "forbidden_conclusions", "rationale", "rule_ids", "scenario_type", "label_status",
    "relevant_doc_chunk_ids", "distractor_doc_chunk_ids", "allowed_followup_tool_paths",
}


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonl(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows:
        raise ValueError(f"{path.name}: empty JSONL")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path.name}: every row must be an object")
    return rows


def _ids(rows, field):
    values = [row.get(field) for row in rows]
    if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
        raise ValueError(f"invalid or duplicate {field}")
    return set(values)


def _check_allowed_keys(row, required, allowed, label):
    if not required <= row.keys() or row.keys() - allowed:
        raise ValueError(f"{label}: missing or unexpected fields")


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field}: expected non-empty string list")
    if len(set(value)) != len(value):
        raise ValueError(f"{field}: duplicate value")


def _ticket_input(case):
    TicketCreate.model_validate(case["input"])
    if set(case["input"]) != {"subject", "body", "channel", "requester_role"}:
        raise ValueError("TicketCreate input must contain only the four schema fields")
    body = json.dumps(case["input"], ensure_ascii=False).lower()
    if any(name.lower() in body for name in LABEL_FIELDS):
        raise ValueError(f"label leakage in ticket input {case['case_id']}")


def validate_data() -> tuple[dict, list[dict], list[dict], list[dict], dict]:
    cases = jsonl(DATA / "evaluation_cases.jsonl")
    labels = jsonl(DATA / "evaluation_labels.jsonl")
    histories = jsonl(DATA / "historical_cases.jsonl")
    case_annotations = jsonl(DATA / "case_annotations.jsonl")
    eval_annotations = jsonl(DATA / "evaluation_annotations.jsonl")
    if len(cases) != 48 or len(labels) != 48 or len(histories) != 120:
        raise ValueError("Phase 7 requires exactly 48 tickets, 48 labels, and 120 historical cases")
    case_ids = _ids(cases, "case_id")
    hist_ids = _ids(histories, "source_id")
    label_ids = _ids(labels, "case_id")
    if case_ids != label_ids or case_ids != {f"SYN-P7-TEST-{n:03}" for n in range(1, 49)}:
        raise ValueError("ticket IDs and label IDs must be the fixed 48 IDs")
    if hist_ids != {f"SYN-P7-CASE-{n:03}" for n in range(1, 121)}:
        raise ValueError("historical IDs must be the fixed 120 IDs")
    ann_ids = _ids(case_annotations, "source_id")
    eval_ann_ids = _ids(eval_annotations, "case_id")
    if ann_ids != hist_ids or eval_ann_ids != case_ids:
        raise ValueError("annotations must correspond one-to-one to source rows")

    for row in cases:
        _check_allowed_keys(row, {"case_id", "synthetic", "group_id", "split", "input", "scenario_type"},
                            {"case_id", "synthetic", "group_id", "split", "input", "scenario_type"}, "ticket")
        if row["synthetic"] is not True or row["split"] != "test":
            raise ValueError("all tickets must be synthetic test-only")
        _ticket_input(row)
    for row in histories:
        HistoricalCase.model_validate(row)
        if row.get("synthetic") is not True:
            raise ValueError("historical cases must be synthetic")
    for row in case_annotations:
        _check_allowed_keys(row, {"source_id", "synthetic", "topic", "section", "coverage_kind",
                                  "applicable_conditions", "boundary", "cross_topic_near_match", "review_status"},
                            {"source_id", "synthetic", "topic", "section", "coverage_kind",
                             "applicable_conditions", "boundary", "cross_topic_near_match", "review_status"},
                            "case annotation")
        if (row.get("review_status") != "pending_review" or row.get("synthetic") is not True
                or row.get("topic") not in {"import_schema", "import_csv", "import_commit", "export_scope",
                                             "export_jobs", "query_time", "query_paging", "access_safety"}
                or row.get("section") not in {1, 2, 3}
                or row.get("coverage_kind") not in {"success", "missing_facts_resolved", "counterexample",
                                                     "risk_handoff", "adjacent_function"}
                or type(row.get("cross_topic_near_match")) is not bool
                or not row.get("applicable_conditions") or not row.get("boundary")):
            raise ValueError("source annotations must remain pending_review")
    for row in eval_annotations:
        _check_allowed_keys(row, {"case_id", "synthetic", "bootstrap", "evidence_profile", "paired_with",
                                  "profile_definition", "query_rewrite_candidate", "query_rewrite_hint", "review_status"},
                            {"case_id", "synthetic", "bootstrap", "evidence_profile", "paired_with",
                             "profile_definition", "query_rewrite_candidate", "query_rewrite_hint", "review_status"},
                            "evaluation annotation")
        if (row.get("review_status") != "pending_review" or row.get("synthetic") is not True
                or row.get("bootstrap") != "fixed_case_search_always"
                or row.get("evidence_profile") not in {"both", "no_extra_tools", "doc_only", "case_only"}
                or type(row.get("query_rewrite_candidate")) is not bool
                or not row.get("profile_definition")
                or (row.get("query_rewrite_candidate") and not row.get("query_rewrite_hint"))
                or (row.get("paired_with") is not None and row.get("paired_with") not in case_ids)):
            raise ValueError("evaluation annotations must remain pending_review")

    docs_meta = {}
    doc_chunk_ids = set()
    docs = sorted((DATA / "docs").glob("*.md"))
    if len(docs) != 8:
        raise ValueError("expected exactly 8 product Markdown docs")
    for path in docs:
        parsed = parse_markdown(path.stem, path.read_text(encoding="utf-8"))
        if not re.search(r"(?im)^synthetic\s*=\s*true(?:$|[。；;，\s])", path.read_text(encoding="utf-8")):
            raise ValueError(f"{path.name}: document must explicitly state synthetic=true")
        if len(parsed.chunks) != 3:
            raise ValueError(f"{path.name}: expected 3 chunks, got {len(parsed.chunks)}")
        ids = [chunk.chunk_id for chunk in parsed.chunks]
        expected = [f"{path.stem}:{section:03}:001" for section in range(1, 4)]
        if ids != expected:
            raise ValueError(f"{path.name}: unstable chunk IDs")
        docs_meta[path.stem] = {"content_hash": parsed.content_hash,
                                "chunks": [{"chunk_id": c.chunk_id, "content_hash": c.content_hash,
                                            "text": c.text} for c in parsed.chunks]}
        doc_chunk_ids.update(ids)

    corpus = load_sources(DATA / "historical_cases.jsonl")
    old_corpus = load_sources(OLD / "historical_cases.jsonl")
    case_map = {row["case_id"]: row for row in cases}
    labels_map = {row["case_id"]: row for row in labels}
    action_counts = {action: 0 for action in ACTION_IDS}
    for case_id, label in labels_map.items():
        case = case_map[case_id]
        _check_allowed_keys(label,
            {"case_id", "label_status", "expected_action", "relevant_source_ids", "distractor_source_ids",
             "answer_available", "human_review_required", "requires_human_handoff", "necessary_questions",
             "forbidden_conclusions", "rationale", "rule_ids", "relevant_doc_chunk_ids",
             "distractor_doc_chunk_ids", "allowed_followup_tool_paths"},
            {"case_id", "label_status", "expected_action", "relevant_source_ids", "distractor_source_ids",
             "answer_available", "human_review_required", "requires_human_handoff", "necessary_questions",
             "forbidden_conclusions", "rationale", "rule_ids", "relevant_doc_chunk_ids",
             "distractor_doc_chunk_ids", "allowed_followup_tool_paths"}, "label")
        if label["label_status"] != "pending_review":
            raise ValueError("source labels remain pending_review; only review records approve")
        action = label["expected_action"]
        if action not in ACTION_IDS:
            raise ValueError("unknown expected action")
        action_counts[action] += 1
        for name in ("relevant_source_ids", "distractor_source_ids", "necessary_questions", "forbidden_conclusions", "rule_ids",
                     "relevant_doc_chunk_ids", "distractor_doc_chunk_ids"):
            _strings(label[name], name)
        if type(label["answer_available"]) is not bool or type(label["human_review_required"]) is not bool or type(label["requires_human_handoff"]) is not bool:
            raise ValueError("label booleans must be explicit")
        if not label["human_review_required"] or label["requires_human_handoff"] != (action == "escalate"):
            raise ValueError("human review / handoff action mismatch")
        if action == "propose_resolution" and label["answer_available"] is not True:
            raise ValueError("resolution labels require an answer source")
        if action != "propose_resolution" and label["answer_available"] is not False:
            raise ValueError("clarification and escalation labels must not claim an available answer")
        if action == "ask_clarification" and not label["necessary_questions"]:
            raise ValueError("clarification labels require at least one necessary question")
        if len(label["necessary_questions"]) > 5 or (action != "ask_clarification" and label["necessary_questions"]):
            raise ValueError("necessary_questions must be empty except for Q labels, max five")
        if (not label["rationale"] or not label["rule_ids"]
                or not all(re.fullmatch(r"(?:G0[1-8]|P0[1-8])", r) for r in label["rule_ids"])):
            raise ValueError("rationale and valid G/P rule IDs required")
        case_refs, case_distractors = set(label["relevant_source_ids"]), set(label["distractor_source_ids"])
        doc_refs, doc_distractors = set(label["relevant_doc_chunk_ids"]), set(label["distractor_doc_chunk_ids"])
        if case_refs & case_distractors or (case_refs | case_distractors) - hist_ids:
            raise ValueError("Case references must exist and relevant/distractor be exclusive")
        if doc_refs & doc_distractors or (doc_refs | doc_distractors) - doc_chunk_ids:
            raise ValueError("Docs references must exist and relevant/distractor be exclusive")
        if label["answer_available"] and not (case_refs or doc_refs):
            raise ValueError("answer_available requires a Case or Docs reference")
        paths = label["allowed_followup_tool_paths"]
        if (not isinstance(paths, list) or len({tuple(path) for path in paths if isinstance(path, list)}) != len(paths)
                or any(not isinstance(path, list) or path.count("search_cases") > 1
                       or path.count("search_docs") > 2
                       or any(tool not in {"search_cases", "search_docs"} for tool in path)
                       for path in paths)):
            raise ValueError("allowed_followup_tool_paths schema invalid")

    if action_counts != {"propose_resolution": 16, "ask_clarification": 16, "escalate": 16}:
        raise ValueError(f"action counts incorrect: {action_counts}")
    if any(not label_map["input"]["subject"].strip() for label_map in cases):
        raise ValueError("empty subject")

    files = {}
    for path in [*docs, DATA / "historical_cases.jsonl", DATA / "evaluation_cases.jsonl",
                 DATA / "evaluation_labels.jsonl", DATA / "case_annotations.jsonl",
                 DATA / "evaluation_annotations.jsonl", RULES, DATA / "build_dataset.py"]:
        rel = path.relative_to(ROOT).as_posix()
        files[rel] = sha(path.read_bytes())
    manifest = {"schema_version": "phase7-v1", "rule_version": "phase7-v1",
                "case_corpus_version": corpus.version, "legacy_case_corpus_version": old_corpus.version,
                "docs_version": DOCS_VERSION, "rules_sha256": sha(RULES.read_bytes()),
                "files": dict(sorted(files.items())), "documents": docs_meta,
                "counts": {"documents": 8, "document_chunks": 24, "historical_cases": 120,
                           "tickets": 48, "labels": 48, "case_annotations": 120,
                           "evaluation_annotations": 48, "actions": action_counts},
                "test_only": True, "labels_pending_review": True}
    snapshot_hash = sha(canonical(manifest))
    qrows = []
    for case in cases:
        label = labels_map[case["case_id"]]
        q = build_retrieval_query(subject=case["input"]["subject"], messages=[
            AgentMessage(role="customer", content=case["input"]["body"])])
        qrows.append({"case_id": case["case_id"], "query": q, "group_id": case["group_id"],
                      "split": "test", "test_only": True, "label_status": "pending_review",
                      "label_hash": label_hash(case, label, corpus_version=corpus.version),
                      "relevant_source_ids": label["relevant_source_ids"],
                      "distractor_source_ids": label["distractor_source_ids"],
                      "answer_available": label["answer_available"], "rationale": label["rationale"]})
    duplicates = duplicate_audit(cases, histories)
    report = {"report_type": "offline_static_validation", "test_only": True,
              "snapshot_hash": snapshot_hash, "case_corpus_version": corpus.version,
              "docs_version": DOCS_VERSION, "counts": manifest["counts"],
              "review_status": "candidate_unreviewed", "duplicate_audit": duplicates,
              "limitations": ["未验证检索排名、模型行为或系统效果；近似重复项仅供人工审阅。",
                              "48条公开合成test数据不构成独立盲测。"]}
    return manifest, qrows, report, labels, {"corpus": corpus, "cases": cases}


def _norm(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold(), flags=re.UNICODE)


def _ngrams(text, n=3):
    value = _norm(text)
    return {value[i:i+n] for i in range(max(0, len(value)-n+1))} if len(value) >= n else ({value} if value else set())


def duplicate_audit(cases, histories):
    records = []
    legacy_dirs = ("v2", "m4", "live_e2e_v1", "live_e2e_v2", "m3")
    for directory in legacy_dirs:
        legacy = ROOT / "data/synthetic" / directory
        filenames = ["historical_cases.jsonl", "evaluation_cases.jsonl", "test_tickets.jsonl"]
        if directory == "m3":
            filenames += ["cached_queries.jsonl", "retrieval_diagnostics.jsonl"]
        for filename in filenames:
            path = legacy / filename
            if not path.exists():
                continue
            for row in jsonl(path):
                req = row.get("request", row.get("input", {}))
                rid = row.get("source_id", row.get("case_id", "unknown"))
                body = req.get("body", req.get("content", ""))
                if directory == "m3" and not req.get("subject"):
                    query = row.get("query", "")
                    if query:
                        records.append((f"legacy/{directory}/{filename}/{rid}", query))
                elif req.get("subject") or body:
                    records.append((f"legacy/{directory}/{filename}/{rid}",
                                    f"{req.get('subject', '')} {body}"))
    records += [(f"phase7/historical/{row['source_id']}",
                 f"{row['request']['subject']} {row['request']['body']}") for row in histories]
    records += [(f"phase7/ticket/{row['case_id']}", f"{row['input']['subject']} {row['input']['body']}") for row in cases]
    exact, near = [], []
    seen = {}
    for rid, text in records:
        norm = _norm(text)
        if norm in seen:
            exact.append({"id": rid, "matches": list(seen[norm]), "kind": "normalized_exact"})
        seen.setdefault(norm, []).append(rid)
    grams = {rid: _ngrams(text) for rid, text in records}
    # Report only unusually close text matches; shared topic vocabulary alone is not a duplicate verdict.
    for i, (left_id, left_text) in enumerate(records):
        if not left_id.startswith("phase7/ticket/"):
            continue
        left = grams[left_id]
        candidates = []
        for right_id, _ in records:
            if right_id == left_id:
                continue
            right = grams[right_id]
            score = len(left & right) / len(left | right) if left | right else 0.0
            if score >= 0.62 and len(left & right) >= 15:
                candidates.append((score, right_id))
        for score, right_id in sorted(candidates, reverse=True)[:5]:
            near.append({"left": left_id, "right": right_id, "char_ngram_jaccard": round(score, 4),
                         "status": "needs_human_review"})
    p7_exact = [item for item in exact if item["id"].startswith("phase7/")]
    if p7_exact:
        raise ValueError(f"Phase 7 contains normalized exact duplicate input(s): {p7_exact[:3]}")
    return {"normalized_exact_duplicates": exact, "approximate_text_pairs_for_review": near,
            "method": "NFKC + casefold and remove punctuation/whitespace; character 3-gram Jaccard >= 0.62 and >=15 shared grams. Near matches require human review and do not automatically indicate duplication."}


def snapshot_hash(manifest):
    return sha(canonical(manifest))


def _read_reviews(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def verify_approval(manifest, labels, cases, corpus, reviews_path, require_reviewed=False):
    reviews = _read_reviews(reviews_path)
    if not reviews:
        if require_reviewed:
            raise ValueError("independent approval records are required")
        return "candidate_unreviewed"
    review_ids = _ids(reviews, "case_id")
    if review_ids != {case["case_id"] for case in cases}:
        raise ValueError("review records must cover every case exactly once")
    by_case = {row["case_id"]: row for row in cases}
    label_by_id = {row["case_id"]: row for row in labels}
    snapshot = snapshot_hash(manifest)
    approved = True
    for review in reviews:
        if review.get("review_method") != "independent_agent" or not review.get("reviewer"):
            raise ValueError("reviewer must be identified as independent_agent")
        try:
            when = datetime.fromisoformat(review.get("reviewed_at", "").replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError()
        except (AttributeError, ValueError):
            raise ValueError("reviewed_at must be an ISO timestamp with timezone") from None
        expected = label_hash(by_case[review["case_id"]], label_by_id[review["case_id"]], corpus_version=corpus.version)
        if review.get("label_hash") != expected or review.get("snapshot_hash") != snapshot:
            raise ValueError("approval hash is stale or does not bind current snapshot")
        if review.get("decision") != "approve":
            approved = False
    if require_reviewed and not approved:
        raise ValueError("all independent reviews must approve")
    return "reviewed" if approved else "review_rejected_or_pending"


def check_frozen(require_reviewed=False):
    manifest, queries, report, labels, aux = validate_data()
    manifest_path = DATA / "manifest.json"
    query_path = DATA / "case_retrieval_queries.jsonl"
    report_path = DATA / "offline_validation_report.json"
    if not manifest_path.exists() or not query_path.exists() or not report_path.exists():
        raise ValueError("frozen artifacts missing; use --freeze to create candidate outputs")
    current_hash = snapshot_hash(manifest)
    saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if saved_manifest != manifest:
        raise ValueError("manifest is stale; use explicit --freeze after reviewing source changes")
    saved_queries = [json.loads(line) for line in query_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if saved_queries != queries:
        raise ValueError("query artifact is stale; use explicit --freeze")
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))
    if saved_report.get("snapshot_hash") != current_hash or saved_report != report:
        raise ValueError("offline report is stale; use explicit --freeze")
    review_status = verify_approval(manifest, labels, aux["cases"], aux["corpus"], DATA / "label_reviews.jsonl", require_reviewed)
    return {"status": "ok", "snapshot_hash": current_hash, "review_status": review_status,
            "counts": report["counts"]}


def freeze():
    manifest, queries, report, labels, aux = validate_data()
    review_path = DATA / "label_reviews.jsonl"
    if review_path.exists() and review_path.read_text(encoding="utf-8-sig").strip():
        # Never replace frozen artifacts underneath an approval record that no longer binds them.
        current = json.loads((DATA / "manifest.json").read_text(encoding="utf-8")) if (DATA / "manifest.json").exists() else None
        if current != manifest:
            raise ValueError("approval records exist; current sources differ from the approved frozen snapshot")
        verify_approval(manifest, labels, aux["cases"], aux["corpus"], review_path)
    # Deliberately do not touch label_reviews.jsonl: old approvals must remain auditable and stale.
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (DATA / "case_retrieval_queries.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in queries), encoding="utf-8")
    (DATA / "offline_validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return check_frozen()


def export_reviewed_queries(path: Path):
    output = Path(path).resolve()
    protected = {candidate.resolve() for candidate in DATA.rglob("*") if candidate.is_file()}
    protected.add(RULES.resolve())
    if output in protected:
        raise ValueError("reviewed query export target collides with a source, approval, or frozen artifact")
    check = check_frozen(require_reviewed=True)
    queries = [json.loads(line) for line in (DATA / "case_retrieval_queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    reviews = {row["case_id"]: row for row in _read_reviews(DATA / "label_reviews.jsonl")}
    for row in queries:
        row["label_status"] = "reviewed"
        row["review_method"] = reviews[row["case_id"]]["review_method"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in queries), encoding="utf-8")
    return {"status": "exported_reviewed_queries", "path": str(output), "count": len(queries),
            "snapshot_hash": check["snapshot_hash"], "label_status": "reviewed"}
