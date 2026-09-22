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


def get_runner(request: Request, *, retrieval_mode=None):
    if request.app.state.runner is not None:
        if retrieval_mode is not None:
            raise AppError(422, "fixed_runner_mode", "当前固定演示适配器不支持切换检索模式")
        return request.app.state.runner
    try:
        config = request.app.state.processing_settings
        if retrieval_mode is not None:
            config = config.model_copy(update={"retrieval_mode": retrieval_mode})
        runner = AgentRunner(QwenSettings(), MilvusSettings(), config,
                             session_factory=request.app.state.session_factory)
        from ticketmind.retrieval.schemas import RetrievalError
        try:
            runner.corpus.dataset()
        except RetrievalError as exc:
            raise AppError(503, exc.code, "知识数据集尚未初始化，请先执行 migration 与 knowledge seed") from None
        return runner
    except (ValidationError, ValueError, OSError):
        raise AppError(503, "agent_configuration_unavailable", "Agent 配置不可用，请检查本地配置") from None
