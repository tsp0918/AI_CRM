"""crm_mvp/api/web/integration_status.py の統合テスト。"""

from __future__ import annotations

import uuid

from crm_mvp.enums import OutboxStatus
from crm_mvp.models import OutboxMessage, WebhookEvent


class TestIntegrationStatusList:
    def test_renders_with_no_data(self, ui_client):
        resp = ui_client.get("/ui/integration-status")
        assert resp.status_code == 200
        assert "まだOutboxメッセージがありません" in resp.text
        assert "まだWebhook受信履歴がありません" in resp.text

    def test_lists_outbox_messages_and_webhook_events(self, ui_client, db_session, tenant_id):
        db_session.add(OutboxMessage(
            tenant_id=tenant_id, target_system="aitm", kind="aitm.transaction.create",
            payload={}, status=OutboxStatus.FAILED, last_error="未登録のkindです",
        ))
        db_session.add(WebhookEvent(
            tenant_id=tenant_id, event_id="evt-abc", source_system="erp",
            event_type="order.updated", payload={},
        ))
        db_session.commit()

        resp = ui_client.get("/ui/integration-status")
        assert resp.status_code == 200
        assert "aitm.transaction.create" in resp.text
        assert "未登録のkindです" in resp.text
        assert "evt-abc" in resp.text
        assert "order.updated" in resp.text


class TestIntegrationStatusEnqueueTestMessage:
    def test_enqueues_message_and_redirects(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/integration-status/test-message",
            data={"target_system": "aitm", "kind": "test.ping", "payload_json": '{"a": 1}'},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/ui/integration-status" in resp.headers["location"]

        message = db_session.query(OutboxMessage).filter_by(
            tenant_id=tenant_id, kind="test.ping",
        ).one()
        assert message.target_system == "aitm"
        assert message.payload == {"a": 1}
        assert message.status == OutboxStatus.PENDING

    def test_invalid_json_payload_is_rejected(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/integration-status/test-message",
            data={"target_system": "aitm", "kind": "test.ping", "payload_json": "{not json"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        assert db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).count() == 0


class TestIntegrationStatusRequeue:
    def test_requeue_resets_failed_message(self, ui_client, db_session, tenant_id):
        message = OutboxMessage(
            tenant_id=tenant_id, target_system="erp", kind="test.x",
            payload={}, status=OutboxStatus.DLQ, last_error="boom",
        )
        db_session.add(message)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/integration-status/{message.id}/requeue", follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.refresh(message)
        assert message.status == OutboxStatus.PENDING
        assert message.last_error is None

    def test_requeue_unknown_message_shows_error(self, ui_client):
        resp = ui_client.post(
            f"/ui/integration-status/{uuid.uuid4()}/requeue", follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
