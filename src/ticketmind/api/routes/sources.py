from fastapi import APIRouter, Query, Request

from ticketmind.core.auth import ActorDependency
from ticketmind.core.errors import AppError
from ticketmind.knowledge.models import KnowledgeCase

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("/{source_id}")
def get_source(source_id: str, actor: ActorDependency, request: Request,
               corpus_version: str = Query(min_length=1, max_length=128)):
    with request.app.state.session_factory() as session:
        case = session.get(KnowledgeCase, (corpus_version, source_id))
        if case is None:
            raise AppError(404, "source_version_unavailable", "该版本来源不存在，请查看运行记录中保存的证据快照")
        # Retired content remains auditable, but retrieval filters it out.
        return {"corpus_version": corpus_version, "source": case.source, "knowledge_revision": case.revision,
                "content_hash": case.content_hash, "status": case.status}
