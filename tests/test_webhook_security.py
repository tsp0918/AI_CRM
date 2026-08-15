"""verify_webhook / record_webhook_event のテスト
(CRM_連携引き継ぎ書.md §4.1署名検証・§4.3冪等性)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from crm_mvp.api.webhook_security import (
    CLOCK_SKEW_TOLERANCE_SEC, record_webhook_event, verify_webhook,
)
from crm_mvp.models import WebhookEvent


def _sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256,
    ).hexdigest()


@pytest.fixture
def app() -> FastAPI:
    """verify_webhook を薄くラップしたテスト専用アプリ。実運用のルートには
    まだ接続しない(Context参照)ため、単体で疎通確認するための最小アプリ。"""
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _handler(request: Request, exc: HTTPException):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @app.post("/test-webhook")
    async def endpoint(request: Request):
        ctx = await verify_webhook(
            request, source="aitm", secret_env="TEST_WH_SECRET", bearer_env="TEST_WH_BEARER",
        )
        return {"tenant_id": str(ctx.tenant_id), "payload": ctx.payload}

    return app


@pytest.fixture
def client(app, monkeypatch) -> TestClient:
    monkeypatch.setenv("TEST_WH_BEARER", "wh-bearer-token")
    monkeypatch.setenv("TEST_WH_SECRET", "wh-signing-secret")
    monkeypatch.delenv("TEST_WH_SECRET_PREVIOUS", raising=False)
    return TestClient(app)


def _post(client, *, tenant_id, payload, secret="wh-signing-secret",
          bearer="wh-bearer-token", ts=None, mangle_body=False):
    body = json.dumps(payload).encode("utf-8")
    ts = ts or str(int(time.time()))
    sig = _sign(secret, ts, body)
    sent_body = body + b"x" if mangle_body else body
    return client.post(
        "/test-webhook",
        content=sent_body,
        headers={
            "Authorization": f"Bearer {bearer}",
            "X-Signature": sig,
            "X-Timestamp": ts,
            "X-Tenant-Id": str(tenant_id),
            "Content-Type": "application/json",
        },
    )


class TestVerifyWebhook:
    def test_valid_signature_passes(self, client):
        tenant_id = uuid.uuid4()
        resp = _post(client, tenant_id=tenant_id, payload={"a": 1})
        assert resp.status_code == 200
        assert resp.json() == {"tenant_id": str(tenant_id), "payload": {"a": 1}}

    def test_tampered_body_is_rejected(self, client):
        resp = _post(client, tenant_id=uuid.uuid4(), payload={"a": 1}, mangle_body=True)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_SIGNATURE"

    def test_wrong_secret_is_rejected(self, client):
        resp = _post(client, tenant_id=uuid.uuid4(), payload={"a": 1}, secret="wrong-secret")
        assert resp.status_code == 401

    def test_wrong_bearer_is_rejected(self, client):
        resp = _post(client, tenant_id=uuid.uuid4(), payload={"a": 1}, bearer="wrong-token")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_TOKEN"

    def test_expired_timestamp_is_rejected(self, client):
        old_ts = str(int(time.time()) - CLOCK_SKEW_TOLERANCE_SEC - 1)
        resp = _post(client, tenant_id=uuid.uuid4(), payload={"a": 1}, ts=old_ts)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "TIMESTAMP_EXPIRED"

    def test_invalid_tenant_header_is_forbidden(self, client):
        body = json.dumps({"a": 1}).encode("utf-8")
        ts = str(int(time.time()))
        sig = _sign("wh-signing-secret", ts, body)
        resp = client.post(
            "/test-webhook", content=body,
            headers={
                "Authorization": "Bearer wh-bearer-token", "X-Signature": sig,
                "X-Timestamp": ts, "X-Tenant-Id": "not-a-uuid",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403

    def test_rotated_previous_secret_still_accepted(self, client, monkeypatch):
        # 鍵ローテーション中: 新鍵をTEST_WH_SECRETに、旧鍵を_PREVIOUSに置く。
        # 旧鍵で署名したリクエストも引き続き通ること(引き継ぎ書§4.1)。
        monkeypatch.setenv("TEST_WH_SECRET", "new-secret")
        monkeypatch.setenv("TEST_WH_SECRET_PREVIOUS", "wh-signing-secret")
        resp = _post(client, tenant_id=uuid.uuid4(), payload={"a": 1}, secret="wh-signing-secret")
        assert resp.status_code == 200


class TestRecordWebhookEvent:
    def test_new_event_id_is_recorded(self, db_session, tenant_id):
        event, is_new = record_webhook_event(
            db_session, tenant_id, event_id="evt-1", source_system="aitm",
            event_type="transaction.approved", payload={"x": 1},
        )
        db_session.commit()
        assert is_new is True
        assert event.result == "processed"

        stored = db_session.query(WebhookEvent).filter_by(
            tenant_id=tenant_id, event_id="evt-1",
        ).one()
        assert stored.source_system == "aitm"

    def test_duplicate_event_id_returns_existing_and_is_new_false(
        self, db_session, tenant_id,
    ):
        first, _ = record_webhook_event(
            db_session, tenant_id, event_id="evt-dup", source_system="erp",
            event_type="order.updated", payload={"x": 1},
        )
        db_session.commit()

        second, is_new = record_webhook_event(
            db_session, tenant_id, event_id="evt-dup", source_system="erp",
            event_type="order.updated", payload={"x": 2},
        )
        assert is_new is False
        assert second.id == first.id

    def test_same_event_id_different_tenant_is_not_a_duplicate(
        self, db_session, tenant_id,
    ):
        from .conftest import set_tenant_context

        record_webhook_event(
            db_session, tenant_id, event_id="evt-shared", source_system="aitm",
            event_type="x", payload={},
        )
        db_session.commit()

        other_tenant = uuid.uuid4()
        set_tenant_context(db_session, other_tenant)
        _, is_new = record_webhook_event(
            db_session, other_tenant, event_id="evt-shared", source_system="aitm",
            event_type="x", payload={},
        )
        assert is_new is True
