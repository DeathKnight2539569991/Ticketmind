from dataclasses import dataclass
from hmac import compare_digest
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from ticketmind.core.config import AuthSettings
from ticketmind.core.errors import AppError

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Literal["operator", "reviewer"]


def current_actor(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Actor:
    settings = request.app.state.auth_settings
    if settings is None:
        try:
            settings = AuthSettings()
        except ValidationError:
            raise AppError(503, "authentication_not_configured", "身份配置无效，请检查服务端凭据配置") from None
    if credentials is None:
        raise AppError(401, "authentication_required", "请提供 Bearer 凭据")
    supplied = credentials.credentials.encode("utf-8")
    for role in ("operator", "reviewer"):
        expected = getattr(settings, f"{role}_token")
        if expected and compare_digest(supplied, expected.get_secret_value().encode("utf-8")):
            return Actor(getattr(settings, f"{role}_id"), role)
    raise AppError(401, "invalid_credentials", "身份凭据无效或未配置")


ActorDependency = Annotated[Actor, Depends(current_actor)]
