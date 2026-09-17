"""Run one authorized case using the unchanged M4 CLI; never retry a case.

Run from the app root. This supervisor only checks freezes and archives outputs;
it does not alter Agent behavior, requests, labels, scoring or the shared ledger.
"""
import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PRE = ROOT / "docs/m4-remaining-2026-09-17"
LEDGER = ROOT / "data/cache/m4/attempts.json"
CEILINGS = {"initial_embedding": 51, "understanding": 39, "research_embedding": 31, "decision": 97}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def verify():
    from ticketmind.core.config import ProcessingSettings, QwenSettings, MilvusSettings
    from ticketmind.agent.decide import DECISION_PROTOCOL, DECISION_OPTIONS, SYSTEM_PROMPT
    from ticketmind.agent.understand import SYSTEM_PROMPT as UNDERSTANDING_PROMPT
    from ticketmind.llm.client import GENERATION_OPTIONS
    freeze = read(PRE / "freeze.json")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == freeze["git"]["commit_sha"]
    for group in ("source_files_sha256", "protected_files_sha256"):
        for path, expected in freeze[group].items():
            if path != "data/cache/m4/attempts.json":
                assert sha(ROOT / path) == expected, f"frozen file changed: {path}"
    config = ProcessingSettings(retrieval_mode="hybrid", retrieval_top_k=5, retrieval_candidate_k=20, retrieval_rrf_k=60)
    qwen = QwenSettings()
    expected = freeze["configuration"]
    assert config.model_dump(mode="json") == expected["processing"]
    assert qwen.model == expected["understanding_model"] and qwen.embedding_model == expected["embedding_model"]
    assert DECISION_PROTOCOL == expected["protocol"] and DECISION_OPTIONS == expected["decision_generation_options"]
    assert GENERATION_OPTIONS == expected["understanding_generation_options"]
    assert hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest() == expected["decision_system_prompt_sha256"]
    assert hashlib.sha256(UNDERSTANDING_PROMPT.encode()).hexdigest() == expected["understanding_system_prompt_sha256"]
    assert MilvusSettings().timeout_seconds == expected["milvus_timeout_seconds"]
    return freeze, config, qwen


