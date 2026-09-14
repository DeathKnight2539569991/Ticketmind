from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ticketmind.api.dependencies import IdempotencyKey, get_runner, get_session
from ticketmind.api.schemas.runs import RunCreate, RunRead, ReviewCreate
from ticketmind.core.auth import ActorDependency, ReviewerDependency
from ticketmind.tickets.reviews import review_run
from ticketmind.core.errors import AppError
from ticketmind.tickets.models import ProcessingResult
from ticketmind.tickets.processing import create_run, require_ticket

router = APIRouter(prefix="/tickets/{ticket_id}/runs", tags=["runs"])


@router.post("", response_model=RunRead, status_code=201)
def start_run(ticket_id: UUID, payload: RunCreate, actor: ActorDependency, key: IdempotencyKey,
              request: Request, response: Response):
    result, created = create_run(request.app.state.session_factory, lambda: get_runner(request),
                                 ticket_id, payload, actor.actor_id, key, workflow=request.app.state.workflow)
    response.status_code = 201 if created else 200
    return result


@router.get("", response_model=list[RunRead])
def list_runs(ticket_id: UUID, actor: ActorDependency, session: Annotated[Session, Depends(get_session)],
              limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    require_ticket(session, ticket_id)
    return session.scalars(select(ProcessingResult).where(ProcessingResult.ticket_id == ticket_id)
                           .order_by(ProcessingResult.run_sequence.desc()).limit(limit).offset(offset)).all()


@router.get("/{run_id}", response_model=RunRead)
def get_run(ticket_id: UUID, run_id: UUID, actor: ActorDependency,
            session: Annotated[Session, Depends(get_session)]):
    run = session.scalar(select(ProcessingResult).where(ProcessingResult.id == run_id,
                                                       ProcessingResult.ticket_id == ticket_id))
    if run is None:
        raise AppError(404, "run_not_found", "该工单下不存在指定运行")
    return run


@router.post("/{run_id}/review", response_model=RunRead, status_code=201)
def review_endpoint(ticket_id: UUID, run_id: UUID, payload: ReviewCreate, actor: ReviewerDependency,
                    key: IdempotencyKey, request: Request, response: Response):
    result, created = review_run(request.app.state.session_factory, request.app.state.workflow,
                                ticket_id, run_id, payload, actor.actor_id, key)
    response.status_code = 201 if created else 200
    return result
