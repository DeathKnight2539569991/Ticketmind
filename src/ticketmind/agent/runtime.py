from dataclasses import dataclass
from time import monotonic

from ticketmind.agent.decide import decide_ticket, DECISION_PROTOCOL
from ticketmind.agent.graph import build_ticket_graph
from ticketmind.agent.understand import understand_ticket
from ticketmind.agent.tools import bounded_decision
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.embeddings import build_embedding_client
from ticketmind.retrieval.milvus_client import build_milvus_client


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
                 *, understanding_fn=None, embedding_factory=None, decision_fn=None, milvus_factory=None):
        self.qwen, self.milvus, self.config = qwen, milvus, config
        self.corpus = load_sources(config.corpus_path)
        self.understanding_fn = understanding_fn
        self.embedding_factory = embedding_factory
        self.decision_fn = decision_fn
        self.milvus_factory = milvus_factory

    @property
    def metadata(self):
        return {"agent_version": self.config.agent_version, "corpus_version": self.corpus.version,
                "retrieval_mode": self.config.retrieval_mode,
                "model_config": {"understanding": self.qwen.model, "decision": self.qwen.model,
                                 "decision_protocol": DECISION_PROTOCOL,
                                 "embedding": self.qwen.embedding_model, "dimension": 1024,
                                 "top_k": self.config.retrieval_top_k,
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
                result = decide_ticket(self.qwen, state, timeout=remaining(),
                                       usage_callback=lambda value: usage.setdefault("decisions", []).append(value))
            remaining()
            return result

        def decide(state):
            nonlocal evidence, stage
            stage = "source_validation"
            state = {**state, **{key: snapshot.get(key, default) for key, default in (
                ("clarification_rounds", 0), ("asked_questions", []), ("approved_clarifications", []))}}
            evidence = self.corpus.evidence(state["retrieval_hits"])
            stage = "decision"
            result, hits = bounded_decision(state, decide=one_decision, embeddings=embeddings,
                client=client, corpus=self.corpus, config=self.config, remaining=remaining, audit=partial["tool_calls"],
                retrieval_timeout=lambda: min(self.milvus.timeout_seconds, remaining()))
            evidence = self.corpus.evidence(hits)
            return result

        try:
            if self.qwen.embedding_model != "text-embedding-v4":
                raise ValueError("当前集合只支持 text-embedding-v4")
            client = (self.milvus_factory or build_milvus_client)(self.milvus.model_copy(
                update={"timeout_seconds": min(self.milvus.timeout_seconds, remaining())}))
            # The production adapter gives each query a remaining-budget timeout, with no retries.
            embeddings = self.embedding_factory(remaining) if self.embedding_factory else build_budgeted_embeddings(
                self.qwen, remaining, lambda value: usage.setdefault("embeddings", []).append(value))
            graph = build_ticket_graph(self.qwen, embeddings=embeddings, client=client,
                                       top_k=self.config.retrieval_top_k,
                                       timeout=min(self.milvus.timeout_seconds, remaining()),
                                       understanding_fn=understand, decision_fn=decide,
                                       retrieval_audit=partial["tool_calls"],
                                       retrieval_timeout_fn=lambda: min(self.milvus.timeout_seconds, remaining()))
            for update in graph.stream(partial, stream_mode="updates"):
                for node, values in update.items():
                    partial.update(values)
                    stage = "retrieval" if node == "understand" else "decision"
                remaining()
            return RunOutput(partial, evidence, usage)
        except Exception as exc:
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
