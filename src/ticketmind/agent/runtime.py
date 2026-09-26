from dataclasses import dataclass
import logging
from time import monotonic

from ticketmind.agent.decide import DECISION_PROTOCOL, decide_ticket
from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.retrieve import build_retrieval_query
from ticketmind.agent.schemas import AgentRunInput
from ticketmind.agent.semantic_judge import GuardrailFailure, JUDGE_PROTOCOL, judge_proposal, validate_judgment
from ticketmind.agent.tools import bounded_decision
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
        agent_input = AgentRunInput.model_validate(agent_input)
        started = monotonic()
        partial = {
            "subject": agent_input.subject,
            "messages": agent_input.messages,
            "clarification_rounds": clarification_rounds,
            "tool_calls": [],
        }
        evidence, usage = [], {}
        stage, client = "initialization", None

        def remaining():
            seconds = self.config.processing_timeout_seconds - (monotonic() - started)
            if seconds <= 0:
                raise TimeoutError("处理时间预算已耗尽")
            return min(30.0, seconds)

        def one_decision(state):
            nonlocal stage, evidence
            stage = "source_validation"
            evidence = self.corpus.evidence(state["retrieval_hits"])
            stage = "decision"
            if self.decision_fn:
                result = self.decision_fn(state, remaining(), usage)
            else:
                result = decide_ticket(
                    self.decision_settings,
                    state,
                    timeout=remaining(),
                    usage_callback=lambda value: usage.setdefault("decisions", []).append(value),
                )
            remaining()
            return result

        def judge(state, proposal):
            nonlocal stage
            stage = "semantic_judge"
            record = {
                "attempt": len(usage.setdefault("semantic_judge", [])) + 1,
                "model": self.judge_settings.model,
                "protocol": JUDGE_PROTOCOL,
                "proposal": proposal.model_dump(),
                "status": "failed",
            }
            usage["semantic_judge"].append(record)
            judge_started = monotonic()
            try:
                if self.decision_fn is not None and self.judge_fn is None:
                    raise ValueError("自定义 Decision 适配器必须显式提供 Judge 适配器")
                if self.judge_fn:
                    result = self.judge_fn(state, proposal, remaining(), usage)
                else:
                    result = judge_proposal(
                        self.judge_settings,
                        state,
                        proposal,
                        timeout=remaining(),
                        usage_callback=lambda value: record.update(usage=value),
                    )
                result = validate_judgment(result, proposal)
                remaining()
                record.update(status="passed" if result.passed else "rejected", result=result.model_dump())
                return result
            except Exception as exc:
                record["error"] = "semantic_judge_error"
                raise GuardrailFailure("semantic_judge_error") from exc
            finally:
                record["duration_ms"] = round((monotonic() - judge_started) * 1000)

        def decide(state):
            nonlocal evidence, stage
            stage = "source_validation"
            evidence = self.corpus.evidence(state["retrieval_hits"])
            stage = "decision"
            result, hits = bounded_decision(
                state,
                decide=one_decision,
                judge=judge,
                corpus=self.corpus,
                config=self.config,
                remaining=remaining,
                audit=partial["tool_calls"],
                search_fn=search,
            )
            evidence = self.corpus.evidence(hits)
            return result

        def search(query, record):
            nonlocal stage
            stage = "retrieval"
            return retrieve_cases(
                query,
                client=client,
                embeddings=embeddings,
                corpus=self.corpus,
                config=self.config,
                timeout=lambda: min(self.milvus.timeout_seconds, remaining()),
                record=record,
                model=self.qwen.embedding_model,
            )

        def initial_retrieval(state, record):
            query = build_retrieval_query(subject=state["subject"], messages=state["messages"])
            return {"retrieval_query": query, "retrieval_hits": search(query, record)}

        try:
            if self.qwen.embedding_model != "text-embedding-v4":
                raise ValueError("当前集合只支持 text-embedding-v4")
            client = (self.milvus_factory or build_milvus_client)(
                self.milvus.model_copy(
                    update={"timeout_seconds": min(self.milvus.timeout_seconds, remaining())}
                )
            )
            embeddings = (
                None
                if self.config.retrieval_mode == "bm25"
                else (
                    self.embedding_factory(remaining)
                    if self.embedding_factory
                    else build_budgeted_embeddings(
                        self.qwen,
                        remaining,
                        lambda value: usage.setdefault("embeddings", []).append(value),
                    )
                )
            )
            graph = build_ticket_graph(
                embeddings=embeddings,
                client=client,
                top_k=self.config.retrieval_top_k,
                timeout=min(self.milvus.timeout_seconds, remaining()),
                decision_fn=decide,
                retrieval_audit=partial["tool_calls"],
                retrieval_fn=initial_retrieval,
                retrieval_timeout_fn=lambda: min(self.milvus.timeout_seconds, remaining()),
            )
            for update in graph.stream(partial, stream_mode="updates"):
                for node, values in update.items():
                    partial.update(values)
                    if node == "retrieve":
                        stage = "decision"
                remaining()
            return RunOutput(partial, evidence, usage)
        except Exception as exc:
            if isinstance(exc, GuardrailFailure):
                stage = "semantic_guardrail"
                usage["guardrail_failure"] = {"code": exc.code, "protocol": JUDGE_PROTOCOL}
            raise RunFailure(stage, partial, evidence, usage) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    # RunOutput and RunFailure retain this same usage dict. Cleanup
                    # must not discard a proposal, evidence, or the original cause.
                    diagnostic = {"resource": "milvus", "code": "milvus_close_failed",
                                  "error_type": type(exc).__name__}
                    usage.setdefault("cleanup_errors", []).append(diagnostic)
                    logger.warning("agent_cleanup_failed resource=milvus error=%s", type(exc).__name__)


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
