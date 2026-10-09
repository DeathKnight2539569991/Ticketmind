"""Production-graph Phase 7 E2E run with durable model-call and cost limits."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from time import monotonic
from uuid import uuid4
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
os.environ["LANGSMITH_TRACING"] = "false"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from phase7_offline import DATA, check_frozen, jsonl
from ticketmind.agent.dev_acceptance import AcceptanceAdapters, AttemptLedger, acceptance_lock, write_json
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.schemas import AgentMessage
from ticketmind.agent.proposals import Resolution, SearchDocs
from ticketmind.agent.runtime import AgentRunner
from ticketmind.agent.semantic_judge import JudgeResult
from ticketmind.core.config import AuthSettings, MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.db.testing import isolated_database
from ticketmind.documents.importer import import_markdown, sync_docs
from ticketmind.documents.index import MilvusDocsIndex
from ticketmind.knowledge.sources import load_sources
from ticketmind.main import create_app
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.tickets.models import ProcessingResult
from fastapi.testclient import TestClient

VECTOR_CACHE = ROOT.parent / "log/evaluation/phase7-retrieval-20261007/vectors"
RUNS = ROOT.parent / "log/evaluation"
DOCS_VERSION = "synthetic-phase7-docs-v1"
CEILINGS = {"initial_embedding": 0, "research_embedding": 48, "decision": 288, "judge": 96}
MAX_COST_CNY = 5.0
MODEL_PRICES = {"decision": (0.8, 2.7), "judge": (2.0, 8.0)}
EMBEDDING_RATE = 0.5
MAX_TOKENS = {"decision": 1600, "judge": 2000}
MAX_PROMPT_BYTES = 120_000


def digest(value):
    # Match evaluate_phase7_retrieval.digest: its default JSON spacing is part of cache identity.
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


class LocalTrace:
    def __init__(self, path, *, snapshot, thread_id, phase, runner=None, **kwargs):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config = getattr(runner, "config", None)
        metadata = {"phase": phase, "thread_id": thread_id, "run_id": snapshot.get("run_id"),
            "agent_version": getattr(config, "agent_version", None),
            "retrieval_mode": getattr(config, "retrieval_mode", None),
            "docs_retrieval_mode": getattr(config, "docs_retrieval_mode", None),
            "corpus_version": getattr(getattr(runner, "corpus", None), "version", None),
            "decision_model": getattr(config, "decision_model", None),
            "judge_model": getattr(config, "judge_model", None)}
        self._write({"type": "trace_start", "metadata": metadata})

    def _write(self, record):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def event(self, name, *, node, inputs=None, outputs=None, error=None, run_type="chain"):
        self._write({"type": "event", "name": name, "node": node, "run_type": run_type,
                     "inputs": inputs or {}, "outputs": outputs or {}, "error": error,
                     "at": datetime.now(UTC).isoformat()})

    def finish(self, *, outcome, error=None):
        self._write({"type": "trace_finish", "outcome": outcome, "error": error,
                     "at": datetime.now(UTC).isoformat()})


def install_local_trace(output, case_id):
    import ticketmind.agent.review as review
    original = review.trace_for
    trace_path = output / "traces" / f"{case_id}.jsonl"
    review.trace_for = lambda snapshot, thread_id, phase, **kwargs: LocalTrace(
        trace_path, snapshot=snapshot, thread_id=thread_id, phase=phase, **kwargs)
    return original, trace_path


def validate_inputs():
    gate = check_frozen(require_reviewed=True)
    cases = jsonl(DATA / "evaluation_cases.jsonl")
    labels = {row["case_id"]: row for row in jsonl(DATA / "evaluation_labels.jsonl")}
    if len(cases) != 48 or len(labels) != 48:
        raise ValueError("phase7_fixed_48_required")
    if any(row.get("label_status") != "pending_review" for row in labels.values()):
        raise ValueError("unexpected_label_status")
    # The model-facing object is constructed only from the TicketCreate input.
    rows = []
    for row in cases:
        ticket_input = row["input"]
        messages = [AgentMessage(role="customer", content=ticket_input["body"])]
        query = build_retrieval_query(subject=ticket_input["subject"], messages=messages)
        rows.append({"case_id": row["case_id"], "input": ticket_input, "query": query,
                     "input_hash": digest(ticket_input), "group_id": row["group_id"]})
    return gate, rows, labels


def exact_query_vectors(queries, workspace_id):
    """Load only cache batches whose stored fingerprint binds the exact current query text."""
    workspace_hash = digest(workspace_id)
    vectors = {}
    for path in sorted(VECTOR_CACHE.glob("*.json")):
        cache = json.loads(path.read_text(encoding="utf-8"))
        ids = cache.get("ids")
        values = cache.get("vectors")
        if not isinstance(ids, list) or not isinstance(values, list) or len(ids) != len(values):
            continue
        if any(case_id not in queries for case_id in ids):
            continue
        batch = [(case_id, queries[case_id]) for case_id in ids]
        expected = digest({"workspace": workspace_hash, "model": "text-embedding-v4",
                           "dimension": 1024, "type": "query", "items": batch})
        if cache.get("fingerprint") != expected:
            continue
        for case_id, vector in zip(ids, values):
            if case_id in vectors:
                raise ValueError("duplicate_exact_query_vector")
            if len(vector) != 1024:
                raise ValueError("invalid_cached_vector_dimension")
            vectors[case_id] = vector
    return vectors


class CostLedger(AttemptLedger):
    """Attempt ledger that reserves a request's worst case before the HTTP call."""
    def __init__(self, path, ceilings):
        super().__init__(path, ceilings)
        self.data.setdefault("settled_cost_cny", 0.0)
        self.data.setdefault("cost_cap_cny", MAX_COST_CNY)
        self.active_record = None
        self.current_case_id = None
        self.pending_embedding_count = 0
        self.pending_call_type = None
        self.data.setdefault("embedding_segments", 0)
        self.save()

    def attempt(self, category, fingerprint, operation):
        def tracked(record):
            self.active_record = record
            if self.current_case_id:
                record["case_id"] = self.current_case_id
                self.save()
            self.pending_call_type = category
            try:
                return operation(record)
            finally:
                self.active_record = None
                self.pending_call_type = None
        return super().attempt(category, fingerprint, tracked)

    def reserve(self, category, prompt_bytes, max_output_tokens=0):
        record = self.active_record
        if record is None:
            raise RuntimeError("cost_reservation_without_started_attempt")
        if record.get("reserved_cny") is not None:
            return
        if prompt_bytes > MAX_PROMPT_BYTES:
            raise ValueError("prompt_byte_ceiling_exceeded")
        if category == "embedding":
            prompt_upper = prompt_bytes + 128
            reserve_cny = prompt_upper * EMBEDDING_RATE / 1_000_000
        else:
            input_rate, output_rate = MODEL_PRICES[category]
            prompt_upper = prompt_bytes + 512
            reserve_cny = (prompt_upper * input_rate + max_output_tokens * output_rate) / 1_000_000
        outstanding = sum(float(item.get("reserved_cny") or 0) for item in self.data["attempts"])
        if float(self.data["settled_cost_cny"]) + outstanding + reserve_cny > MAX_COST_CNY + 1e-12:
            raise RuntimeError("phase7_cost_cap_reached")
        if category == "embedding":
            if self.data["embedding_segments"] >= 48:
                raise RuntimeError("phase7_embedding_segment_limit")
            self.data["embedding_segments"] += 1
        record.update(reserved_cny=round(reserve_cny, 9), reserved_prompt_bytes=prompt_bytes,
                      reserved_input_token_upper=prompt_upper,
                      reserved_output_tokens=max_output_tokens, cost_state="reserved")
        self.save()  # Cost reservation is durable before control reaches the network client.

    def record_usage(self, usage, category):
        if not isinstance(usage, dict):
            return
        if category == "embedding":
            tokens = usage.get("total_tokens")
            if type(tokens) is int:
                actual = tokens * EMBEDDING_RATE / 1_000_000
            else:
                return
        else:
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if type(prompt) is not int or type(completion) is not int:
                return
            in_rate, out_rate = MODEL_PRICES[category]
            actual = (prompt * in_rate + completion * out_rate) / 1_000_000
        record = self.active_record
        if record is None:
            return
        reserved = float(record.get("reserved_cny") or 0)
        # If reported usage exceeds the bound, stop the batch and account the larger amount.
        if float(self.data["settled_cost_cny"]) + actual > MAX_COST_CNY + 1e-12:
            record["cost_state"] = "reported_over_cap"
            record["reported_cost_cny"] = round(actual, 9)
            self.save()
            raise RuntimeError("reported_usage_over_cost_cap")
        self.data["settled_cost_cny"] = round(float(self.data["settled_cost_cny"]) + actual, 9)
        record.update(cost_state="settled", actual_cost_cny=round(actual, 9), reserved_cny=0.0)
        self.save()


