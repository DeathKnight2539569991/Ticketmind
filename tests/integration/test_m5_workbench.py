"""Streamlit AppTest -> loopback HTTP/PG/checkpointer; Agent is a synthetic double."""
from pathlib import Path
from uuid import UUID, uuid4
import socket
import time
from threading import Thread

import pytest
import httpx
import uvicorn
from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

import test_m1_api as m1
from ticketmind.core.config import AuthSettings
from ticketmind.main import create_app
from ticketmind.tickets.models import ProcessingReview, Ticket
from ticketmind.workbench.demo import DemoRunner

pytestmark = m1.pytestmark
database = m1.database
APP = Path(__file__).resolve().parents[2] / "src/ticketmind/workbench/app.py"


@pytest.fixture
def ui(database, monkeypatch):
    _, factory, _ = database
    auth = AuthSettings(_env_file=None, operator_token="m5-operator-" + "x" * 32, reviewer_token="m5-reviewer-" + "y" * 32)
    app = create_app(session_factory=factory, runner=DemoRunner(), auth_settings=auth)
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        monkeypatch.setenv("TICKETMIND_API_URL", f"http://127.0.0.1:{port}")
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            client.app = app
            at = AppTest.from_file(str(APP), default_timeout=15).run()
            yield at, client, factory, auth
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def widget(at, kind, label):
    return next(w for w in at.get(kind) if w.label == label)


def click(at, label):
    widget(at, "button", label).click().run()
    assert not at.exception, at.exception


def login(at, token):
    widget(at, "text_input", "Bearer 凭据").set_value(token)
    click(at, "登录")


def create(at, subject):
    widget(at, "text_input", "工单标题").set_value(subject)
    widget(at, "text_area", "问题描述").set_value("合成演示：本机代理连接失败")
    click(at, "创建工单")
    click(at, "确认提交 / 原样重试")
    return at.session_state.selected_id


def process(at):
    click(at, "处理最新客户消息")
    click(at, "确认提交 / 原样重试")


def approve(at, *, edit=False):
    widget(at, "selectbox", "审核决定").select("edit" if edit else "approve")
    if edit:
        widget(at, "text_area", "编辑后的回复（仅编辑后批准使用）").set_value("人工核对后的回复")
        widget(at, "text_area", "审核理由（编辑或转人工必填）").set_value("核对适用条件")
    widget(at, "checkbox", "我已核对回复，确认应用审核并保存发布消息").check()
    click(at, "提交审核")
    click(at, "确认提交 / 原样重试")


@pytest.mark.parametrize("subject,status", [("正常建议", "open"), ("补问继续", "awaiting_customer"), ("转人工", "escalated")])
def test_ui_three_paths_and_database_results(ui, subject, status):
    at, client, factory, auth = ui
    login(at, auth.reviewer_token.get_secret_value())
    ticket_id = create(at, subject)
    process(at)
    assert any("待人工审核" in h.value for h in at.subheader)
    approve(at, edit=status == "open")
    with factory() as session:
        ticket = session.get(Ticket, UUID(ticket_id))
        assert ticket.status == status and ticket.version == 2
        assert len(ticket.messages) == 2
        if status == "open":
            assert ticket.messages[-1].body == "人工核对后的回复"
    if status == "awaiting_customer":
        widget(at, "text_area", "消息正文").set_value("已启用本机 HTTP 代理")
        click(at, "保存消息")
        click(at, "确认提交 / 原样重试")
        process(at)
        approve(at)
        with factory() as session:
            assert session.get(Ticket, UUID(ticket_id)).status == "open"
    if status == "escalated":
        assert widget(at, "button", "处理最新客户消息").disabled
        widget(at, "selectbox", "消息类型").select("human_reply")
        widget(at, "text_area", "消息正文").set_value("人工核实处理完毕")
        click(at, "保存消息")
        click(at, "确认提交 / 原样重试")
    widget(at, "text_area", "解决说明").set_value("客户确认已解决")
    widget(at, "checkbox", "我已确认问题解决，可以关闭工单").check()
    click(at, "确认解决并关闭")
    click(at, "确认提交 / 原样重试")
    with factory() as session:
        assert session.get(Ticket, UUID(ticket_id)).status == "resolved"


def test_ui_operator_permissions_and_failed_201(ui):
    at, client, factory, auth = ui
    login(at, auth.operator_token.get_secret_value())
    ticket_id = create(at, "故障演示")
    process(at)
    assert any("失败" in e.value for e in at.error)
    assert not any(b.label == "确认解决并关闭" for b in at.button)
    assert widget(at, "selectbox", "消息类型").options == ["客户补充"]
    with factory() as session:
        assert session.get(Ticket, UUID(ticket_id)).status == "open"
    response = client.post(f"/tickets/{ticket_id}/close", headers={"Authorization": "Bearer " + auth.operator_token.get_secret_value(), "Idempotency-Key": uuid4().hex},
                           json={"expected_version": 1, "reason": "unauthorized"})
    assert response.status_code == 403
    assert client.get("/auth/me").status_code == 401


def test_ui_stale_request_preserves_viewed_version_and_blocks_review(ui):
    at, client, factory, auth = ui
    login(at, auth.reviewer_token.get_secret_value())
    ticket_id = create(at, "过期审核")
    process(at)
    widget(at, "checkbox", "我已核对回复，确认应用审核并保存发布消息").check()
    click(at, "提交审核")
    pending = at.session_state.pending
    assert pending.payload["expected_version"] == 1
    response = client.post(f"/tickets/{ticket_id}/messages", headers={"Authorization": "Bearer " + auth.operator_token.get_secret_value(), "Idempotency-Key": uuid4().hex},
                           json={"kind": "customer_update", "body": "其他会话的新信息", "expected_version": 1})
    assert response.status_code == 201
    click(at, "确认提交 / 原样重试")
    assert any("变化" in e.value for e in at.error)
    assert at.session_state.pending == pending
    with factory() as session:
        assert session.get(Ticket, UUID(ticket_id)).version == 2
    click(at, "取消当前请求")
    assert any("失效" in h.value for h in at.subheader)
    assert not any(b.label == "提交审核" for b in at.button)


def test_ui_saved_failed_review_resumes_with_original_key(ui, monkeypatch):
    at, client, factory, auth = ui
    login(at, auth.reviewer_token.get_secret_value())
    ticket_id = create(at, "审核恢复")
    process(at)
    resume = client.app.state.workflow.resume
    def fail(*args):
        raise RuntimeError("synthetic resume error")
    monkeypatch.setattr(client.app.state.workflow, "resume", fail)
    approve(at)
    key = at.session_state.pending.key
    assert any("应用失败" in e.value for e in at.error)
    click(at, "取消当前请求")
    monkeypatch.setattr(client.app.state.workflow, "resume", resume)
    click(at, "重试已保存的审核")
    assert at.session_state.pending.key == key
    pending = at.session_state.pending
    click(at, "确认提交 / 原样重试")
    response = client.post(pending.path, json=pending.payload, headers={"Authorization": "Bearer " + auth.reviewer_token.get_secret_value(), "Idempotency-Key": key})
    assert response.status_code == 200
    with factory() as session:
        ticket = session.get(Ticket, UUID(ticket_id))
        assert len(ticket.messages) == 2
        assert session.scalar(select(func.count()).select_from(ProcessingReview).where(ProcessingReview.idempotency_key == key)) == 1
