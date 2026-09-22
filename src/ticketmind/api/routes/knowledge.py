from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from ticketmind.api.dependencies import IdempotencyKey
from ticketmind.core.auth import ActorDependency, ReviewerDependency
from ticketmind.core.config import MilvusSettings, QwenSettings
from ticketmind.core.errors import AppError
from ticketmind.knowledge.index import MilvusKnowledgeIndex
from ticketmind.knowledge.service import KnowledgeWrite, approve_knowledge, change_knowledge, read_case, require_case, ticket_knowledge
from ticketmind.knowledge.sync import KnowledgeSync
from ticketmind.knowledge.schemas import KnowledgeApprove
from ticketmind.retrieval.milvus_client import build_milvus_client
from ticketmind.retrieval.schemas import RetrievalError

router = APIRouter(tags=["knowledge"])


def sync_case(request, dataset, source_id, *, repair_active=False):
    factory = request.app.state.session_factory
    with factory() as session:
        before = require_case(session, dataset, source_id)
        observed_version = before.version
        if (before.status == "active" and not repair_active) or (before.status == "retired" and before.index_error is None):
            return read_case(before)
    if request.app.state.knowledge_sync is not None:
        return request.app.state.knowledge_sync.one(dataset, source_id, repair_active=repair_active)
    client = None
    try:
        milvus = MilvusSettings()
        try:
            qwen = QwenSettings()
        except ValidationError:
            qwen = None  # Keyword indexing does not require model credentials.
        client = build_milvus_client(milvus)
        # HTTP publication/retry NEVER authorizes paid embedding calls.
        return KnowledgeSync(factory, MilvusKnowledgeIndex(client, milvus.timeout_seconds), qwen).one(
            dataset, source_id, repair_active=repair_active)
    except RetrievalError as exc:
        if exc.code == "knowledge_sync_busy":
            raise AppError(409, exc.code, "知识同步正在执行；稍后以原请求重试") from None
        raise
    except Exception:
        with factory() as session, session.begin():
            case = require_case(session, dataset, source_id, lock=True)
            if case.version == observed_version:
                if case.status != "retired":
                    case.status = "index_failed"
                case.index_error = "knowledge_index_unavailable"
                case.version += 1
            session.flush()
            return read_case(case)
    finally:
        if client is not None:
            client.close()


@router.get("/tickets/{ticket_id}/knowledge")
def get_ticket_knowledge(ticket_id: UUID, request: Request, actor: ActorDependency):
    with request.app.state.session_factory() as session:
        return ticket_knowledge(session, ticket_id)


@router.post("/tickets/{ticket_id}/knowledge/approve")
def approve(ticket_id: UUID, payload: KnowledgeApprove, actor: ReviewerDependency, key: IdempotencyKey,
            request: Request, response: Response):
    case, created = approve_knowledge(request.app.state.session_factory, ticket_id, payload, actor, key)
    response.status_code = 201 if created else 200
    return sync_case(request, case["dataset_version"], case["source_id"])


@router.get("/knowledge/{dataset}/{source_id}")
def get_knowledge(dataset: str, source_id: str, request: Request, actor: ActorDependency):
    with request.app.state.session_factory() as session:
        return read_case(require_case(session, dataset, source_id))


@router.post("/knowledge/{dataset}/{source_id}/retry")
def retry(dataset: str, source_id: str, payload: KnowledgeWrite, request: Request, actor: ReviewerDependency, key: IdempotencyKey):
    change_knowledge(request.app.state.session_factory, dataset, source_id, payload, actor, key, operation="retry")
    return sync_case(request, dataset, source_id, repair_active=True)


@router.post("/knowledge/{dataset}/{source_id}/retire")
def retire(dataset: str, source_id: str, payload: KnowledgeWrite, request: Request, actor: ReviewerDependency, key: IdempotencyKey):
    change_knowledge(request.app.state.session_factory, dataset, source_id, payload, actor, key, operation="retire")
    return sync_case(request, dataset, source_id)
