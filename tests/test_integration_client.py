"""SignedClient(署名付きHTTPクライアント)と classify_http_response のテスト
(CRM_連携引き継ぎ書.md §4.1・§4.4)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import pytest

from crm_mvp.enums import OutboxResult
from crm_mvp.services.integration_client import SignedClient, SignedClientConfigError
from crm_mvp.services.outbox import classify_http_response


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_client(monkeypatch, tenant_id, *, transport: httpx.MockTransport) -> SignedClient:
    monkeypatch.setenv("TEST_BEARER", "s3cr3t-bearer")
    monkeypatch.setenv("TEST_SIGNING_SECRET", "s3cr3t-signing-key")
    return SignedClient(
        "https://aitm.example.com", tenant_id,
        bearer_env="TEST_BEARER", secret_env="TEST_SIGNING_SECRET",
        client=httpx.Client(transport=transport),
    )


class TestSignedClientConfig:
    def test_missing_base_url_raises(self, monkeypatch, tenant_id):
        monkeypatch.setenv("TEST_BEARER", "x")
        monkeypatch.setenv("TEST_SIGNING_SECRET", "y")
        with pytest.raises(SignedClientConfigError):
            SignedClient(None, tenant_id, bearer_env="TEST_BEARER", secret_env="TEST_SIGNING_SECRET")

    def test_missing_bearer_env_raises(self, monkeypatch, tenant_id):
        monkeypatch.delenv("TEST_BEARER", raising=False)
        monkeypatch.setenv("TEST_SIGNING_SECRET", "y")
        with pytest.raises(SignedClientConfigError):
            SignedClient(
                "https://x", tenant_id, bearer_env="TEST_BEARER", secret_env="TEST_SIGNING_SECRET",
            )

    def test_missing_secret_env_raises(self, monkeypatch, tenant_id):
        monkeypatch.setenv("TEST_BEARER", "x")
        monkeypatch.delenv("TEST_SIGNING_SECRET", raising=False)
        with pytest.raises(SignedClientConfigError):
            SignedClient(
                "https://x", tenant_id, bearer_env="TEST_BEARER", secret_env="TEST_SIGNING_SECRET",
            )


class TestSignedClientPost:
    def test_sends_expected_headers_and_valid_signature(self, monkeypatch, tenant_id):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"ok": True})

        client = _make_client(monkeypatch, tenant_id, transport=httpx.MockTransport(handler))
        resp = client.post("/transactions", {"foo": "ばー"})

        assert resp.status_code == 200
        req = captured["request"]
        assert req.headers["authorization"] == "Bearer s3cr3t-bearer"
        assert req.headers["x-tenant-id"] == str(tenant_id)
        assert req.headers["x-signature"].startswith("sha256=")

        body = req.content
        ts = req.headers["x-timestamp"]
        expected_sig = hmac.new(
            b"s3cr3t-signing-key", f"{ts}.".encode("utf-8") + body, hashlib.sha256,
        ).hexdigest()
        assert req.headers["x-signature"] == f"sha256={expected_sig}"
        # 署名対象と送信ボディは同一バイト列(再シリアライズしていないことの確認)。
        assert json.loads(body) == {"foo": "ばー"}

    def test_request_id_is_used_when_provided(self, monkeypatch, tenant_id):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200)

        client = _make_client(monkeypatch, tenant_id, transport=httpx.MockTransport(handler))
        client.post("/x", {}, request_id="fixed-id-123")
        assert captured["request"].headers["x-request-id"] == "fixed-id-123"


class TestClassifyHttpResponse:
    def test_exception_is_retry(self):
        assert classify_http_response(None, exc=TimeoutError()) == OutboxResult.RETRY

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_retryable_statuses(self, status):
        resp = httpx.Response(status, request=httpx.Request("POST", "https://x"))
        assert classify_http_response(resp) == OutboxResult.RETRY

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_non_retryable_statuses(self, status):
        resp = httpx.Response(status, request=httpx.Request("POST", "https://x"))
        assert classify_http_response(resp) == OutboxResult.FAILED_NO_RETRY

    @pytest.mark.parametrize("status", [200, 201])
    def test_success_statuses_are_sent(self, status):
        resp = httpx.Response(status, request=httpx.Request("POST", "https://x"))
        assert classify_http_response(resp) == OutboxResult.SENT

    def test_409_is_treated_as_sent(self):
        resp = httpx.Response(409, request=httpx.Request("POST", "https://x"))
        assert classify_http_response(resp) == OutboxResult.SENT
