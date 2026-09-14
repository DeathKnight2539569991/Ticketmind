from typing import Annotated

from fastapi import Header, Request
from pydantic import StringConstraints, ValidationError

from ticketmind.agent.runtime import AgentRunner
from ticketmind.core.config import MilvusSettings, QwenSettings
from ticketmind.core.errors import AppError

IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
                           Header(alias="Idempotency-Key")]


def get_session(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def get_runner(request: Request):
    if request.app.state.runner is not None:
        return request.app.state.runner
    try:
        return AgentRunner(QwenSettings(), MilvusSettings(), request.app.state.processing_settings)
    except (ValidationError, ValueError, OSError):
        raise AppError(503, "agent_configuration_unavailable", "Agent 配置或语料不可用，请检查本地配置") from None