def initialize():
    freeze, config, qwen = verify()
    assert sha(LEDGER) == freeze["budget"]["ledger_sha256"]
    assert not (ROOT / "data/cache/m4/session.lock").exists()
    # Read-only service and exact-cache checks before any paid request.
    from sqlalchemy import create_engine, text
    from ticketmind.core.config import Settings, MilvusSettings
    from ticketmind.knowledge.sources import load_sources
    from ticketmind.retrieval.milvus_client import build_milvus_client
    from ticketmind.retrieval.versioned_collection import validate_collection
    from ticketmind.agent.run_cache import QueryVectorCache, load_cache, query_fingerprint
    for query in read(PRE / "preflight.json")["queries"]:
        fp = query_fingerprint(settings=qwen, query=query["query"])
        assert load_cache(ROOT / "data/cache/m4/query" / f"{fp}.json", QueryVectorCache, expected_fingerprint=fp) is not None
    engine = create_engine(Settings().database_url.unicode_string(), connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            assert connection.execute(text("select 1")).scalar_one() == 1
    finally:
        engine.dispose()
    client = build_milvus_client(MilvusSettings())
    try:
        collection = validate_collection(client, load_sources(config.corpus_path), timeout=10, require_data=True)
        server = client.get_server_version(timeout=10)
    finally:
        client.close()
    write_new(OUT / "ledger-before.json", read(LEDGER))
    write_new(OUT / "manifest.json", {"run_id": OUT.name, "started_at": datetime.now(UTC).isoformat(),
        "authorization": "User explicitly replied 授权 after the remaining-33 plan and token estimate; no historical-six reruns.",
        "authorized_cumulative_ceilings": CEILINGS, "authorized_max_new_attempts": 153,
        "ledger_path": "data/cache/m4/attempts.json", "ledger_start_count": 65,
        "preflight_freeze": "../m4-remaining-2026-09-17/freeze.json", "preflight_freeze_sha256": sha(PRE / "freeze.json"),
        "supervisor_sha256": sha(Path(__file__)), "frozen_configuration": freeze["configuration"],
        "git": freeze["git"], "labels": freeze["labels"], "source_set_sha256": freeze["source_set_sha256"],
        "case_ids": freeze["scope"]["remaining_ids"], "service_preflight": {"postgres": "read_only_select_1", "milvus_collection": collection, "milvus_server_version": server},
        "business_review": "not_executed"})
    print("INITIALIZED: frozen files/config match; original ledger 65; PG/Milvus and 33 exact vectors ready", flush=True)


def execute_next():
    freeze, _, _ = verify()
    manifest = read(OUT / "manifest.json")
    assert sha(Path(__file__)) == manifest["supervisor_sha256"]
    assert sha(PRE / "freeze.json") == manifest["preflight_freeze_sha256"]
    ids = manifest["case_ids"]
    completed = sorted((OUT / "cases").glob("*/execution.json"))
    assert [read(p)["case_id"] for p in completed] == ids[:len(completed)]
    if len(completed) == len(ids):
        print("COMPLETE: all 33 cases recorded", flush=True)
        return
    # An interrupted or unknown failed case needs inspection, never automatic retry.
    if completed and not read(completed[-1])["safe_to_continue"]:
        assert (completed[-1].parent / "continuation-review.json").exists(), "inspect last failure before continuing"
    previous = read(completed[-1].parent / "ledger-after.json") if completed else read(OUT / "ledger-before.json")
    assert read(LEDGER) == previous, "shared ledger changed outside this run"
    case_id = ids[len(completed)]
    directory = OUT / "cases" / case_id
    directory.mkdir(parents=True, exist_ok=False)
    write_new(directory / "started.json", {"case_id": case_id, "at": datetime.now(UTC).isoformat(), "ledger_count": len(previous["attempts"])})
    args = [sys.executable, "-X", "utf8", "scripts/evaluate_m4.py", "--stage", "agent", "--execute", "--case", case_id,
            "--decision-model", "glm-5.2", "--agent-mode", "hybrid", "--output", str(directory / "report.json")]
    for category, ceiling in CEILINGS.items():
        args.extend(["--" + category.replace("_", "-") + "-ceiling", str(ceiling)])
    write_new(directory / "command.json", args)
    with (directory / "execution.log").open("xb") as log:
        result = subprocess.run(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    after = read(LEDGER)
    assert after["attempts"][:len(previous["attempts"])] == previous["attempts"], "ledger history changed"
    write_new(directory / "ledger-after.json", after)
    delta = after["attempts"][len(previous["attempts"]):]
    write_new(directory / "attempts.json", {"attempts": delta})
    for attempt in delta:
        folder = {"understanding": "understanding", "decision": "decision", "initial_embedding": "query", "research_embedding": "query"}[attempt["category"]]
        source = ROOT / "data/cache/m4" / folder / (attempt["fingerprint"] + ".json")
        if source.exists():
            dest = directory / "responses" / folder / source.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
    report = read(directory / "report.json") if (directory / "report.json").exists() else {}
    prediction = report.get("rows", [{}])[-1] if report.get("rows") else {}
    if prediction:
        write_new(directory / "prediction.json", prediction)
    run = prediction.get("run", {})
    diagnostics = (run.get("usage") or {}).get("acceptance_decisions", [])
    rejection = any(d.get("validation", {}).get("status") == "rejected" for d in diagnostics)
    consistent = prediction.get("http_database_consistent") and prediction.get("idempotent_no_extra_run")
    safe = bool(result.returncode == 0 and consistent and (run.get("run_status") == "waiting_review" or
                run.get("error_code") == "proposal_unsupported_action_claim" or rejection))
    counts = Counter(a["category"] for a in after["attempts"])
    assert all(counts[k] <= v for k,v in CEILINGS.items())
    verify()
    summary = {"case_id": case_id, "finished_at": datetime.now(UTC).isoformat(), "process_exit_code": result.returncode,
        "run_status": run.get("run_status"), "error_code": run.get("error_code"), "safe_to_continue": safe,
        "new_attempts": dict(Counter(a["category"] for a in delta)), "cumulative_attempts": len(after["attempts"]),
        "raw_action": (prediction.get("raw_proposal") or {}).get("next_step"),
        "final_action": (prediction.get("final_proposal") or {}).get("next_step"),
        "prediction_sha256": sha(directory / "prediction.json") if prediction else None}
    write_new(directory / "execution.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not safe:
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    initialize() if args.init else execute_next()
