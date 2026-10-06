from dataclasses import dataclass
from threading import Lock
import logging
from time import monotonic
import math
import hashlib
from inspect import signature
from pydantic import ValidationError

from ticketmind.agent.state import copy_data, durable_state, typed_state

from ticketmind.agent.decide import DECISION_PROTOCOL, decide_ticket
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.schemas import AgentRunInput
from ticketmind.agent.semantic_judge import GuardrailFailure, JUDGE_PROTOCOL, judge_proposal, validate_judgment
from ticketmind.agent.tools import (decision_turn, execute_decision_tool,
                                   final_candidate, judge_candidate, repair_candidate,
                                   normalize_decision, normalize_proposal)
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.repository import KnowledgeStore
from ticketmind.retrieval.embeddings import build_embedding_client
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.service import retrieve_cases
from ticketmind.retrieval.versioned_collection import selected_collection

logger = logging.getLogger(__name__)


@dataclass
class RunOutput:
    state: dict
    evidence: list[dict]
    usage: dict


class RunFailure(Exception):
    def __init__(self, stage: str, partial: dict, evidence: list[dict], usage: dict):
        self.stage, self.partial, self.evidence, self.usage = stage, partial, evidence, usage
        super().__init__(f"{stage} 阶段失败")


def fatal_failure(failure: RunFailure) -> bool:
    """Fail closed for deterministic/protocol/authentication failures.

    Judge and repair already wrap every failure as GuardrailFailure. A timeout
    is retryable only while the original total active budget remains available.
    """
    cause, seen = failure.__cause__, set()
    deterministic_retrieval = {"embedding_model_mismatch", "corpus_version_mismatch",
        "collection_version_mismatch", "collection_schema_mismatch", "collection_analyzer_mismatch",
        "collection_index_mismatch", "collection_data_incomplete", "knowledge_index_hash_missing",
        "retrieval_empty_query", "dense_invalid_response", "bm25_invalid_response"}
    while cause is not None and id(cause) not in seen and len(seen) < 16:
        seen.add(id(cause))
        if isinstance(cause, (GuardrailFailure, ValidationError, ValueError, PermissionError)):
            return True
        if getattr(cause, "code", None) in deterministic_retrieval:
            return True
        status = getattr(cause, "status_code", None) or getattr(getattr(cause, "response", None), "status_code", None)
        if status in (401, 403):
            return True
        if isinstance(cause, TimeoutError) and str(cause) == "处理时间预算已耗尽":
            return True
        cause = cause.__cause__
    return False


class AgentRunner:
    """One synchronous run over a validated AgentRunInput; owns external resources, never a database Session."""

    def __init__(
        self,
        qwen: QwenSettings,
        milvus: MilvusSettings,
        config: ProcessingSettings,
        *,
        embedding_factory=None,
        decision_fn=None,
        milvus_factory=None,
        session_factory=None,
        corpus=None,
        judge_fn=None,
    ):
        self.qwen, self.milvus, self.config = qwen, milvus, config
        self._workflow_lock = Lock()
        ProcessingSettings.model_validate(config.model_dump())
        self.decision_settings = qwen.model_copy(update={"model": config.decision_model})
        self.judge_settings = qwen.model_copy(update={"model": config.judge_model})
        self.judge_fn = judge_fn
        if session_factory is None:
            from ticketmind.db.session import SesstionLocal

            session_factory = SesstionLocal
        self.corpus = corpus if corpus is not None else KnowledgeStore(session_factory, config.knowledge_dataset)
        self.embedding_factory = embedding_factory
        self.decision_fn = decision_fn
        self.milvus_factory = milvus_factory

    @property
    def metadata(self):
        dataset = self.corpus.dataset() if isinstance(self.corpus, KnowledgeStore) else None
        collections = ({"dense": dataset.collection_name,
                        "bm25": dataset.bm25_collection_name or dataset.collection_name}
                       if dataset is not None else None)
        return {
            "agent_version": self.config.agent_version,
            "corpus_version": self.corpus.version,
            "retrieval_mode": self.config.retrieval_mode,
            "model_config": {
                "decision": self.decision_settings.model,
                "decision_protocol": DECISION_PROTOCOL,
                "semantic_judge": self.judge_settings.model,
                "judge_protocol": JUDGE_PROTOCOL,
                "max_guardrail_retries": 1,
                "embedding": self.qwen.embedding_model,
                "dimension": 1024,
                "top_k": self.config.retrieval_top_k,
                "candidate_k": self.config.retrieval_candidate_k,
                "rrf_k": self.config.retrieval_rrf_k,
                "collection": (
                    collections["bm25" if self.config.retrieval_mode == "bm25" else "dense"]
                    if collections is not None
                    else selected_collection(self.corpus, self.config.retrieval_mode)
                ),
                "collections": collections,
                "limits": self.config.model_dump(
                    include={
                        "max_search_rounds",
                        "max_case_details",
                        "max_agent_steps",
                        "max_clarification_rounds",
                        "processing_timeout_seconds",
                    }
                ),
            },
        }

    def __call__(self, agent_input: AgentRunInput, *, clarification_rounds: int = 0) -> RunOutput:
        from ticketmind.agent.review import ReviewWorkflow
        with self._workflow_lock:
            if not hasattr(self, "_workflow"):
                self._workflow = ReviewWorkflow(None)
        return self._workflow.compute(AgentRunInput.model_validate(agent_input), self,
                                      clarification_rounds=clarification_rounds)

    def new_execution(self, agent_input=None, clarification_rounds=0, *, data=None):
        return AgentExecution(self, agent_input, clarification_rounds, data=data)

    @property
    def recovery_contract(self):
        return copy_data({**self.metadata, "knowledge_dataset": self.config.knowledge_dataset,
            "model_call_timeout_seconds": 30.0,
            "retrieval_timeout_seconds": self.milvus.timeout_seconds,
            "endpoint_identity": hashlib.sha256((self.milvus.uri + "\n" + self.qwen.workspace_id).encode()).hexdigest()})