class BudgetAdapters(AcceptanceAdapters):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._phase7_category = None

    def decision(self, state, timeout, usage):
        self._phase7_category = "decision"
        return super().decision(state, timeout, usage)

    def judge(self, state, proposal, timeout, usage):
        self._phase7_category = "judge"
        return super().judge(state, proposal, timeout, usage)


def install_budget_hooks(adapters, ledger):
    """Wrap the acceptance module's final model/embedding boundaries, fail closed."""
    import ticketmind.agent.dev_acceptance as acceptance
    original_generate = acceptance.generate_text
    original_embeddings = acceptance.build_budgeted_embeddings

    def guarded_generate(**kwargs):
        active = ledger.active_record
        if active is None:
            raise RuntimeError("model_request_without_started_attempt")
        category = active["category"]
        system, user = kwargs["system_prompt"], kwargs["user_prompt"]
        prompt_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        ledger.reserve(category, prompt_bytes, MAX_TOKENS[category])
        callback = kwargs.get("response_callback")
        def capture(value):
            if callback:
                callback(value)
            ledger.record_usage(value.get("usage"), category)
        kwargs["response_callback"] = capture
        return original_generate(**kwargs)

    def guarded_embeddings(settings, remaining, callback):
        inner = original_embeddings(settings, remaining, callback)
        class Guarded:
            def embed_query(self, text):
                ledger.reserve("embedding", len(text.encode("utf-8")))
                value = inner.embed_query(text)
                record = ledger.active_record
                if record is not None:
                    ledger.record_usage(record.get("usage"), "embedding")
                return value
        return Guarded()

    acceptance.generate_text = guarded_generate
    acceptance.build_budgeted_embeddings = guarded_embeddings
    return original_generate, original_embeddings


