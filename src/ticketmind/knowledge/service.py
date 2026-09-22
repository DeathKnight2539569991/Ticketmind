"""Knowledge publishing requires a trusted reviewer and a resolved ticket snapshot."""
from datetime import UTC, datetime

from sqlalchemy import select

from ticketmind.core.errors import AppError
from ticketmind.knowledge.models import KnowledgeCase, KnowledgeOperation, PRODUCTION_DATASET
from ticketmind.knowledge.seed import content_hash, ensure_production
from ticketmind.tickets.enums import TicketStatus
from ticketmind.tickets.models import TicketMessage
from ticketmind.knowledge.schemas import KnowledgeWrite


def require_reviewer(actor):
    if actor.role != "reviewer":
        raise AppError(403, "reviewer_required", "发布、重试或停用知识需要 reviewer 权限")


def read_case(case):
    result = {key: getattr(case, key) for key in (
        "dataset_version", "source_id", "title", "problem", "content", "source", "case_metadata", "source_type",
        "source_ticket_id", "content_hash", "revision", "version", "status", "reviewer_id", "approved_at",
        "indexed_hash", "index_error", "dense_indexed_hash", "dense_index_error",
        "indexed_at", "retired_at", "created_at", "updated_at")}
    result["retrieval_ready"] = {
        "bm25": case.status == "active" and case.indexed_hash == case.content_hash,
        "dense": case.status == "active" and case.dense_indexed_hash == case.content_hash,
    }
    return {key: value.astimezone(UTC) if isinstance(value, datetime) else value for key, value in result.items()}


def require_case(session, dataset, source_id, *, lock=False):
    query = select(KnowledgeCase).where(KnowledgeCase.dataset_version == dataset, KnowledgeCase.source_id == source_id)
    case = session.scalar(query.with_for_update() if lock else query)
    if case is None:
        raise AppError(404, "knowledge_not_found", "知识案例不存在")
    return case


def ticket_candidate(session, ticket):
    if ticket.status != TicketStatus.RESOLVED:
        return None
    messages = session.scalars(select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
                               .order_by(TicketMessage.sequence_number)).all()
    # Include only published messages; draft ProcessingResult text is never used.
    source = {"source_id": f"TICKET-{ticket.id}", "synthetic": False, "status": "resolved",
              "ticket_id": str(ticket.id), "ticket_version": ticket.version,
              "request": {"subject": ticket.subject, "channel": ticket.channel.value, "requester_role": ticket.requester_role},
              "messages": [{"id": str(m.id), "sequence_number": m.sequence_number, "author_type": m.author_type.value,
                            "operation": m.operation, "body": m.body} for m in messages]}
    problem = "\n\n".join(m.body for m in messages if m.author_type.value == "customer")
    replies = "\n\n".join(f"[{m.sequence_number} {m.author_type.value}]\n{m.body}" for m in messages)
    content = f"标题：{ticket.subject}\n\n已解决工单的已发布会话（按时间顺序）：\n{replies}"
    return {"dataset_version": PRODUCTION_DATASET, "source_id": source["source_id"], "title": ticket.subject,
            "problem": problem, "content": content, "source": source, "content_hash": content_hash(source),
            "case_metadata": {"synthetic": False, "ticket_number": ticket.ticket_number, "ticket_version": ticket.version},
            "source_type": "ticket", "source_ticket_id": ticket.id}


def ticket_knowledge(session, ticket_id):
    from ticketmind.tickets.processing import require_ticket
    ticket = require_ticket(session, ticket_id)
    case = session.scalar(select(KnowledgeCase).where(KnowledgeCase.source_ticket_id == ticket_id))
    if case:
        return {"eligible": True, "candidate": None, "knowledge": read_case(case), "ticket_version": ticket.version}
    candidate = ticket_candidate(session, ticket)
    return {"eligible": ticket.status == TicketStatus.RESOLVED,
            "candidate": {**candidate, "status": "candidate"} if candidate else None,
            "knowledge": None, "ticket_version": ticket.version}


def operation_once(session, resource, operation, actor, key, payload):
    data = payload.model_dump(mode="json")
    if data.get("article") is None:
        data.pop("article", None)
    digest = content_hash(data)
    existing = session.scalar(select(KnowledgeOperation).where(KnowledgeOperation.resource == resource,
        KnowledgeOperation.actor_id == actor.actor_id, KnowledgeOperation.operation == operation,
        KnowledgeOperation.idempotency_key == key))
    if existing:
        if existing.request_hash != digest:
            raise AppError(409, "idempotency_conflict", "同一请求标识已用于不同内容")
        return True
    session.add(KnowledgeOperation(resource=resource, actor_id=actor.actor_id, operation=operation,
        idempotency_key=key, request_hash=digest))
    return False


def approve_knowledge(factory, ticket_id, payload, actor, key):
    from ticketmind.tickets.processing import require_ticket
    require_reviewer(actor)
    with factory() as session, session.begin():
        ticket = require_ticket(session, ticket_id, lock=True)
        replay = operation_once(session, str(ticket_id), "approve", actor, key, payload)
        case = session.scalar(select(KnowledgeCase).where(KnowledgeCase.source_ticket_id == ticket_id))
        if replay:
            return read_case(case), False
        if ticket.version != payload.expected_version:
            raise AppError(409, "version_conflict", "工单版本已变化，请重新核对候选内容")
        candidate = ticket_candidate(session, ticket)
        if candidate is None:
            raise AppError(409, "ticket_not_resolved", "只有已解决工单可以批准为知识")
        if case:
            return read_case(case), False
        article = getattr(payload, "article", None)
        if article is None:
            raise AppError(422, "knowledge_article_required", "请整理问题、适用条件、最终处理步骤和验证结果后再批准")
        try:
            candidate["content"] = article.validate_size(ticket.subject)
        except ValueError as exc:
            raise AppError(422, "knowledge_text_too_long", str(exc)) from None
        candidate["problem"] = article.problem
        # Keep the full, original transcript for audit only. The model sees the
        # explicitly reviewed article, including through get_case_detail.
        candidate["source"] = {**candidate["source"], "article": article.model_dump()}
        candidate["content_hash"] = content_hash(candidate["source"])
        ensure_production(session)
        case = KnowledgeCase(**candidate, reviewer_id=actor.actor_id, approved_at=datetime.now(UTC))
        session.add(case)
        session.flush()
        return read_case(case), True


def change_knowledge(factory, dataset, source_id, payload, actor, key, *, operation):
    require_reviewer(actor)
    if operation not in ("retry", "retire"):
        raise ValueError("invalid knowledge operation")
    with factory() as session, session.begin():
        case = require_case(session, dataset, source_id, lock=True)
        replay = operation_once(session, f"{dataset}/{source_id}", operation, actor, key, payload)
        if replay:
            return read_case(case)
        if case.version != payload.expected_version:
            raise AppError(409, "version_conflict", "知识状态已变化，请刷新")
        if operation == "retire":
            case.status, case.retired_at = "retired", datetime.now(UTC)
            case.index_error = "index_delete_pending"
        elif case.status not in ("retired", "active"):
            case.status, case.index_error = "pending_index", None
        case.version += 1
        session.flush()
        return read_case(case)
