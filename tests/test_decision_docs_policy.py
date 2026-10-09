"""Frozen scenarios exercise Docs routing with controlled model/retrieval replies.

These offline tests verify contracts and evidence delivery, not live model tool
selection. Reference replies and negative examples stay in tests, never prompts.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from docs_fakes import FakeDocStore
from ticketmind.agent import decide, runtime
from ticketmind.agent.proposals import decision_adapter
from ticketmind.agent.schemas import AgentMessage, AgentRunInput
from ticketmind.core.config import MilvusSettings, ProcessingSettings, QwenSettings
from ticketmind.documents.markdown import parse_markdown
from ticketmind.knowledge.sources import load_sources
from ticketmind.retrieval.dense import RetrievalHit
from ticketmind.retrieval.schemas import DocEvidenceHit


DATA = Path(__file__).resolve().parents[1] / "data/synthetic/phase7_v1"
ROWS = {row["case_id"]: row for row in map(json.loads,
        (DATA / "evaluation_cases.jsonl").read_text(encoding="utf-8").splitlines())}
# Test-only reference answers, supported by the selected document section.
REGRESSIONS = (
    ("002", "import_schema", 1, "SYN-P7-CASE-006",
     "显式映射和样例对应已确认，可核对后继续预览，不因来源别名退回文件。",
     ("显式映射", "继续预览"), "直接尝试提交"),
    ("013", "import_commit", 1, "SYN-P7-CASE-031",
     "正式提交前核对 append 模式、目标项目和新记录范围；只追加通过校验的新记录，不保证全成功。",
     ("模式", "目标项目", "新记录范围", "不保证全成功"), "预览通过就保证全部写入"),
    ("014", "import_commit", 2, "SYN-P7-CASE-041",
     "按逐行报告只读核对失败行的 record_id 与目标项目既有记录；成功行与失败行分开，不自动重试写入，重试风险须人工确认。",
     ("逐行报告", "目标项目", "既有记录", "不自动重试"), "改用 upsert 模式"),
    ("026", "export_jobs", 1, "SYN-P7-CASE-090",
     "链接失效不同于生成失败。先只读核对作业元信息，重新生成前确认项目和字段权限、过滤快照及时间范围，不保证原文件恢复。",
     ("元信息", "字段权限", "过滤快照", "时间范围"), "原始数据记录完好"),
    ("043", "access_safety", 0, "SYN-P7-CASE-046",
     "viewer 在获授项目内只读查看；editor 可在获授项目提出编辑；admin 管理授权，但不自动拥有所有项目或敏感字段。",
     ("viewer", "editor", "admin", "获授项目", "敏感字段"), "知识库尚未包含角色定义"),
)


def reference_quality_issues(proposal, required, unsupported, doc_source):
    """Literal rubric for authored fixtures only, not arbitrary model semantics."""
    issues = []
    if proposal.next_step != "propose_resolution":
        issues.append("wrong_action")
    if proposal.evidence_ids != [doc_source]:
        issues.append("wrong_evidence")
    if not all(facet in proposal.reply for facet in required):
        issues.append("missing_diagnostic_fact")
    if unsupported in proposal.reply:
        issues.append("unsupported_product_claim")
    return issues


def document_hit(doc_id, section_index):
    document = parse_markdown(doc_id, (DATA / "docs" / f"{doc_id}.md").read_text(encoding="utf-8"))
    chunk = document.chunks[section_index]
    return DocEvidenceHit(source_id="docs:" + chunk.chunk_id, doc_id=doc_id, chunk_id=chunk.chunk_id,
        title=document.title, section=chunk.section, text=chunk.text, score=1.,
        docs_version="synthetic-phase7-docs-v1", content_hash=chunk.content_hash,
        synthetic=True, mode="bm25", rank=1)


@pytest.mark.parametrize("regression", REGRESSIONS, ids=lambda row: "TEST-" + row[0])
def test_docs_rule_reaches_final_proposal_without_case_substitution(monkeypatch, regression):
    number, doc_id, section, case_id, reply, required, unsupported = regression
    row = ROWS["SYN-P7-TEST-" + number]
    corpus = load_sources(DATA / "historical_cases.jsonl")
    from ticketmind.knowledge.corpus import build_case_text
    case = RetrievalHit(source_id=case_id, text=build_case_text(corpus.cases[case_id]), score=1.)
    doc = document_hit(doc_id, section)
    captured = []
    searches = []
    monkeypatch.setattr(runtime, "retrieve_cases", lambda *args, **kwargs: [case])

    def docs_search(query, **kwargs):
        searches.append(query)
        assert kwargs["mode"] == "bm25"
        return [doc]

    def model_substitute(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        captured.append(payload)
        assert "Cases 是历史处理经验，不能替代 Docs" in kwargs["system_prompt"]
        assert "优先考虑 search_docs" in kwargs["system_prompt"]
        assert not {"expected_action", "expected_answer", "necessary_questions"} & payload.keys()
        if not payload["docs"]:
            assert payload["cases"][0]["source_id"] == case_id
            assert payload["docs_search_rounds"] == 0
            assert payload["agent_steps"] < payload["execution_limits"]["max_agent_steps"]
            return json.dumps({"next_step": "search_docs", "query": row["input"]["subject"],
                               "reason": "产品规则缺少文档依据"}, ensure_ascii=False)
        assert payload["docs"][0]["text"] == doc.text
        return json.dumps({"next_step": "propose_resolution", "reason": "依据适用产品文档解释",
                           "reply": reply, "evidence_ids": [doc.source_id]}, ensure_ascii=False)

    monkeypatch.setattr(runtime, "retrieve_docs", docs_search)
    monkeypatch.setattr(decide, "generate_text", model_substitute)
    runner = runtime.AgentRunner(
        QwenSettings(_env_file=None, DASHSCOPE_API_KEY="unused", DASHSCOPE_WORKSPACE_ID="unused"),
        MilvusSettings(_env_file=None, uri="http://unused.invalid"),
        ProcessingSettings(_env_file=None, retrieval_mode="bm25", decision_model="qwen3.8-flash",
                           docs_dataset=doc.docs_version),
        corpus=corpus, docs_store=FakeDocStore(doc.docs_version), session_factory=lambda: None,
        milvus_factory=lambda _: SimpleNamespace(close=lambda: None),
        judge_fn=lambda *args: {"violations": []},
    )
    output = runner(AgentRunInput(subject=row["input"]["subject"], messages=[
        AgentMessage(role="customer", content=row["input"]["body"])]))
    proposal = output.state["proposal"]
    assert [call["tool"] for call in output.state["tool_calls"]] == ["search_cases", "search_docs"]
    assert all(call["status"] == "succeeded" for call in output.state["tool_calls"])
    assert len(searches) == 1 and len(captured) == 2
    assert proposal.next_step == "propose_resolution"  # Concept/read-only inquiry need not escalate.
    assert proposal.evidence_ids == [doc.source_id] and case_id not in proposal.evidence_ids
    assert reference_quality_issues(proposal, required, unsupported, doc.source_id) == []
    assert output.state["docs_search_rounds"] == 1
    assert output.state["agent_steps"] <= runner.config.max_agent_steps
    assert {"kind": "docs", **doc.model_dump(mode="json")} in output.evidence


@pytest.mark.parametrize("regression", REGRESSIONS, ids=lambda row: "TEST-" + row[0])
def test_quality_rubric_rejects_missing_facts_wrong_escalation_and_invented_rules(regression):
    _, doc_id, section, case_id, reply, required, unsupported = regression
    doc = document_hit(doc_id, section)
    base = {"next_step": "propose_resolution", "reason": "测试专用参考",
            "reply": reply, "evidence_ids": [doc.source_id]}
    assert reference_quality_issues(decision_adapter.validate_python(base), required, unsupported, doc.source_id) == []
    for changes, issue in (
        ({"next_step": "escalate"}, "wrong_action"),
        ({"evidence_ids": [case_id]}, "wrong_evidence"),
        ({"reply": reply.replace(required[-1], "")}, "missing_diagnostic_fact"),
        ({"reply": reply + unsupported}, "unsupported_product_claim"),
    ):
        proposal = decision_adapter.validate_python({**base, **changes})
        assert issue in reference_quality_issues(proposal, required, unsupported, doc.source_id)


@pytest.mark.parametrize("condition", ["未检索", "无命中", "证据不适用"])
def test_no_docs_evidence_does_not_establish_negative_product_rule(condition):
    system, user = decide.decision_messages({
        "subject": "产品规则咨询", "messages": [AgentMessage(role="customer", content="请解释规则")],
        "tool_calls": [], "docs_hits": [], "retrieval_hits": [],
    })
    assert condition in system
    assert "不能断言产品不支持或知识库不存在相关规则" in system
    assert "也不应仅据此转人工" in system
    assert json.loads(user)["docs"] == []


def test_policy_is_conditional_bounded_and_compact_without_fixed_answers():
    prompt = decide.SYSTEM_PROMPT
    assert "回复依赖产品特定规则且现有证据不足时" in prompt
    assert "检索次数与 step budget 内" in prompt and "Cases/Docs 额度独立" in prompt
    assert "不强制每单查 Docs" in prompt
    assert "仅询问产品规则或角色概念，不等于请求执行高风险操作" in prompt
    clarification = prompt.split("4. **澄清问题**")[1].split("5. **")[0]
    assert len(clarification) <= 400
    for number, _, _, _, reference, _, _ in REGRESSIONS:
        assert "TEST-" + number not in prompt and reference not in prompt
    assert "upsert" not in prompt and "viewer" not in prompt