class ExecutionBudget:
    """One active compute scope, with data-only accumulated time between scopes.

    Waiting/offline time is excluded by ending a scope. Uncheckpointed work after
    a hard process crash is not measurable here; completed scopes retain elapsed time.
    """

    def __init__(self, limit_seconds, *, elapsed_seconds=0.0, clock=None):
        if (type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds)
                or elapsed_seconds < 0):
            raise ValueError("累计计算耗时必须是非负有限数值")
        if type(limit_seconds) not in (int, float) or not math.isfinite(limit_seconds) or limit_seconds <= 0:
            raise ValueError("计算预算必须是正有限数值")
        self.limit_seconds = limit_seconds
        self.previous_elapsed = elapsed_seconds
        self.clock = clock or monotonic
        self.started = self.clock()
        self.stopped = None

    @property
    def elapsed_seconds(self):
        end = self.clock() if self.stopped is None else self.stopped
        return self.previous_elapsed + max(0.0, end - self.started)

    def remaining(self):
        seconds = self.limit_seconds - self.elapsed_seconds
        if seconds <= 0:
            raise TimeoutError("处理时间预算已耗尽")
        return min(30.0, seconds)

    def stop(self):
        if self.stopped is None:
            self.stopped = self.clock()
        return self.elapsed_seconds


