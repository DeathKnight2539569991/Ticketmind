import httpx
import pytest

from ticketmind.workbench.client import ApiClient, ApiError, PendingWrite, validate_base_url


def test_timeout_retry_keeps_original_payload_key_and_does_not_auto_retry():
    calls = []
    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("private upstream details", request=request)
        return httpx.Response(201, json={"run_status": "failed", "error_code": "model_failed"})
    api = ApiClient("http://127.0.0.1:8000", "secret", transport=httpx.MockTransport(transport))
    payload = {"expected_version": 1}
    pending = PendingWrite.create("/tickets/id/runs", payload, "处理")
    payload["expected_version"] = 2
    with pytest.raises(ApiError, match="连接失败") as exc:
        api.send(pending)
    assert "private" not in str(exc.value) and len(calls) == 1
    assert api.send(pending)["run_status"] == "failed"
    assert calls[0].content == calls[1].content == b'{"expected_version":1}'
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]


def test_redirect_does_not_forward_token_to_another_origin():
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://external.invalid"}, json={})
    api = ApiClient("http://localhost:8000", "secret", transport=httpx.MockTransport(transport))
    with pytest.raises(ApiError):
        api.get("/auth/me")
    assert len(calls) == 1


def test_unstructured_error_does_not_echo_proxy_page_or_secrets():
    api = ApiClient("http://localhost:8000", "secret", transport=httpx.MockTransport(
        lambda _: httpx.Response(502, text="private upstream page")))
    with pytest.raises(ApiError, match="有效 JSON") as exc:
        api.get("/tickets")
    assert "private" not in str(exc.value)
    for url in ("http://external.invalid", "https://user:password@host", "https://host/?secret=1", "file:///env"):
        with pytest.raises(ValueError):
            validate_base_url(url)
