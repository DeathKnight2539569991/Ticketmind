"""No automatic write retries, redirects, credential persistence or shared cache."""
from dataclasses import dataclass
from copy import deepcopy
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


class ApiError(Exception):
    def __init__(self, status, code, message, request_id=None):
        self.status, self.code, self.request_id = status, code, request_id
        super().__init__(message)


def validate_base_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise ValueError("API 地址必须为不含凭据、路径或查询参数的 HTTP(S) 地址")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("非本机 API 必须使用 HTTPS")
    return url.rstrip("/")


@dataclass(frozen=True)
class PendingWrite:
    path: str
    payload: dict
    label: str
    key: str

    @classmethod
    def create(cls, path, payload, label):
        return cls(path, deepcopy(payload), label, uuid4().hex)


class ApiClient:
    def __init__(self, base_url, token, *, transport=None):
        self.base_url = validate_base_url(base_url)
        if not token or not token.isascii() or any(c.isspace() for c in token):
            raise ApiError(400, "invalid_credential_input", "请填写不含空白的 ASCII Bearer 凭据。")
        self.token, self.transport = token, transport

    def request(self, method, path, *, params=None, pending=None):
        headers = {"Authorization": "Bearer " + self.token}
        if pending:
            headers["Idempotency-Key"] = pending.key
        try:
            with httpx.Client(base_url=self.base_url, headers=headers, trust_env=False,
                              follow_redirects=False, transport=self.transport,
                              timeout=httpx.Timeout(150, connect=5)) as client:
                response = client.request(method, path, params=params, json=pending.payload if pending else None)
        except httpx.RequestError:
            raise ApiError(None, "connection_uncertain", "连接失败或超时。写入结果可能已保存，请先刷新查看，重试会保留同一请求标识。") from None
        try:
            data = response.json()
        except ValueError:
            raise ApiError(response.status_code, "invalid_response", "API 未返回有效 JSON，请检查服务日志。") from None
        if not response.is_success:
            if not isinstance(data, dict):
                data = {}
            raise ApiError(response.status_code, data.get("error_code", "http_error"),
                           data.get("message", "请求失败"), data.get("request_id") or response.headers.get("X-Request-ID"))
        return data

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def send(self, pending):
        return self.request("POST", pending.path, pending=pending)