def raw_outputs(output, adapter, usage, field):
    records = []
    for item in (usage or {}).get(field, []):
        raw_file = adapter.path("decision" if field == "acceptance_decisions" else "judge", item["fingerprint"])
        cache = json.loads(raw_file.read_text(encoding="utf-8"))
        response = cache.get("response", {})
        content = response.get("choices", [{}])[0].get("content")
        parsed = None
        try:
            parsed = json.loads(content or "")
        except (ValueError, TypeError):
            pass
        user_prompt = cache.get("user_prompt", "")
        records.append({"fingerprint": item["fingerprint"], "request_id": item.get("request_id"),
                        "usage": item.get("usage"), "content": parsed,
                        "repair_turn": '"guardrail_feedback"' in user_prompt,
                        "prompt_bytes": len(cache.get("system_prompt", "").encode("utf-8")) +
                                       len(user_prompt.encode("utf-8"))})
    return records


def attempt_fingerprints(results):
    values = set()
    for result in results:
        for key in ("acceptance_decisions", "acceptance_judges"):
            for item in (result.get("usage") or {}).get(key, []):
                if item.get("fingerprint"):
                    values.add(item["fingerprint"])
        for field in ("decisions", "judges"):
            values.update(item.get("fingerprint") for item in result.get(field, []) if item.get("fingerprint"))
    return values


