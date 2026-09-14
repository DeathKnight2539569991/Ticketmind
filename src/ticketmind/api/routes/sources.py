from fastapi import APIRouter, Query, Request

from ticketmind.core.auth import ActorDependency
from ticketmind.core.errors import AppError
from ticketmind.knowledge.sources import load_sources

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("/{source_id}")
def get_source(source_id: str, actor: ActorDependency, request: Request,
               corpus_version: str = Query(min_length=1, max_length=128)):
    try:
        corpus = load_sources(request.app.state.processing_settings.corpus_path)
    except (OSError, ValueError):
        raise AppError(503, "corpus_unavailable", "来源语料不可用") from None
    if corpus.version != corpus_version:
        raise AppError(404, "source_version_unavailable", "该语料版本不可用，请查看运行记录中保存的证据快照")
    case = corpus.cases.get(source_id)
    if case is None:
        raise AppError(404, "source_not_found", "来源不存在")
    return {"corpus_version": corpus.version, "source": case.model_dump(mode="json")}