class AgentExecution:
    """Request-scoped capabilities/resources; data snapshots never contain this object.

    Durable nodes initialize from JSON data and own one active compute scope.
    Capability methods never depend on __call__ closures or a database Session.
    """

    def __init__(self, runner, agent_input=None, clarification_rounds=0, *, data=None):
        self.runner = runner
        self.data = durable_state(data) if data is not None else durable_state({
            "subject": agent_input.subject, "messages": agent_input.messages,
            "clarification_rounds": clarification_rounds, "tool_calls": [],
            "usage": {}, "evidence": [], "compute_elapsed_seconds": 0.0})
        self.budget = ExecutionBudget(runner.config.processing_timeout_seconds,
                                      elapsed_seconds=self.data.get("compute_elapsed_seconds", 0.0))
        self.partial = typed_state(self.data)
        self.usage = copy_data(self.data.get("usage", {}))
        self.evidence = copy_data(self.data.get("evidence", []))
        self.stage = "initialization"
        self.client, self.embeddings = None, None

    def capture(self, state=None):
        if state is not None:
            # Replace values with independent snapshots rather than merging lists.
            self.partial.update(typed_state(durable_state(state)))
        estimated = self.partial.get("compute_estimated_seconds", 0.0)
        if estimated:
            self.usage["execution_budget"] = {"observed_elapsed_seconds": self.budget.elapsed_seconds - estimated,
                "estimated_elapsed_seconds": estimated, "total_elapsed_seconds": self.budget.elapsed_seconds,
                "estimates": copy_data(self.partial.get("compute_estimates", []))}
        self.data = durable_state({**self.partial, "usage": self.usage, "evidence": self.evidence,
                                   "compute_observed_seconds": self.budget.elapsed_seconds - estimated,
                                   "compute_elapsed_seconds": self.budget.elapsed_seconds})

    def retrieval_timeout(self):
        return min(self.runner.milvus.timeout_seconds, self.budget.remaining())

    def record_decision_usage(self, value):
        self.usage.setdefault("decisions", []).append(copy_data(value))

    def record_embedding_usage(self, value):
        self.usage.setdefault("embeddings", []).append(copy_data(value))

    def initialize(self):
        self.stage = "initialization"
        runner = self.runner
        if runner.qwen.embedding_model != "text-embedding-v4":
            raise ValueError("当前集合只支持 text-embedding-v4")
        self.client = (runner.milvus_factory or build_milvus_client)(
            runner.milvus.model_copy(update={"timeout_seconds": self.retrieval_timeout()}))
        if runner.config.retrieval_mode != "bm25":
            if runner.embedding_factory:
                factory = runner.embedding_factory
                # Optional phase-aware factories preserve acceptance accounting
                # across resource scopes; existing one-argument factories remain valid.
                try:
                    phase_aware = "retrieval_phase" in signature(factory).parameters
                except (TypeError, ValueError):
                    phase_aware = False
                kwargs = ({"retrieval_phase": "research" if "retrieval_query" in self.partial else "initial"}
                          if phase_aware else {})
                self.embeddings = factory(self.budget.remaining, **kwargs)
            else:
                self.embeddings = build_budgeted_embeddings(runner.qwen, self.budget.remaining,
                                                           self.record_embedding_usage)

    def search_cases(self, query, record):
        if self.client is None:
            self.initialize()
        self.stage = "retrieval"
        runner = self.runner
        return retrieve_cases(query, client=self.client, embeddings=self.embeddings,
                              corpus=runner.corpus, config=runner.config,
                              timeout=self.retrieval_timeout, record=record,
                              model=runner.qwen.embedding_model)

    def retrieve(self, state, record):
        query = build_retrieval_query(subject=state["subject"], messages=state["messages"])
        return {"retrieval_query": query, "retrieval_hits": self.search_cases(query, record)}

    def get_case_detail(self, source_id):
        return self.runner.corpus.get_case_detail(source_id)

    def decide(self, state):
        self.capture(state)
        self.stage = "source_validation"
        self.evidence = self.runner.corpus.evidence(state["retrieval_hits"])
        self.stage = "decision"
        if self.runner.decision_fn:
            result = self.runner.decision_fn(state, self.budget.remaining(), self.usage)
        else:
            result = decide_ticket(self.runner.decision_settings, state, timeout=self.budget.remaining(),
                                   usage_callback=self.record_decision_usage)
        self.budget.remaining()
        return result

    def judge(self, state, proposal):
        self.stage = "semantic_judge"
        runner = self.runner
        record = {"attempt": len(self.usage.setdefault("semantic_judge", [])) + 1,
                  "model": runner.judge_settings.model, "protocol": JUDGE_PROTOCOL,
                  "proposal": proposal.model_dump(), "status": "failed"}
        self.usage["semantic_judge"].append(record)
        started = monotonic()
        try:
            if runner.decision_fn is not None and runner.judge_fn is None:
                raise ValueError("自定义 Decision 适配器必须显式提供 Judge 适配器")
            if runner.judge_fn:
                result = runner.judge_fn(state, proposal, self.budget.remaining(), self.usage)
            else:
                result = judge_proposal(runner.judge_settings, state, proposal,
                                        timeout=self.budget.remaining(),
                                        usage_callback=lambda value: record.update(usage=value))
            result = validate_judgment(result, proposal)
            self.budget.remaining()
            record.update(status="passed" if result.passed else "rejected", result=result.model_dump())
            return result
        except Exception as exc:
            record["error"] = "semantic_judge_error"
            raise GuardrailFailure("semantic_judge_error") from exc
        finally:
            record["duration_ms"] = round((monotonic() - started) * 1000)

    def repair(self, state):
        # The workflow supplies final-only feedback; its proposal adapter remains the
        # enforcement boundary even for custom Decision adapters.
        return self.decide(state)

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception as exc:
                self.usage.setdefault("cleanup_errors", []).append(
                    {"resource": "milvus", "code": "milvus_close_failed", "error_type": type(exc).__name__})
                logger.warning("agent_cleanup_failed resource=milvus error=%s", type(exc).__name__)
        self.budget.stop()

    def run_node(self, node):
        """Execute one durable boundary; close resources before exporting its data."""
        failure = None
        try:
            self.budget.remaining()
            if node == "retrieve":
                query = build_retrieval_query(subject=self.partial["subject"], messages=self.partial["messages"])
                record = {"tool": "search_cases", "parameters": {"query": query},
                          "reason": "首次检索客户明确提供的工单事实", "status": "failed",
                          "result_source_ids": []}
                self.partial["tool_calls"].append(record)
                started = monotonic()
                try:
                    update = self.retrieve(self.partial, record)
                    record["parameters"] = {"query": update["retrieval_query"]}
                    record.update(status="succeeded",
                                  result_source_ids=[hit.source_id for hit in update["retrieval_hits"]],
                                  result_summary=f"返回 {len(update['retrieval_hits'])} 条候选")
                    self.partial.update(update)
                    self.partial.update(agent_steps=1, search_rounds=1, repair_attempt=0,
                                        case_details={}, detail_ids=[],
                                        seen_queries=[update["retrieval_query"].strip().casefold()],
                                        execution_limits=self.runner.config.model_dump(include={
                                            "max_search_rounds", "max_case_details", "max_agent_steps",
                                            "max_clarification_rounds"}))
                    self.stage = "source_validation"
                    self.evidence = self.runner.corpus.evidence(update["retrieval_hits"])
                except Exception as exc:
                    record["error"] = "tool_execution_failed"
                    if hasattr(exc, "code"):
                        record["retrieval_error"] = exc.code
                    raise
                finally:
                    record["duration_ms"] = round((monotonic() - started) * 1000)
            elif node == "decision":
                self.stage = "decision"
                decision_turn(self.partial, decide=self.decide, config=self.runner.config,
                              remaining=self.budget.remaining)
            elif node in ("search_cases", "get_case_detail"):
                decision = normalize_decision(self.partial["decision_result"])
                if decision.next_step != node:
                    raise ValueError("工具节点与 Decision 不一致")
                self.stage = "retrieval" if node == "search_cases" else "case_detail"
                rejected = execute_decision_tool(self.partial, decision, corpus=self.runner.corpus,
                    config=self.runner.config, remaining=self.budget.remaining,
                    search_fn=self.search_cases, detail_fn=self.get_case_detail)
                if rejected is not None:
                    self.partial["candidate_proposal"] = rejected.model_dump(mode="json")
                self.stage = "source_validation"
                self.evidence = self.runner.corpus.evidence(self.partial["retrieval_hits"])
            elif node == "judge":
                self.stage = "decision"
                proposal = (normalize_proposal(self.partial["candidate_proposal"])
                            if "candidate_proposal" in self.partial else
                            final_candidate(self.partial, normalize_decision(self.partial["decision_result"]),
                                            self.runner.config))
                proposal, result = judge_candidate(self.partial, proposal, judge=self.judge,
                                                   remaining=self.budget.remaining)
                self.evidence = self.runner.corpus.evidence(self.partial["retrieval_hits"])
                if result.passed:
                    self.partial["proposal"] = proposal
            elif node == "repair":
                self.stage = "decision"
                repair_candidate(self.partial, repair=self.repair, config=self.runner.config,
                                 remaining=self.budget.remaining)
            else:
                raise ValueError("未知 Agent node")
            self.budget.remaining()
        except Exception as exc:
            failure = exc
            self.partial.pop("proposal", None)
            if isinstance(exc, GuardrailFailure):
                self.stage = "semantic_guardrail"
                self.usage["guardrail_failure"] = {"code": exc.code, "protocol": JUDGE_PROTOCOL}
        finally:
            self.close()
            if failure is not None:
                self.usage["execution_budget"] = {"observed_elapsed_seconds": self.budget.elapsed_seconds - self.partial.get("compute_estimated_seconds", 0),
                    "estimated_elapsed_seconds": self.partial.get("compute_estimated_seconds", 0),
                    "total_elapsed_seconds": self.budget.elapsed_seconds}
            self.capture()
        if failure is not None:
            raise RunFailure(self.stage, typed_state(self.data), copy_data(self.evidence),
                             copy_data(self.usage)) from failure
        return copy_data(self.data)


def build_budgeted_embeddings(settings, remaining, usage_callback=None):
    """Use the existing embedding adapter with a bounded, single-send SDK transport."""
    from ticketmind.retrieval.transport import SingleRequestSession

    embeddings = build_embedding_client(settings)
    sdk_client = embeddings.client

    class BudgetedClient:
        @staticmethod
        def call(**kwargs):
            with SingleRequestSession() as session:
                response = sdk_client.call(
                    **kwargs,
                    api_key=settings.api_key.get_secret_value(),
                    dimension=1024,
                    request_timeout=remaining(),
                    session=session,
                )
                if usage_callback is not None:
                    usage_callback(response.get("usage"))
                return response

    embeddings.client = BudgetedClient
    return embeddings