def case_has_model_cache(output, row):
    needles = [value for value in (row["input"].get("subject"), row["input"].get("body")) if value]
    for folder in (output / "cache" / "decision", output / "cache" / "judge"):
        if not folder.exists():
            continue
        for path in folder.glob("*.json"):
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                return True
            prompt = "\n".join((cache.get("system_prompt", ""), cache.get("user_prompt", "")))
            if all(value in prompt or json.dumps(value, ensure_ascii=False)[1:-1] in prompt for value in needles):
                return True
    return False


def verified_precall_failure(output, row, prior, attempts, all_results):
    error = (prior.get("error") or "").casefold()
    db_error = prior.get("error_type") == "OperationalError" and any(token in error for token in
        ("server closed the connection", "connection failed", "not yet accepting connections", "consistent recovery"))
    trace_exists = (output / "traces" / f"{row['case_id']}.jsonl").exists()
    usage = prior.get("usage") or {}
    linked_in_result = bool(prior.get("decisions") or prior.get("judges") or
        usage.get("acceptance_decisions") or usage.get("acceptance_judges"))
    linked_in_ledger = any(item.get("case_id") == row["case_id"] for item in attempts)
    known_fingerprints = attempt_fingerprints(all_results)
    ledger_fingerprints = {item.get("fingerprint") for item in attempts}
    ledger_is_fully_linked = ledger_fingerprints <= known_fingerprints
    no_request_cache = not case_has_model_cache(output, row)
    return {"eligible": db_error and not trace_exists and not linked_in_result and not linked_in_ledger
                        and ledger_is_fully_linked and no_request_cache,
            "db_error": db_error, "trace_exists": trace_exists, "linked_in_result": linked_in_result,
            "linked_in_ledger": linked_in_ledger, "ledger_is_fully_linked": ledger_is_fully_linked,
            "no_request_cache": no_request_cache}


def append_infrastructure_event(path, event):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"events": []}
    data["events"].append(event)
    write_json(path, data)


def is_database_outage(error):
    try:
        from sqlalchemy.exc import OperationalError
        if not isinstance(error, OperationalError):
            return False
    except ImportError:
        return False
    message = str(error).casefold()
    return any(token in message for token in ("server closed the connection", "connection failed",
        "not yet accepting connections", "consistent recovery state has not been reached"))


