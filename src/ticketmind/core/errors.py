from uuid import uuid4
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def response(request: Request, status: int, code: str, message: str):
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(status_code=status, headers=headers, content={
            "error_code": code, "message": message, "request_id": request.state.request_id,
        })

    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError):
        return response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return response(request, 422, "invalid_request", "请求字段、路径或请求头不符合接口定义")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        logger.error("request_id=%s database_error=%s", request.state.request_id, type(exc).__name__)
        return response(request, 503, "database_unavailable", "数据库操作失败，请用原幂等标识查询或重试")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("request_id=%s internal_error=%s", request.state.request_id, type(exc).__name__)
        return response(request, 500, "internal_error", "请求未能完成，请根据 request_id 检查服务端状态")
