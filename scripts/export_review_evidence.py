"""Export allowlisted synthetic evaluation evidence, without credentials or new calls.

Run from app: python scripts/export_review_evidence.py --evidence-root ../log
The external log/app-docs directory contains the resulting snapshot.
Regeneration requires the original local run archives.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(evidence_root):
    history = evidence_root / "eval_runs/ticketmind_qwen_bm25_20260919"
    loop = evidence_root / "eval_runs/loop_paths_20260926"
    review_path = ROOT.parent / "log/app-docs/m4-reviewed-retrieval-results.json"
    retrieval = read(review_path)
    records = []
    provenance = {}
    for path in sorted((history / "results").glob("*/*.json")):
        value = read(path)
        run = value.get("run") or {}
        records.append({"case_id": value["case_id"], "decision_model": value["decision_model"],
            "input": value["input"], "run_status": run.get("run_status"),
            "proposal": value.get("final_proposal"), "error_code": value.get("error_code"),
            "tools": [{k: t[k] for k in ("tool", "status", "result_source_ids") if k in t}
                      for t in value.get("tool_calls", [])],
            "judge_statuses": [j["status"] for j in (run.get("usage") or {}).get("semantic_judge", [])],
            "retrieval_mode": value.get("actual_retrieval_mode"),
            "source_ids": value.get("returned_source_ids"), "original_sha256": sha(path)})
    assert len(records) == 80 and len({r["case_id"] for r in records}) == 40
    for path in (history / "manifest.json", history / "adjudication_summary.json",
                 history / "blind_adjudications.jsonl", loop / "summary.json", loop / "cleanup-check.json"):
        provenance[path.relative_to(evidence_root).as_posix()] = sha(path)
    adjudications = [json.loads(line) for line in (history / "blind_adjudications.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    manifest = read(history / "manifest.json")
    # Only explicitly public experiment metadata; never serialize environment/settings.
    historical_config = {k: manifest[k] for k in ("run_id", "cases_sha256", "case_count", "models", "protocols")}
    config = manifest["config"]
    historical_config["retrieval"] = {k: config[k] for k in ("retrieval_mode", "retrieval_top_k", "retrieval_candidate_k", "retrieval_rrf_k")}
    loop_summary = read(loop / "summary.json")
    for row in loop_summary["rows"]:
        if row.get("recovery"):
            row["recovery"].pop("original_path", None)
    data = {"export_date": "2026-09-28", "new_model_calls": 0,
        "notice": "Historical synthetic experiments, different versions; not current-model accuracy or independent human grading.",
        "retrieval_20260915": {"source": "../m4-reviewed-retrieval-results.json", "source_sha256": sha(review_path),
            "corpus_documents": 12, "top_k": 5,
            "summary": {split: {mode: {"executed": item["executed"], "failed": item["failed"], "reviewed": item["reviewed"]}
                                    for mode, item in modes.items()} for split, modes in retrieval["summary"].items()}},
        "comparison_20260919": {"configuration": historical_config,
            "summary": read(history / "adjudication_summary.json"), "records": records,
            "adjudications": adjudications},
        "paths_20260926": {"summary": loop_summary, "cleanup_all_clean": read(loop / "cleanup-check.json")["all_clean"]},
        "original_file_sha256": provenance}
    destination = ROOT.parent / "log/app-docs/evaluation/evidence-summary.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(destination), "comparison_runs": len(records), "new_model_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    export(parser.parse_args().evidence_root.resolve())
