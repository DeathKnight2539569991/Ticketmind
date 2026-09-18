from dataclasses import dataclass
from time import monotonic

from ticketmind.agent.decide import decide_ticket, DECISION_PROTOCOL
from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.understand import understand_ticket
from ticketmind.agent.tools import bounded_decision
from ticketmind.agent.semantic_judge import JUDGE_PROTOCOL, GuardrailFailure, judge_proposal, validate_judgment
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.repository import KnowledgeStore
from ticketmind.retrieval.embeddings import build_embedding_client
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.service import retrieve_cases
from ticketmind.retrieval.versioned_collection import selected_collection
from ticketmind.agent.retrieve import build_retrieval_query


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
    """One synchronous run; owns external resources, never a database Session."""
    def __init__(self, qwen: QwenSettings, milvus: MilvusSettings, config: ProcessingSettings,
                 *, understanding_fn=None, embedding_factory=None, decision_fn=None, milvus_factory=None,
                 session_factory=None, corpus=None, judge_fn=None):
        self.qwen, self.milvus, self.config = qwen, milvus, config
        # Revalidate model_copy updates too, before any provider call.
        ProcessingSettings.model_validate(config.model_dump())
        self.decision_settings = qwen.model_copy(update={"model": config.decision_model})
        self.judge_settings = qwen.model_copy(update={"model": config.judge_model})
        self.judge_fn = judge_fn
        if session_factory is None:
            from ticketmind.db.session import SesstionLocal
            session_factory = SesstionLocal
        # Frozen corpus injection is reserved for explicit evaluation adapters.
        self.corpus = corpus if corpus is not None else KnowledgeStore(session_factory, config.knowledge_dataset)
        self.understanding_fn = understanding_fn
        self.embedding_factory = embedding_factory
        self.decision_fn = decision_fn
        self.milvus_factory = milvus_factory

    @property
    def metadata(self):
        return {"agent_version": self.config.agent_version, "corpus_version": self.corpus.version,
                "retrieval_mode": self.config.retrieval_mode,
                "model_config": {"understanding": self.qwen.model, "decision": self.decision_settings.model,
                                 "decision_protocol": DECISION_PROTOCOL,
                                 "semantic_judge": self.judge_settings.model, "judge_protocol": JUDGE_PROTOCOL,
                                 "max_guardrail_retries": 1,
                                 "embedding": self.qwen.embedding_model, "dimension": 1024,
                                 "top_k": self.config.retrieval_top_k,
                                 "candidate_k": self.config.retrieval_candidate_k,
                                 "rrf_k": self.config.retrieval_rrf_k,
                                 "collection": self.corpus.dataset().collection_name if isinstance(self.corpus, KnowledgeStore)
                                               else selected_collection(self.corpus, self.config.retrieval_mode),
                                 "limits": self.config.model_dump(include={"max_search_rounds", "max_case_details", "max_agent_steps", "max_clarification_rounds", "processing_timeout_seconds"})}}

    def __call__(self, snapshot: dict) -> RunOutput:
        started = monotonic()
        partial = {"subject": snapshot["subject"], "body": snapshot["body"], "tool_calls": []}
        evidence, usage = [], {}
        stage, client = "initialization", None

        def remaining():
            seconds = self.config.processing_timeout_seconds - (monotonic() - started)
            if seconds <= 0:
                raise TimeoutError("处理时间预算已耗尽")
            return min(30.0, seconds)

        def understand(**kwargs):
            nonlocal stage
            stage = "understanding"
            if self.understanding_fn:
                result = self.understanding_fn(**kwargs)
                remaining()
                return result
            return understand_ticket(**kwargs, timeout=remaining(),
                                      usage_callback=lambda value: usage.update(understanding=value))

        def one_decision(state):
            nonlocal stage, evidence
            stage = "source_validation"
            evidence = self.corpus.evidence(state["retrieval_hits"])
            stage = "decision"
            if self.decision_fn:
                result = self.decision_fn(state, remaining(), usage)
            else:
                result = decide_ticket(self.decision_settings, state, timeout=remaining(),
                                       usage_callback=lambda value: usage.setdefault("decisions", []).append(value))
            remaining()
            return result

        def judge(state, proposal):
            nonlocal stage
            stage = "semantic_judge"
            record = {"attempt": len(usage.setdefault("semantic_judge", [])) + 1,
                      "model": self.judge_settings.model, "protocol": JUDGE_PROTOCOL,
                      "proposal": proposal.model_dump(), "status": "failed"}
            usage["semantic_judge"].append(record)
            judge_started = monotonic()
            try:
                if self.decision_fn is not None and self.judge_fn is None:
                    # Cached/evaluation adapters have their own paid-call ledger.
                    # Never silently add provider calls outside that authorization.
                    raise ValueError("自定义 Decision 适配器必须显式提供 Judge 适配器")
                if self.judge_fn:
                    result = self.judge_fn(state, proposal, remaining(), usage)
                else:
                    result = judge_proposal(self.judge_settings, state, proposal, timeout=remaining(),
                                            usage_callback=lambda value: record.update(usage=value))
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
            state = {**state, **{key: snapshot.get(key, default) for key, default in (
                ("clarification_rounds", 0), ("asked_questions", []), ("approved_clarifications", []))}}
            evidence = self.corpus.evidence(state["retrieval_hits"])
            stage = "decision"
            result, hits = bounded_decision(state, decide=one_decision, judge=judge, embeddings=embeddings,
                client=client, corpus=self.corpus, config=self.config, remaining=remaining, audit=partial["tool_calls"],
                retrieval_timeout=lambda: min(self.milvus.timeout_seconds, remaining()), search_fn=search)
            evidence = self.corpus.evidence(hits)
            return result

        def search(query, record):
            nonlocal stage
            stage = "retrieval"
            return retrieve_cases(query, client=client, embeddings=embeddings, corpus=self.corpus,
                config=self.config, timeout=lambda: min(self.milvus.timeout_seconds, remaining()),
                record=record, model=self.qwen.embedding_model)

        def initial_retrieval(state, record):
            query = build_retrieval_query(subject=state["subject"], body=state["body"])
            return {"retrieval_query": query, "retrieval_hits": search(query, record)}

        try:
            if self.qwen.embedding_model != "text-embedding-v4":
                raise ValueError("当前集合只支持 text-embedding-v4")
            client = (self.milvus_factory or build_milvus_client)(self.milvus.model_copy(
                update={"timeout_seconds": min(self.milvus.timeout_seconds, remaining())}))
            # The production adapter gives each query a remaining-budget timeout, with no retries.
            embeddings = None if self.config.retrieval_mode == "bm25" else (
                self.embedding_factory(remaining) if self.embedding_factory else build_budgeted_embeddings(
                    self.qwen, remaining, lambda value: usage.setdefault("embeddings", []).append(value)))
            graph = build_ticket_graph(self.qwen, embeddings=embeddings, client=client,
                                       top_k=self.config.retrieval_top_k,
                                       timeout=min(self.milvus.timeout_seconds, remaining()),
                                       understanding_fn=understand, decision_fn=decide,
                                       retrieval_audit=partial["tool_calls"],
                                       retrieval_fn=initial_retrieval,
                                       retrieval_timeout_fn=lambda: min(self.milvus.timeout_seconds, remaining()))
            for update in graph.stream(partial, stream_mode="updates"):
                for node, values in update.items():
                    partial.update(values)
                    stage = "retrieval" if node == "understand" else "decision"
                remaining()
            return RunOutput(partial, evidence, usage)
        except Exception as exc:
            if isinstance(exc, GuardrailFailure):
                stage = "semantic_guardrail"
                usage["guardrail_failure"] = {"code": exc.code, "protocol": JUDGE_PROTOCOL}
            raise RunFailure(stage, partial, evidence, usage) from exc
        finally:
            if client is not None:
                client.close()


def build_budgeted_embeddings(settings, remaining, usage_callback=None):
    """Use the existing embedding adapter with a bounded, single-send SDK transport."""
    from ticketmind.retrieval.transport import SingleRequestSession
    embeddings = build_embedding_client(settings)
    sdk_client = embeddings.client

    class BudgetedClient:
        @staticmethod
        def call(**kwargs):
            with SingleRequestSession() as session:
                response = sdk_client.call(**kwargs, api_key=settings.api_key.get_secret_value(),
                                          dimension=1024, request_timeout=remaining(), session=session)
                if usage_callback is not None:
                    usage_callback(response.get("usage"))
                return response

    embeddings.client = BudgetedClient
    return embeddings
