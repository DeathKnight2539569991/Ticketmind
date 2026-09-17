"""Run: streamlit run src/ticketmind/workbench/app.py. Never imports DB/model clients."""
import os
from urllib.parse import quote

import streamlit as st

from ticketmind.workbench.client import ApiClient, ApiError, PendingWrite

STATUS = {"open": "处理中", "awaiting_customer": "等待客户", "escalated": "已转人工", "resolved": "已解决"}
RUN = {"running": "执行中", "waiting_review": "待人工审核", "completed": "审核已应用", "failed": "执行失败", "cancelled": "已失效"}
ACTION = {"propose_resolution": "建议解决方案", "resolve": "建议解决方案", "ask_clarification": "补充信息", "escalate": "转人工建议"}
AUTHOR = {"customer": "客户", "agent": "已审核的 Agent 回复", "human_support": "人工客服", "system": "系统审计"}


def error(exc):
    st.error(str(exc))
    st.caption(f"错误码：{exc.code} · 请求 ID：{exc.request_id or '未取得'}")
    if exc.status == 409:
        st.info("数据或状态已变化。请取消当前请求并刷新工单，核对后重新操作。")


def queue(path, payload, label):
    st.session_state.pending = PendingWrite.create(path, payload, label)
    st.rerun()


def pending_panel(api):
    pending = st.session_state.get("pending")
    if not pending:
        return
    st.subheader("确认操作")
    st.write(pending.label)
    if pending.path.endswith("/runs") and st.session_state.actor.get("mode") != "synthetic_demo":
        st.warning("正式处理会调用模型与向量服务，可能产生费用。请仅在已授权额度内执行。")
    labels = {"subject": "工单标题", "body": "正文", "edited_reply": "编辑后的回复", "comment": "审核理由", "reason": "解决说明"}
    for field, label in labels.items():
        if pending.payload.get(field):
            st.markdown(f"**{label}**")
            st.text(pending.payload[field])
    if "decision" in pending.payload:
        st.write("审核决定：" + {"approve": "批准已核对的草稿", "edit": "编辑后批准", "escalate": "改为转人工"}[pending.payload["decision"]])
    st.caption("此请求保留原版本和请求标识；网络异常后可原样重试。刷新浏览器或退出会话会丢失未完成请求，请先核对工单记录。")
    with st.expander("请求记录（用于失败定位与原样重试）"):
        st.code(pending.key, language=None)
        st.json(pending.payload)
    if pending.path.startswith("/tickets/") and st.button("查看最新状态（保留当前请求）"):
        try:
            current = api.get("/tickets/" + pending.path.split("/")[2])
            st.json({"status": current["status"], "version": current["version"],
                     "messages": current["messages"], "latest_run": current["latest_run"]})
        except ApiError as exc:
            error(exc)
    left, right = st.columns(2)
    if left.button("确认提交 / 原样重试", type="primary", key="confirm_write"):
        try:
            with st.spinner("正在提交，请勿关闭页面…"):
                result = api.send(pending)
            st.session_state.pop("detail", None)
            if result.get("run_status") == "failed" and result.get("review"):
                st.error("审核已保存，但应用失败。可使用当前请求原样重试。")
                st.text(result.get("error_summary") or result.get("error_code"))
            else:
                st.session_state.pop("pending", None)
                if pending.path == "/tickets":
                    st.session_state.selected_id = result["id"]
                st.session_state.notice = (
                    "error" if result.get("run_status") == "failed" else "success",
                    f"运行：{RUN.get(result['run_status'], result['run_status'])}。{result.get('error_summary') or ''}"
                    if "run_status" in result else "操作已保存。",
                )
                st.rerun()
        except ApiError as exc:
            error(exc)
    if right.button("取消当前请求", key="cancel_write"):
        st.session_state.pop("pending", None)
        st.session_state.pop("detail", None)
        st.rerun()
    st.stop()


def create_form():
    with st.expander("新建工单", expanded=not st.session_state.get("selected_id")):
        with st.form("create"):
            subject = st.text_input("工单标题", max_chars=500)
            body = st.text_area("问题描述", height=130)
            channel = st.selectbox("来源渠道", ["web", "email", "api"])
            requester = st.text_input("客户角色（仅业务信息）", value="用户", max_chars=64)
            if st.form_submit_button("创建工单"):
                if not all(x.strip() for x in (subject, body, requester)):
                    st.error("请填写标题、问题描述和客户角色。")
                else:
                    queue("/tickets", dict(subject=subject, body=body, channel=channel, requester_role=requester), "创建工单")


