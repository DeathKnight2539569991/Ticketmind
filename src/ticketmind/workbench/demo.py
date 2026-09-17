"""Explicit synthetic runner for isolated UI demos. Never enabled by production API."""
from ticketmind.agent.proposals import proposal_adapter
from ticketmind.agent.runtime import RunOutput
from ticketmind.agent.schemas import TicketUnderstanding
from ticketmind.core.config import ProcessingSettings
from ticketmind.knowledge.corpus import build_case_text
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.dense import RetrievalHit


class DemoRunner:
    demo_mode = True

    def __init__(self):
        self.corpus = load_sources(ProcessingSettings().corpus_path)
        self.metadata = {"agent_version": "m5-synthetic-demo", "corpus_version": self.corpus.version,
                         "retrieval_mode": "synthetic", "model_config": {"synthetic_test_double": True}}

    def __call__(self, snapshot):
        subject = snapshot["subject"]
        if "故障" in subject:
            raise RuntimeError("synthetic demo failure")
        action = "escalate" if "转人工" in subject else (
            "ask_clarification" if "补问" in subject and snapshot.get("clarification_rounds", 0) == 0 else "propose_resolution")
        source = self.corpus.cases["SYN-HIST-V2-007"]
        reply = {"escalate": "建议由人工核查当前环境与权限。", "ask_clarification": "请补充是否启用本机代理及代理类型。",
                 "propose_resolution": "请先核对本机代理类型及当前客户端支持的代理配置，再复测连接。"}[action]
        proposal = proposal_adapter.validate_python(dict(next_step=action, reason="隔离演示：按标题中的演示类型选择固定路径，非模型输出。",
            reply=reply, evidence_ids=[source.source_id], questions=["是否启用本机代理？代理类型是什么？"] if action == "ask_clarification" else []))
        return RunOutput({"understanding": TicketUnderstanding(summary="合成演示输入：" + subject, error_codes=[], environment=[]),
                          "proposal": proposal}, self.corpus.evidence([RetrievalHit(source_id=source.source_id,
                          text=build_case_text(source), score=0.5)]), {"synthetic_test_double": True})
