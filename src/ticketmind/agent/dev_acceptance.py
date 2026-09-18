"""Acceptance-only adapters: durable attempt ceilings, exact caches, raw decisions.

The caller owns an exclusive ledger lock for the entire session. Never installed
in the production API. Limits are cumulative ceilings, not credits per restart.
"""
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, ConfigDict, ValidationError

from ticketmind.agent.decide import decision_options, decision_messages
from ticketmind.agent.dev_cache import CachedUnderstanding, CachedQueryEmbeddings
from ticketmind.agent.dev_decision_cache import decision_fingerprint
from ticketmind.agent.proposals import decision_adapter, validate_proposal, validate_decision_evidence
from ticketmind.agent.run_cache import (UnderstandingCache, QueryVectorCache, load_cache,
    save_cache, understanding_fingerprint, query_fingerprint)
from ticketmind.agent.runtime import build_budgeted_embeddings
from ticketmind.agent.understand import understand_ticket
from ticketmind.llm.client import generate_text

CATEGORIES = ("understanding", "initial_embedding", "research_embedding", "decision")


class Record(BaseModel):
    model_config = ConfigDict(extra="allow")


def write_json(path, data):
    save_cache(Path(path), Record.model_validate(data))


@contextmanager
def acceptance_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.lock"
    # An interrupted session intentionally leaves its lock for manual inspection.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(datetime.now(UTC).isoformat())
    try:
        yield
    finally:
        path.unlink()


class AttemptLedger:
    def __init__(self, path, ceilings):
        if set(ceilings) != set(CATEGORIES) or any(type(v) is not int or v < 0 for v in ceilings.values()):
            raise ValueError("必须分别指定四类非负整数上限")
        self.path, self.ceilings = Path(path), ceilings
        self.data = Record.model_validate_json(self.path.read_text(encoding="utf-8")).model_dump() if self.path.exists() else {"attempts": []}
        self.cache_hits = []

    def save(self):
        write_json(self.path, self.data)

    def attempt(self, category, fingerprint, operation):
        attempts = self.data["attempts"]
        if any(a["fingerprint"] == fingerprint and a["category"] == category for a in attempts):
            raise RuntimeError("该请求已有尝试但无可复用结果，禁止自动重试")
        if sum(a["category"] == category for a in attempts) >= self.ceilings[category]:
            raise RuntimeError(f"{category} 新增调用未授权或累计额度已用完")
        record = {"category": category, "fingerprint": fingerprint, "status": "started",
                  "started_at": datetime.now(UTC).isoformat(), "usage": None}
        attempts.append(record)
        self.save()  # Consume the attempt before any network call; crash is not a refund.
        started = monotonic()
        try:
            result = operation(record)
            record["status"] = "succeeded"
            return result
        except Exception as exc:
            record["status"], record["error_type"] = "failed", type(exc).__name__
            raise
        finally:
            record["duration_ms"] = round((monotonic() - started) * 1000)
            self.save()

    def hit(self, category, fingerprint):
        self.cache_hits.append({"category": category, "fingerprint": fingerprint})


class AcceptanceAdapters:
    def __init__(self, settings, directory, ledger, *, legacy_directory=None):
        self.settings, self.directory, self.ledger = settings, Path(directory), ledger
        self.legacy_directory = legacy_directory
        self.remaining = lambda: 30.0
        self.embedding_index = 0

    def path(self, kind, fingerprint):
        return self.directory / kind / f"{fingerprint}.json"

    def understanding(self, *, settings, subject, body):
        fp = understanding_fingerprint(settings=settings, subject=subject, body=body)
        path = self.path("understanding", fp)
        if self.legacy_directory:
            cache = CachedUnderstanding(self.legacy_directory / "understanding.json").read(
                settings=settings, subject=subject, body=body)
        else:
            cache = load_cache(path, UnderstandingCache, expected_fingerprint=fp)
        if cache:
            self.ledger.hit("understanding", fp)
            return cache.understanding
        def call(record):
            result = understand_ticket(settings=settings, subject=subject, body=body, timeout=self.remaining(),
                                       usage_callback=lambda value: record.update(usage=value))
            save_cache(path, UnderstandingCache(subject=subject, body=body, model=settings.model,
                                               request_fingerprint=fp, understanding=result))
            return result
        return self.ledger.attempt("understanding", fp, call)

    def embeddings(self, remaining):
        self.remaining, self.embedding_index = remaining, 0
        return self

    def embed_query(self, text):
        category = "initial_embedding" if self.embedding_index == 0 else "research_embedding"
        self.embedding_index += 1
        fp = query_fingerprint(settings=self.settings, query=text)
        path = self.path("query", fp)
        if category == "initial_embedding" and self.legacy_directory:
            cache = CachedQueryEmbeddings(self.settings, self.legacy_directory / "query.json", factory=None).read(text)
        else:
            cache = load_cache(path, QueryVectorCache, expected_fingerprint=fp)
        if cache:
            self.ledger.hit(category, fp)
            return cache.vector
        def call(record):
            result = build_budgeted_embeddings(self.settings, self.remaining,
                lambda value: record.update(usage=value)).embed_query(text)
            save_cache(path, QueryVectorCache(query=text, model=self.settings.embedding_model,
                                              request_fingerprint=fp, vector=result))
            return result
        return self.ledger.attempt(category, fp, call)

    def decision(self, state, timeout, usage):
        fp = decision_fingerprint(self.settings, state)
        path = self.path("decision", fp)
        if path.exists():
            response = Record.model_validate_json(path.read_text(encoding="utf-8")).model_dump()
            if response.get("request_fingerprint") != fp:
                raise ValueError("决策缓存指纹不匹配")
            self.ledger.hit("decision", fp)
        else:
            system, user = decision_messages(state)
            def call(record):
                record["agent_steps"] = state["agent_steps"]
                def capture(value):
                    record.update(usage=value["usage"], request_id=value["request_id"], cache_path=str(path))
                    # Save even malformed JSON / truncated responses before policy validation.
                    write_json(path, {"request_fingerprint": fp, "response": value,
                                      "system_prompt": system, "user_prompt": user})
                generate_text(settings=self.settings, system_prompt=system, user_prompt=user,
                    json_mode=True, timeout=timeout, generation_options=decision_options(self.settings), response_callback=capture)
            self.ledger.attempt("decision", fp, call)
            response = Record.model_validate_json(path.read_text(encoding="utf-8")).model_dump()
        raw = response["response"]
        diagnostic = {"fingerprint": fp, "usage": raw["usage"], "request_id": raw["request_id"]}
        usage.setdefault("acceptance_decisions", []).append(diagnostic)
        if not raw["choices"] or raw["choices"][0]["finish_reason"] != "stop":
            diagnostic["validation"] = {"status": "rejected", "stage": "response_completion"}
            raise ValueError("原始决策响应未正常完成；保留证据，禁止补调")
        try:
            result = decision_adapter.validate_json(raw["choices"][0]["content"] or "")
        except ValidationError as exc:
            diagnostic["validation"] = {"status": "rejected", "stage": "schema",
                "errors": [{"type": e["type"], "loc": list(e["loc"]), "message": e["msg"]}
                           for e in exc.errors(include_input=False, include_context=False, include_url=False)]}
            raise
        try:
            if result.next_step not in ("search_cases", "get_case_detail"):
                validate_proposal(result, {hit.source_id for hit in state["retrieval_hits"]})
                validate_decision_evidence(result, state["retrieval_hits"])
        except ValueError:
            diagnostic["validation"] = {"status": "rejected", "stage": "proposal_policy"}
            raise
        diagnostic["validation"] = {"status": "passed"}
        return result
