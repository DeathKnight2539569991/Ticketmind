"""AppTest -> real loopback HTTP -> real PG; index/vector doubles are explicit."""
from uuid import uuid4

import pytest

from test_m5_workbench import ui, database, login, create, click, widget
from test_m1_api import pytestmark
from ticketmind.workbench.demo import demo_knowledge_sync


@pytest.mark.parametrize("missing_vector", [False, True])
def test_workbench_knowledge_publish_and_retry(ui, missing_vector):
    at, client, factory, auth = ui
    sync = demo_knowledge_sync(factory)
    if missing_vector:
        sync.embedding_budget = 0
    client.app.state.knowledge_sync = sync
    login(at, auth.reviewer_token.get_secret_value())
    ticket = create(at, "知识沉淀合成场景 " + uuid4().hex[:8])
    widget(at, "text_area", "解决说明").set_value("客户已确认解决")
    widget(at, "checkbox", "我已确认问题解决，可以关闭工单").check()
    click(at, "确认解决并关闭")
    click(at, "确认提交 / 原样重试")
    widget(at, "checkbox", "我已核对完整会话，批准该案例作为可检索知识").check().run()
    click(at, "Publish to Knowledge Base")
    pending = at.session_state.pending
    click(at, "确认提交 / 原样重试")
    headers = {"Authorization": "Bearer " + auth.reviewer_token.get_secret_value()}
    result = client.get(f"/tickets/{ticket}/knowledge", headers=headers).json()["knowledge"]
    if missing_vector:
        assert result["status"] == "index_failed"
        assert any("embedding_cache_missing" in e.value for e in at.error)
        sync.embedding_budget = 1
        click(at, "重试知识索引（仅使用已有向量）")
        click(at, "确认提交 / 原样重试")
        result = client.get(f"/tickets/{ticket}/knowledge", headers=headers).json()["knowledge"]
    assert result["status"] == "active" and result["source_ticket_id"] == ticket
    replay = client.post(pending.path, json=pending.payload, headers={**headers, "Idempotency-Key": pending.key})
    assert replay.status_code == 200 and replay.json()["source_id"] == result["source_id"]


def test_operator_sees_candidate_but_has_no_publish_control(ui):
    at, client, factory, auth = ui
    login(at, auth.operator_token.get_secret_value())
    ticket = create(at, "operator knowledge visibility")
    response = client.post(f"/tickets/{ticket}/close", json={"expected_version": 1, "reason": "reviewer closed"},
        headers={"Authorization": "Bearer " + auth.reviewer_token.get_secret_value(), "Idempotency-Key": uuid4().hex})
    assert response.status_code == 200
    click(at, "刷新当前工单")
    assert any("知识沉淀" in s.value for s in at.subheader)
    assert not any(b.label == "Publish to Knowledge Base" for b in at.button)