def run_one(row, label, args, gate, cache_vectors):
    qwen = QwenSettings().model_copy(update={"model": "qwen3.8-flash", "embedding_model": "text-embedding-v4"})
    config = ProcessingSettings(retrieval_mode="hybrid", retrieval_top_k=5, retrieval_candidate_k=20,
        retrieval_rrf_k=60, decision_model="qwen3.8-flash", judge_model="deepseek-v4.1-flash",
        docs_retrieval_mode="bm25", docs_dataset=DOCS_VERSION, corpus_path=DATA / "historical_cases.jsonl",
        max_search_rounds=2, max_docs_search_rounds=2, max_agent_steps=8)
    # A frozen adapter is used only for the exact Phase 7 Cases corpus; retrieval is the production service.
    corpus = load_sources(config.corpus_path)
    vectors_by_query = {item["query"]: cache_vectors[item["case_id"]] for item in [row]
                        if item["case_id"] in cache_vectors}

    output = {"case_id": row["case_id"], "input_hash": row["input_hash"], "status": "failed",
              "verification": "production_AgentRunner_API_PostgresSaver_Milvus", "approval_simulation": "isolated_only"}
    trace_restore, trace_path = install_local_trace(args.output, row["case_id"])
    with isolated_database(os.getenv("TICKETMIND_TEST_DATABASE_URL")) as (_, factory, schema):
        # Import the frozen 8-document corpus into this disposable schema and reconcile only its production BM25 index.
        import_markdown(factory, DATA / "docs", version=DOCS_VERSION)
        client = build_milvus_client(MilvusSettings())
        try:
            docs_import = sync_docs(factory, MilvusDocsIndex(client), version=DOCS_VERSION)
        finally:
            client.close()
        ledger = args.ledger
        decisions = BudgetAdapters(qwen, args.output / "cache", ledger)
        judges = BudgetAdapters(qwen.model_copy(update={"model": "deepseek-v4.1-flash"}), args.output / "cache", ledger)
        original_generate, original_embeddings = install_budget_hooks(decisions, ledger)
        if args.offline_smoke:
            def deterministic_decision(state, timeout, usage):
                docs = state.get("docs_hits", [])
                if not docs:
                    return SearchDocs(next_step="search_docs", reason="离线验证生产Docs BM25检索入口", query=row["input"]["subject"])
                return Resolution(next_step="propose_resolution", reason="仅用于验证隔离API与审核状态迁移。",
                    reply="这是确定性离线验证草稿，等待隔离环境中的审核模拟。",
                    evidence_ids=[docs[0].source_id])
            decisions.decision = deterministic_decision
            judges.judge = lambda state, proposal, timeout, usage: JudgeResult(violations=[])
        # AgentRunner passes phase-aware embedding factories to the acceptance adapters.
        if row["query"] in vectors_by_query:
            class ExactInitial:
                def embed_query(self, text):
                    if text in vectors_by_query: return vectors_by_query[text]
                    return decisions.embeddings(lambda: 30.0, retrieval_phase="research").embed_query(text)
            embedding_factory = lambda remaining, retrieval_phase="initial": ExactInitial() if retrieval_phase == "initial" else decisions.embeddings(remaining, retrieval_phase="research")
        else:
            embedding_factory = decisions.embeddings
        runner = AgentRunner(qwen, MilvusSettings(), config, embedding_factory=embedding_factory,
            decision_fn=decisions.decision, judge_fn=judges.judge, corpus=corpus,
            docs_store=None, session_factory=factory)
        auth = AuthSettings(_env_file=None, operator_token=secrets.token_urlsafe(32),
                            reviewer_token=secrets.token_urlsafe(32))
        app = create_app(session_factory=factory, runner=runner, auth_settings=auth, processing_settings=config)
        try:
            with TestClient(app) as http:
                http.headers["Authorization"] = "Bearer " + auth.operator_token.get_secret_value()
                created = http.post("/tickets", json=row["input"], headers={"Idempotency-Key": uuid4().hex})
                if created.status_code != 201: raise RuntimeError(f"ticket_create_{created.status_code}")
                ticket_id = created.json()["id"]
                ticket_response = http.get(f"/tickets/{ticket_id}")
                if ticket_response.status_code != 200: raise RuntimeError(f"ticket_read_{ticket_response.status_code}")
                ticket = ticket_response.json()
                started = monotonic()
                run_key_for_run = uuid4().hex
                response = http.post(f"/tickets/{ticket['id']}/runs",
                    json={"expected_version": ticket["version"], "trigger_message_id": ticket["messages"][-1]["id"],
                          "retrieval_mode": "hybrid"}, headers={"Idempotency-Key": run_key_for_run})
                duration_ms = round((monotonic() - started) * 1000)
                if response.status_code != 201: raise RuntimeError(f"run_start_{response.status_code}")
                run = response.json()
                output.update(status="succeeded" if run["run_status"] == "waiting_review" else "failed",
                    ticket_id=ticket["id"], run_id=run["id"], thread_id=run["thread_id"],
                    run_status=run["run_status"], proposal=run["proposal"], action=run["action"],
                    retrieval_evidence=run["retrieval_evidence"], tool_calls=run["tool_calls"],
                    usage=run.get("usage"), duration_ms=duration_ms, temporary_schema=schema,
                    docs_import=docs_import, idempotency_key_replay=False,
                    execution_mode="deterministic_no_model_calls" if args.offline_smoke else "authorized_live",
                    cases_authority="frozen Phase7 corpus adapter with production retrieval service",
                    docs_authority="PostgreSQL DocStore with production BM25 service")
                if run.get("error_code") is not None:
                    output["run_error_code"] = run["error_code"]
                if run.get("error") is not None:
                    output["run_error"] = run["error"]
                output["local_trace"] = str(trace_path)
                with factory() as session:
                    saved = session.get(ProcessingResult, UUID(run["id"]))
                    output["business_row_matches_api"] = bool(saved and saved.proposal == run["proposal"] and
                        saved.retrieval_evidence == run["retrieval_evidence"] and saved.tool_calls == run["tool_calls"])
                    output["durable_checkpoint_thread"] = run["thread_id"]
                checkpoint = app.state.workflow.graph().get_state(app.state.workflow.config(run["thread_id"]))
                output["waiting_review_checkpoint"] = {"next": list(checkpoint.next),
                    "has_interrupt": any(task.interrupts for task in checkpoint.tasks)}
                LocalTrace(trace_path, snapshot={"run_id": run["id"]}, thread_id=run["thread_id"],
                           phase="api_observation", runner=runner).event("api.waiting_review", node="review",
                    outputs=output["waiting_review_checkpoint"])
                decision_rows = raw_outputs(args.output, decisions, run.get("usage"), "acceptance_decisions")
                judge_rows = raw_outputs(args.output, judges, run.get("usage"), "acceptance_judges")
                output["decisions"] = decision_rows
                output["judges"] = judge_rows
                # Verify API idempotency before isolated review apply.
                replay = http.post(f"/tickets/{ticket['id']}/runs",
                    json={"expected_version": ticket["version"], "trigger_message_id": ticket["messages"][-1]["id"],
                          "retrieval_mode": "hybrid"}, headers={"Idempotency-Key": run_key_for_run})
                output["idempotency_key_replay"] = replay.status_code == 200 and replay.json() == run
                if run["run_status"] == "waiting_review":
                    http.headers["Authorization"] = "Bearer " + auth.reviewer_token.get_secret_value()
                    run_key = uuid4().hex
                    review_payload = {"decision": "approve", "expected_version": run["ticket_version"],
                                      "comment": "Phase7 isolated persistence simulation; not human quality approval."}
                    review_headers = {"Idempotency-Key": run_key}
                    approved = http.post(f"/tickets/{ticket['id']}/runs/{run['id']}/review",
                                         json=review_payload, headers=review_headers)
                    review_replay = http.post(f"/tickets/{ticket['id']}/runs/{run['id']}/review",
                                              json=review_payload, headers=review_headers)
                    output["approval_simulation"] = {"status_code": approved.status_code,
                        "run_status": approved.json().get("run_status") if approved.status_code == 201 else None,
                        "ticket_status": http.get(f"/tickets/{ticket['id']}").json().get("status"),
                        "idempotency_replay": review_replay.status_code == 200 and review_replay.json() == approved.json()}
                    output["status_persisted_after_simulation"] = approved.status_code == 201 and output["approval_simulation"]["idempotency_replay"]
                    reviewed_ticket = http.get(f"/tickets/{ticket['id']}").json()
                    output["published_message_persisted_in_isolated_schema"] = any(
                        message.get("operation") == "review" for message in reviewed_ticket.get("messages", []))
                    LocalTrace(trace_path, snapshot={"run_id": run["id"]}, thread_id=run["thread_id"],
                               phase="api_observation", runner=runner).event("api.review_applied", node="review",
                        outputs={"run_status": output["approval_simulation"]["run_status"],
                                 "ticket_status": output["approval_simulation"]["ticket_status"],
                                 "published_message_persisted": output["published_message_persisted_in_isolated_schema"]})
        finally:
            import ticketmind.agent.dev_acceptance as acceptance
            acceptance.generate_text, acceptance.build_budgeted_embeddings = original_generate, original_embeddings
            import ticketmind.agent.review as review
            review.trace_for = trace_restore
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", help="fixed ticket IDs; first live batch must be six")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--offline-smoke", action="store_true", help="real isolated DB/API/retrieval graph, deterministic model substitutes")
    parser.add_argument("--resume-precall-failures", action="store_true",
                        help="resume only prior database-outage cases proven to have no model call or local trace")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to(RUNS) or args.output == RUNS:
        parser.error("output must be under D:/AnalyzeAgent/log/evaluation")
    gate, rows, labels = validate_inputs()
    all_ids = [row["case_id"] for row in rows]
    selected_ids = args.cases or all_ids
    by_id = {row["case_id"]: row for row in rows}
    if len(selected_ids) != len(set(selected_ids)) or set(selected_ids) - set(by_id):
        parser.error("unknown_or_duplicate_case_id")
    qwen = QwenSettings().model_copy(update={"model": "qwen3.8-flash", "embedding_model": "text-embedding-v4"})
    vectors = exact_query_vectors({row["case_id"]: row["query"] for row in rows}, qwen.workspace_id)
    if len(vectors) != 48:
        raise ValueError(f"exact_initial_query_vector_cache_incomplete:{len(vectors)}/48")
    config = {"snapshot_hash": gate["snapshot_hash"], "case_ids": all_ids,
        "decision_model": "qwen3.8-flash", "judge_model": "deepseek-v4.1-flash",
        "embedding_model": "text-embedding-v4", "retrieval": {"cases": "production_hybrid", "docs": "production_bm25"},
        "exact_initial_query_cache_ids": sorted(vectors), "ceilings": CEILINGS, "cost_cap_cny": MAX_COST_CNY,
        "cost_rates_cny_per_million": {"decision": list(MODEL_PRICES["decision"]), "judge": list(MODEL_PRICES["judge"]),
                                         "embedding": EMBEDDING_RATE},
        "trace": "local", "automatic_retries": 0, "labels_to_model": False,
        "approval": "disposable_schema_simulation_only"}
    args.output.mkdir(parents=True, exist_ok=True)
    config_path = args.output / "config.json"
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config:
        raise ValueError("phase7_run_config_conflict")
    write_json(config_path, config)
    if not args.execute and not args.offline_smoke:
        print(json.dumps({"status": "prepared_no_external_calls", "selected": selected_ids,
                          "cache_hits": len(vectors), "cache_missing": [r["case_id"] for r in rows if r["case_id"] not in vectors],
                          "config": config}, ensure_ascii=False))
        return
    if args.offline_smoke and len(selected_ids) != 1:
        parser.error("offline-smoke accepts exactly one case ID")
    with acceptance_lock(args.output):
        ledger = CostLedger(args.output / "attempts.json", CEILINGS)
        args.ledger = ledger
        results_path = args.output / "results.json"
        prior = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {"results": []}
        if prior.get("config") not in (None, config):
            raise ValueError("phase7_run_config_conflict")
        results = list(prior.get("results", []))
        if args.resume_precall_failures:
            attempts_before_resume = ledger.data["attempts"]
            all_fingerprints = attempt_fingerprints(results)
            if {item.get("fingerprint") for item in attempts_before_resume} - all_fingerprints:
                raise ValueError("cannot_prove_all_existing_attempts_link_to_saved_success_rows")
            resume_rows = [item for item in results if item.get("case_id") in selected_ids and item.get("status") == "failed"]
            if not resume_rows:
                raise ValueError("no_precall_failures_selected_for_resume")
            for old in resume_rows:
                check = verified_precall_failure(args.output, by_id[old["case_id"]], old,
                                                 attempts_before_resume, results)
                if not check["eligible"]:
                    raise ValueError(f"precall_resume_not_proven_safe:{old['case_id']}:{check}")
                append_infrastructure_event(args.output / "infrastructure-events.json", {
                    "type": "prior_precall_failure_preserved_before_explicit_resume",
                    "case_id": old["case_id"], "original_result": old, "verification": check,
                    "preserved_at": datetime.now(UTC).isoformat()})
            resumed_ids = {item["case_id"] for item in resume_rows}
            results = [item for item in results if item.get("case_id") not in resumed_ids]
        done = {r["case_id"] for r in results}
        if done & set(selected_ids):
            raise ValueError("phase7_case_already_recorded")
        for case_id in selected_ids:
            ledger.current_case_id = case_id
            attempts_before_case = len(ledger.data["attempts"])
            try:
                result = run_one(by_id[case_id], labels[case_id], args, gate, vectors)
            except Exception as exc:
                result = {"case_id": case_id, "status": "failed", "error_type": type(exc).__name__,
                          "error": str(exc)}
                orig = getattr(exc, "orig", None)
                diagnostic = getattr(orig, "diag", None)
                error_code = (getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or
                              getattr(diagnostic, "sqlstate", None) or getattr(orig, "pgcode", None))
                if error_code is not None:
                    result["error_code"] = str(error_code)
                if is_database_outage(exc):
                    attempts_for_case = [item for item in ledger.data["attempts"]
                                         if item.get("case_id") == case_id]
                    trace_exists = (args.output / "traces" / f"{case_id}.jsonl").exists()
                    cache_absent = not case_has_model_cache(args.output, by_id[case_id])
                    result.update(infrastructure_failure=True,
                        pre_call_failure=not attempts_for_case and not trace_exists and cache_absent,
                        attempts_for_case=len(attempts_for_case), trace_exists=trace_exists,
                        no_case_prompt_cache=cache_absent)
                    append_infrastructure_event(args.output / "infrastructure-events.json", {
                        "type": "database_outage", "case_id": case_id, "error_type": type(exc).__name__,
                        "error": str(exc), "attempts_before": attempts_before_case,
                        "attempts_after": len(ledger.data["attempts"]),
                        "pre_call_failure": result["pre_call_failure"], "verification": {
                            "case_attempts": len(attempts_for_case), "trace_exists": trace_exists,
                            "no_case_prompt_cache": cache_absent},
                        "occurred_at": datetime.now(UTC).isoformat()})
            finally:
                ledger.current_case_id = None
            results.append(result)
            write_json(results_path, {"config": config, "results": results,
                "ledger": ledger.data, "unrun_case_ids": [key for key in all_ids if key not in {r["case_id"] for r in results}]})
            print(json.dumps({"case_id": case_id, "status": result["status"],
                "spent_cny": ledger.data.get("settled_cost_cny"), "reserved_cny": sum(
                    float(x.get("reserved_cny") or 0) for x in ledger.data["attempts"])}, ensure_ascii=False), flush=True)
            if result.get("infrastructure_failure"):
                break
        write_json(args.output / "summary.json", {"config": config, "results": results,
            "ledger": ledger.data, "not_run_case_ids": [key for key in all_ids if key not in {r["case_id"] for r in results}],
            "limitations": ["Synthetic fixed set; labels are pending review.",
                            "Answer correctness remains unadjudicated; Judge only checks guardrail violations.",
                            "Approval is a disposable-schema persistence simulation, not a human quality approval."]})


if __name__ == "__main__":
    main()