def run_view(api, ticket, run, reviewer):
    st.subheader(f"运行 #{run['run_sequence']} · {RUN.get(run['run_status'], run['run_status'])}")
    st.caption(f"{run['id']} · {run.get('retrieval_mode') or '—'} · {run.get('duration_ms') or 0} ms")
    if run["run_status"] == "failed":
        st.error(f"{run.get('error_code')}：{run.get('error_summary')}")
    elif run["run_status"] == "cancelled":
        st.warning("此提案已失效，不能用于审核发布。")
    elif run["run_status"] == "waiting_review":
        st.info("草稿尚未发布。请核对事实、引用及状态承诺。")
    proposal = run.get("proposal") or {}
    st.write(ACTION.get(proposal.get("next_step"), proposal.get("next_step") or "尚无提案"))
    st.text(proposal.get("reason") or run.get("reason") or "")
    st.markdown("**原始提案 / 草稿**")
    st.text(proposal.get("reply") or run.get("final_reply") or "尚无草稿")
    for question in proposal.get("questions", []):
        st.text("• " + question)
    with st.expander("理解结果"):
        st.json(run.get("extracted_information") or {})
    with st.expander("引用与检索证据"):
        st.caption("检索命中不等于适用；分数不是正确概率。以下为运行时保存的证据。")
        st.write("提案引用：" + "、".join(proposal.get("evidence_ids", [])))
        for index, evidence in enumerate(run.get("retrieval_evidence", [])):
            st.json(evidence)
            source_id = evidence.get("source_id")
            if source_id and st.button(f"查看来源 {source_id}", key=f"source-{run['id']}-{index}"):
                try:
                    st.json(api.get("/sources/" + quote(source_id, safe=""), corpus_version=run["corpus_version"]))
                except ApiError as exc:
                    error(exc)
        st.caption("语料版本：" + str(run.get("corpus_version")))
    with st.expander("工具、模型与调用记录"):
        st.json({k: run.get(k) for k in ("tool_calls", "models", "usage", "agent_version")})
    review = run.get("review")
    if review:
        st.markdown("**人工审核记录**")
        st.json(review)
        published = next((m for m in ticket["messages"] if m["id"] == run.get("published_message_id")), None)
        st.markdown("**实际发布消息**")
        st.text(published["body"] if published else "尚无发布消息")
        if not review.get("applied_at") and run["run_status"] == "failed" and reviewer:
            if st.button("重试已保存的审核", key="retry_review"):
                st.session_state.pending = PendingWrite(
                    f"/tickets/{ticket['id']}/runs/{run['id']}/review",
                    {k: review[k] for k in ("decision", "expected_version", "edited_reply", "comment")},
                    "以原审核人身份重试已保存的审核", review["idempotency_key"])
                st.rerun()
    if reviewer and run["run_status"] == "waiting_review" and not review:
        with st.form(f"review-{run['id']}-{ticket['version']}"):
            decision = st.selectbox("审核决定", ["approve", "edit", "escalate"],
                                    format_func=lambda x: {"approve": "批准草稿", "edit": "编辑后批准", "escalate": "改为转人工"}[x])
            edited = st.text_area("编辑后的回复（仅编辑后批准使用）", value=proposal.get("reply") or "", height=160)
            comment = st.text_area("审核理由（编辑或转人工必填）")
            acknowledged = st.checkbox("我已核对回复，确认应用审核并保存发布消息")
            if st.form_submit_button("提交审核"):
                if not acknowledged or (decision in ("edit", "escalate") and not comment.strip()) or (decision == "edit" and not edited.strip()):
                    st.error("请确认已核对回复，并填写所需文本与理由。")
                else:
                    queue(f"/tickets/{ticket['id']}/runs/{run['id']}/review",
                          dict(decision=decision, expected_version=ticket["version"],
                               edited_reply=edited if decision == "edit" else None, comment=comment or None), "确认人工审核；发布仅保存到本地数据库")


