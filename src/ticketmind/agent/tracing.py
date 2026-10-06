"""Best-effort, bounded LangSmith events for the custom Agent capabilities.

Only JSON data crosses this boundary. Tracing never participates in checkpoints,
recovery contracts, budgets, or business error handling.
"""
from __future__ import annotations

from datetime import UTC, datetime
from queue import Queue
from threading import Lock, Thread
from uuid import uuid4
import logging
import re

from ticketmind.agent.state import copy_data
from ticketmind.core.config import TraceSettings

logger = logging.getLogger(__name__)
_credential = re.compile(
    r"(?i)(\b(?:api[_ -]?key|access[_ -]?token|secret|password|authorization|bearer)\b\s*[:=]?\s*)([^\s,;]+)"
    r"|\bsk-[A-Za-z0-9_-]{12,}\b"
)


def _safe_data(value):
    """Copy JSON values and redact common credential syntax in free text."""
    value = copy_data(value)
    if isinstance(value, str):
        return _credential.sub(lambda match: (match.group(1) or "") + "[REDACTED]", value)
    if isinstance(value, list):
        return [_safe_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_data(item) for key, item in value.items()}
    return value
_queue: Queue = Queue(maxsize=256)
_worker_lock = Lock()
_worker_started = False
_client = None
_client_key = None


def _worker():
    global _client, _client_key
    while True:
        api_key, project, method, kwargs = _queue.get()
        try:
            if _client is None or _client_key != api_key:
                from langsmith import Client
                _client = Client(api_key=api_key, auto_batch_tracing=False, timeout_ms=1000)
                _client_key = api_key
            if method == "create":
                _client.create_run(project_name=project, **kwargs)
            else:
                _client.update_run(**kwargs)
        except Exception as exc:
            # SDK/network failures must never escape into the Agent.
            logger.debug("langsmith_trace_failed method=%s error=%s", method, type(exc).__name__)
            _client = None
            _client_key = None
        finally:
            _queue.task_done()


def _submit(api_key, project, method, kwargs):
    global _worker_started
    try:
        with _worker_lock:
            if not _worker_started:
                Thread(target=_worker, name="ticketmind-langsmith", daemon=True).start()
                _worker_started = True
        _queue.put_nowait((api_key, project, method, kwargs))
    except Exception:
        # Includes thread startup errors and queue pressure.
        return


class Trace:
    """Invocation-local identifiers; asynchronous SDK writes are process-wide."""

    def __init__(self, settings: TraceSettings, *, snapshot: dict, thread_id: str,
                 phase: str, metadata: dict | None = None, checkpoint_id: str | None = None,
                 attempt: int = 1):
        self.enabled = bool(settings.enabled and settings.api_key and
                            settings.api_key.get_secret_value().strip())
        if not self.enabled:
            return
        self.api_key = settings.api_key.get_secret_value()
        self.project = settings.project
        self.run_id = str(snapshot.get("run_id", "standalone"))
        self.thread_id = thread_id
        self.root_id = uuid4()
        self.attempt_id = uuid4()
        self.root_order = self._segment(self.root_id)
        self.attempt_order = self.root_order + "." + self._segment(self.attempt_id)
        self.metadata = _safe_data({
            **(metadata or {}),
            "business_run_id": self.run_id,
            "thread_id": thread_id,
            "attempt": attempt,
            "checkpoint_id": checkpoint_id,
            "phase": phase,
        })
        self._create(self.root_id, "ticketmind.run", "chain", None, self.root_order,
                     {"phase": phase}, self.metadata)
        self._create(self.attempt_id, "ticketmind.attempt", "chain", self.root_id, self.attempt_order,
                     {"phase": phase}, self.metadata)

    @staticmethod
    def _segment(run_id):
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)

    def _create(self, run_id, name, run_type, parent_id, dotted_order, inputs, metadata):
        _submit(self.api_key, self.project, "create", {
            "name": name, "run_type": run_type, "id": run_id,
            "parent_run_id": parent_id, "trace_id": self.root_id,
            "dotted_order": dotted_order, "start_time": datetime.now(UTC),
            "inputs": _safe_data(inputs), "extra": {"metadata": _safe_data(metadata)},
        })

    def event(self, name: str, *, node: str, inputs: dict | None = None,
              outputs: dict | None = None, error: str | None = None,
              run_type: str = "chain"):
        if not self.enabled:
            return
        try:
            safe_inputs = _safe_data(inputs or {})
            safe_outputs = _safe_data(outputs or {})
            event_id = uuid4()
            self._create(event_id, name, run_type, self.attempt_id,
                         self.attempt_order + "." + self._segment(event_id), safe_inputs,
                         {**self.metadata, "node": node})
            _submit(self.api_key, self.project, "update", {
                "run_id": event_id, "end_time": datetime.now(UTC),
                "outputs": safe_outputs, "error": error,
            })
        except Exception:
            return

    def finish(self, *, outcome: str, error: str | None = None):
        if not self.enabled:
            return
        for run_id in (self.attempt_id, self.root_id):
            _submit(self.api_key, self.project, "update", {
                "run_id": run_id, "end_time": datetime.now(UTC),
                "outputs": {"outcome": outcome}, "error": error,
            })


def trace_for(snapshot, thread_id, phase, *, runner=None, checkpoint_id=None, attempt=1):
    """Project known pure data; never call runner.metadata or touch a database."""
    try:
        from ticketmind.agent.decide import DECISION_PROTOCOL
        from ticketmind.agent.semantic_judge import JUDGE_PROTOCOL
        contract = snapshot.get("runtime_contract") or {}
        models = contract.get("model_config", {})
        docs = models.get("docs", {})
        config = getattr(runner, "config", None)
        metadata = {
            "agent_version": snapshot.get("agent_version") or getattr(config, "agent_version", None),
            "retrieval_mode": snapshot.get("retrieval_mode") or getattr(config, "retrieval_mode", None),
            "corpus_version": snapshot.get("corpus_version"),
            "docs_version": docs.get("docs_version") or getattr(getattr(runner, "docs_store", None), "version", None),
            "docs_catalog_hash": docs.get("catalog_hash"),
            "decision_model": models.get("decision") or getattr(config, "decision_model", None),
            "judge_model": models.get("semantic_judge") or getattr(config, "judge_model", None),
            "decision_protocol": models.get("decision_protocol") or DECISION_PROTOCOL,
            "judge_protocol": models.get("judge_protocol") or JUDGE_PROTOCOL,
        }
        trace = Trace(TraceSettings(), snapshot=snapshot, thread_id=thread_id,
                      phase=phase, metadata=metadata, checkpoint_id=checkpoint_id,
                      attempt=attempt)
        return trace if trace.enabled else None
    except Exception as exc:
        logger.debug("langsmith_trace_setup_failed error=%s", type(exc).__name__)
        return None
