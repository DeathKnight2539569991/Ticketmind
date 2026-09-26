"""AppTest -> real loopback HTTP -> real PG; index/vector doubles are explicit."""
from uuid import uuid4

import pytest

from test_m5_workbench import ui, database, login, create, click, widget
from test_m1_api import pytestmark
from ticketmind.workbench.demo import demo_knowledge_sync


def prepare_knowledge(at):
    article = {"问题概述": "本机代理连接失败", "适用条件与不适用情况": "仅适用已核对代理类型的测试客户端",
               "最终有效的处理步骤": "核对并修正客户端代理配置", "验证结果": "用户复测连接成功"}
    for label, value in article.items():
        widget(at, "text_area", label).set_value(value)
    widget(at, "checkbox", "我已核对原始会话与以上正文，批准作为可检索知识").check()
    click(at, "批准发布知识")
    pending = at.session_state.pending
    assert set(pending.payload) == {"expected_version", "article"}
    assert pending.payload["article"] == dict(zip(
        ("problem", "applicability", "solution", "verification"), article.values(), strict=True))
    return pending


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
    pending = prepare_knowledge(at)
    headers = {"Authorization": "Bearer " + auth.reviewer_token.get_secret_value()}
    assert client.get(f"/tickets/{ticket}/knowledge", headers=headers).json()["knowledge"] is None
    click(at, "确认提交 / 原样重试")
    result = client.get(f"/tickets/{ticket}/knowledge", headers=headers).json()["knowledge"]
    if missing_vector:
        assert result["status"] == "index_failed"
        assert any("embedding_cache_missing" in e.value for e in at.error)
        sync.embedding_budget = 1
        click(at, "重试知识索引（仅使用已有向量）")
        click(at, "确认提交 / 原样重试")
        result = client.get(f"/tickets/{ticket}/knowledge", headers=headers).json()["knowledge"]
    assert result["status"] == "active" and result["source_ticket_id"] == ticket
    assert "核对并修正客户端代理配置" in result["content"]
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
    assert not any(b.label == "批准发布知识" for b in at.button)


def test_reviewer_can_retire_knowledge_from_workbench(ui):
    at, client, factory, auth = ui
    sync = demo_knowledge_sync(factory)
    client.app.state.knowledge_sync = sync
    login(at, auth.reviewer_token.get_secret_value())
    ticket_id = create(at, "知识停用 " + uuid4().hex[:8])
    widget(at, "text_area", "解决说明").set_value("客户确认解决")
    widget(at, "checkbox", "我已确认问题解决，可以关闭工单").check()
    click(at, "确认解决并关闭")
    click(at, "确认提交 / 原样重试")
    prepare_knowledge(at)
    click(at, "确认提交 / 原样重试")
    headers = {"Authorization": "Bearer " + auth.reviewer_token.get_secret_value()}
    case = client.get(f"/tickets/{ticket_id}/knowledge", headers=headers).json()["knowledge"]
    assert case["status"] == "active"
    assert (case["dataset_version"], case["source_id"]) in sync.index.rows

    assert widget(at, "button", "停用这条知识").disabled
    widget(at, "checkbox", "我已核对知识全文，确认停用此知识").check().run()
    click(at, "停用这条知识")
    pending = at.session_state.pending
    assert pending.path == f"/knowledge/{case['dataset_version']}/{case['source_id']}/retire"
    assert pending.payload == {"expected_version": case["version"]}
    click(at, "确认提交 / 原样重试")

    retired = client.get(f"/tickets/{ticket_id}/knowledge", headers=headers).json()["knowledge"]
    assert retired["status"] == "retired" and retired["retired_at"]
    assert retired["content"] == case["content"]
    assert (case["dataset_version"], case["source_id"]) not in sync.index.rows
    assert not any(button.label == "停用这条知识" for button in at.button)
    replay = client.post(pending.path, json=pending.payload,
                         headers={**headers, "Idempotency-Key": pending.key})
    assert replay.status_code == 200 and replay.json()["version"] == retired["version"]
    source = client.get(f"/sources/{case['source_id']}",
                        params={"corpus_version": case["dataset_version"]}, headers=headers)
    assert source.status_code == 200


def test_retired_index_delete_failure_has_cleanup_action(ui, monkeypatch):
    at, client, factory, auth = ui
    sync = demo_knowledge_sync(factory)
    client.app.state.knowledge_sync = sync
    login(at, auth.reviewer_token.get_secret_value())
    ticket_id = create(at, "停用失败恢复 " + uuid4().hex[:8])
    widget(at, "text_area", "解决说明").set_value("客户确认解决")
    widget(at, "checkbox", "我已确认问题解决，可以关闭工单").check()
    click(at, "确认解决并关闭")
    click(at, "确认提交 / 原样重试")
    prepare_knowledge(at)
    click(at, "确认提交 / 原样重试")

    delete = sync.index.delete
    def fail_delete(*args):
        raise TimeoutError("synthetic delete error")
    monkeypatch.setattr(sync.index, "delete", fail_delete)
    widget(at, "checkbox", "我已核对知识全文，确认停用此知识").check().run()
    click(at, "停用这条知识")
    click(at, "确认提交 / 原样重试")
    headers = {"Authorization": "Bearer " + auth.reviewer_token.get_secret_value()}
    case = client.get(f"/tickets/{ticket_id}/knowledge", headers=headers).json()["knowledge"]
    assert case["status"] == "retired" and case["index_error"]
    assert not any(button.label == "停用这条知识" for button in at.button)
    assert any(button.label == "重试清理停用知识的索引" for button in at.button)

    monkeypatch.setattr(sync.index, "delete", delete)
    click(at, "重试清理停用知识的索引")
    pending = at.session_state.pending
    assert pending.payload == {"expected_version": case["version"]}
    click(at, "确认提交 / 原样重试")
    cleaned = client.get(f"/tickets/{ticket_id}/knowledge", headers=headers).json()["knowledge"]
    assert cleaned["status"] == "retired" and cleaned["index_error"] is None
    assert (case["dataset_version"], case["source_id"]) not in sync.index.rows


def test_operator_cannot_retire_published_knowledge_from_workbench(ui):
    at, client, factory, auth = ui
    sync = demo_knowledge_sync(factory)
    client.app.state.knowledge_sync = sync
    login(at, auth.reviewer_token.get_secret_value())
    ticket_id = create(at, "操作员知识权限 " + uuid4().hex[:8])
    widget(at, "text_area", "解决说明").set_value("客户确认解决")
    widget(at, "checkbox", "我已确认问题解决，可以关闭工单").check()
    click(at, "确认解决并关闭")
    click(at, "确认提交 / 原样重试")
    prepare_knowledge(at)
    click(at, "确认提交 / 原样重试")
    case = client.get(f"/tickets/{ticket_id}/knowledge", headers={
        "Authorization": "Bearer " + auth.reviewer_token.get_secret_value()}).json()["knowledge"]
    click(at, "退出登录")
    login(at, auth.operator_token.get_secret_value())
    ticket_button = next(button.label for button in at.button if button.label.startswith("操作员知识权限 "))
    click(at, ticket_button)
    assert any("知识沉淀" in section.value for section in at.subheader)
    assert not any(button.label == "停用这条知识" for button in at.button)
    response = client.post(f"/knowledge/{case['dataset_version']}/{case['source_id']}/retire",
        json={"expected_version": case["version"]},
        headers={"Authorization": "Bearer " + auth.operator_token.get_secret_value(),
                 "Idempotency-Key": uuid4().hex})
    assert response.status_code == 403