def detail_view(api, ticket, reviewer):
    ticket_id, version = ticket["id"], ticket["version"]
    st.header(ticket["subject"])
    st.caption(f"{ticket['ticket_number']} · {STATUS[ticket['status']]} · 版本 {version}")
    if st.button("刷新当前工单", key="refresh_detail"):
        st.session_state.pop("detail", None)
        st.rerun()
    messages_tab, runs_tab, actions_tab = st.tabs(["消息记录", "运行与审核", "工单操作"])
    with messages_tab:
        for msg in ticket["messages"]:
            with st.container(border=True):
                st.caption(f"{AUTHOR.get(msg['author_type'], msg['author_type'])} · {msg['created_at']} · #{msg['sequence_number']}")
                st.text(msg["body"])
    with runs_tab:
        offset = st.number_input("运行记录页（每页 20 条）", min_value=1, step=1, key=f"runs_page-{ticket_id}")
        runs = api.get(f"/tickets/{ticket_id}/runs", limit=20, offset=(offset - 1) * 20)
        if runs:
            selected = st.selectbox("选择运行", [r["id"] for r in runs],
                format_func=lambda rid: next(f"#{r['run_sequence']} · {RUN[r['run_status']]}" for r in runs if r["id"] == rid), key=f"run-{ticket_id}-{offset}")
            run_view(api, ticket, next(r for r in runs if r["id"] == selected), reviewer)
        else:
            st.info("此页暂无运行记录。")
    with actions_tab:
        running = bool(ticket.get("latest_run") and ticket["latest_run"]["run_status"] == "running")
        locked = ticket["status"] == "resolved" or running
        if locked:
            st.info("工单已解决或正在执行，暂不能写入。")
        customer = next((m for m in reversed(ticket["messages"]) if m["author_type"] == "customer"), None)
        st.caption("处理会创建 Agent 运行；生成建议后仍需审核。")
        if st.button("处理最新客户消息", disabled=locked or ticket["status"] != "open" or not customer, key="process"):
            queue(f"/tickets/{ticket_id}/runs", dict(expected_version=version, trigger_message_id=customer["id"]), "处理最新客户消息")
        with st.form(f"message-{ticket_id}-{version}"):
            kind = st.selectbox("消息类型", ["customer_update", "human_reply"] if reviewer else ["customer_update"],
                                format_func=lambda x: "客户补充" if x == "customer_update" else "人工回复")
            body = st.text_area("消息正文")
            st.caption("保存新消息会使旧的待审提案失效。人工回复将直接保存发布消息。")
            if st.form_submit_button("保存消息", disabled=locked):
                if body.strip():
                    queue(f"/tickets/{ticket_id}/messages", dict(kind=kind, body=body, expected_version=version), "保存客户补充" if kind == "customer_update" else "确认发布人工回复")
                else:
                    st.error("消息不能为空。")
        if reviewer:
            with st.form(f"close-{ticket_id}-{version}"):
                reason = st.text_area("解决说明")
                confirmed = st.checkbox("我已确认问题解决，可以关闭工单")
                if st.form_submit_button("确认解决并关闭", disabled=locked):
                    if confirmed and reason.strip():
                        queue(f"/tickets/{ticket_id}/close", dict(reason=reason, expected_version=version), "确认解决并关闭工单")
                    else:
                        st.error("请填写解决说明并明确确认。")


def main():
    st.set_page_config(page_title="TicketMind · 工单工作台", page_icon="🎫", layout="wide")
    st.title("TicketMind")
    st.caption("技术支持工作台 · 合成场景演示 · 所有建议先经人工审核")
    base_url = os.getenv("TICKETMIND_API_URL", "http://127.0.0.1:8000")
    with st.sidebar:
        st.header("工作会话")
        st.caption("凭据仅保存在当前会话；刷新浏览器后需重新登录。")
        if not st.session_state.get("token"):
            with st.form("login"):
                token = st.text_input("Bearer 凭据", type="password")
                login = st.form_submit_button("登录")
            if login:
                try:
                    actor = ApiClient(base_url, token).get("/auth/me")
                    st.session_state.token = token
                    st.session_state.actor = actor
                    st.rerun()
                except ApiError as exc:
                    error(exc)
                except ValueError as exc:
                    st.error(str(exc))
            st.stop()
        api = ApiClient(base_url, st.session_state.token)
        if st.button("退出登录"):
            st.session_state.clear()
            st.rerun()
        try:
            actor = api.get("/auth/me")
        except ApiError as exc:
            error(exc)
            st.stop()
        st.text(f"{actor['actor_id']} · {actor['role']}")
        if actor["role"] != "reviewer":
            st.caption("当前身份可录入工单和客户消息；审核、人工回复和关闭需 reviewer。")
    if actor.get("mode") == "synthetic_demo":
        st.warning("当前为隔离演示：Agent 与检索使用确定性替身，数据库与审核流程真实执行，不调用付费模型。")
    if st.session_state.get("notice"):
        level, text = st.session_state.pop("notice")
        getattr(st, level)(text)
    pending_panel(api)
    create_form()
    try:
        left, right = st.columns([1, 3])
        with left:
            st.subheader("工单列表")
            status = st.selectbox("工单状态", ["all", *STATUS], format_func=lambda x: "全部" if x == "all" else STATUS[x])
            page = st.number_input("工单页（每页 20 条）", min_value=1, step=1, key=f"page-{status}")
            tickets = api.get("/tickets", limit=20, offset=(page - 1) * 20, **({"status": status} if status != "all" else {}))
            if not tickets:
                st.info("此页暂无工单。")
            for ticket in tickets:
                if st.button(f"{ticket['subject']} · {STATUS[ticket['status']]}", help=ticket["ticket_number"], key=ticket["id"], use_container_width=True):
                    st.session_state.selected_id = ticket["id"]
                    st.session_state.pop("detail", None)
                    st.rerun()
        with right:
            selected = st.session_state.get("selected_id")
            if selected:
                # Keep the viewed version until explicit refresh or a confirmed write.
                # A form rerun must never silently adopt a newer expected_version.
                if "detail" not in st.session_state:
                    st.session_state.detail = api.get("/tickets/" + selected)
                detail_view(api, st.session_state.detail, actor["role"] == "reviewer")
            else:
                st.info("新建工单，或从左侧选择一张工单。")
    except ApiError as exc:
        error(exc)


if __name__ == "__main__":
    main()
