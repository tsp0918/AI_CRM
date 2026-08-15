"""Outbox送信保証キューのテスト(CRM_連携引き継ぎ書.md §4.4リトライ規約・§6.2)。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from crm_mvp.enums import OutboxResult, OutboxStatus
from crm_mvp.models import OutboxMessage
from crm_mvp.services import outbox as outbox_module
from crm_mvp.services.outbox import (
    MAX_ATTEMPTS, enqueue_outbox, process_outbox, register_dispatcher,
    requeue_outbox_message,
)

from .conftest import set_tenant_context


@pytest.fixture(autouse=True)
def _clean_dispatchers():
    """テスト間でグローバルな _DISPATCHERS レジストリが汚染されないようにする。"""
    saved = dict(outbox_module._DISPATCHERS)
    outbox_module._DISPATCHERS.clear()
    yield
    outbox_module._DISPATCHERS.clear()
    outbox_module._DISPATCHERS.update(saved)


class TestEnqueueAndUnregisteredKind:
    def test_unregistered_kind_is_failed_no_retry_immediately(self, db_session, tenant_id):
        enqueue_outbox(
            db_session, tenant_id, target_system="aitm", kind="no.such.dispatcher",
            payload={"x": 1},
        )
        db_session.commit()

        summary = process_outbox(db_session, tenant_id)
        db_session.commit()

        assert summary == {"sent": 0, "retried": 0, "dlq": 0, "failed_no_retry": 1}
        message = db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).one()
        assert message.status == OutboxStatus.FAILED
        assert message.attempt_count == 0  # 未登録kindはdispatcherを呼ばないため試行にカウントしない


class TestRetryAndBackoff:
    def test_retry_increments_attempt_and_sets_next_attempt_at(self, db_session, tenant_id):
        register_dispatcher("test.retry", lambda session, msg: OutboxResult.RETRY)
        enqueue_outbox(
            db_session, tenant_id, target_system="aitm", kind="test.retry", payload={},
        )
        db_session.commit()

        summary = process_outbox(db_session, tenant_id)
        db_session.commit()

        assert summary == {"sent": 0, "retried": 1, "dlq": 0, "failed_no_retry": 0}
        message = db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).one()
        assert message.status == OutboxStatus.PENDING
        assert message.attempt_count == 1
        assert message.next_attempt_at > datetime.now(timezone.utc)

    def test_not_yet_due_message_is_not_reprocessed(self, db_session, tenant_id):
        register_dispatcher("test.retry", lambda session, msg: OutboxResult.RETRY)
        enqueue_outbox(
            db_session, tenant_id, target_system="aitm", kind="test.retry", payload={},
        )
        db_session.commit()

        process_outbox(db_session, tenant_id)
        db_session.commit()
        # next_attempt_at はまだ未来のはず — 直後にもう一度処理しても何もしない
        summary = process_outbox(db_session, tenant_id)
        db_session.commit()
        assert summary == {"sent": 0, "retried": 0, "dlq": 0, "failed_no_retry": 0}

    def test_exceeding_max_attempts_moves_to_dlq(self, db_session, tenant_id):
        register_dispatcher("test.always_retry", lambda session, msg: OutboxResult.RETRY)
        message = enqueue_outbox(
            db_session, tenant_id, target_system="aitm", kind="test.always_retry", payload={},
        )
        db_session.commit()

        # 手動でattempt_countをMAX_ATTEMPTS-1まで進め、next_attempt_atを過去にする
        # (バックオフを待たずに境界条件を検証するため)。
        message.attempt_count = MAX_ATTEMPTS - 1
        message.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db_session.commit()

        summary = process_outbox(db_session, tenant_id)
        db_session.commit()

        assert summary == {"sent": 0, "retried": 0, "dlq": 1, "failed_no_retry": 0}
        db_session.refresh(message)
        assert message.status == OutboxStatus.DLQ
        assert message.attempt_count == MAX_ATTEMPTS

    def test_dispatcher_exception_is_treated_as_retry(self, db_session, tenant_id):
        def _boom(session, msg):
            raise RuntimeError("network exploded")

        register_dispatcher("test.raises", _boom)
        enqueue_outbox(
            db_session, tenant_id, target_system="aitm", kind="test.raises", payload={},
        )
        db_session.commit()

        summary = process_outbox(db_session, tenant_id)
        db_session.commit()

        assert summary["retried"] == 1
        message = db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).one()
        assert "network exploded" in message.last_error


class TestSentAndRequeue:
    def test_sent_result_marks_sent_with_timestamp(self, db_session, tenant_id):
        register_dispatcher("test.ok", lambda session, msg: OutboxResult.SENT)
        enqueue_outbox(
            db_session, tenant_id, target_system="erp", kind="test.ok", payload={},
        )
        db_session.commit()

        summary = process_outbox(db_session, tenant_id)
        db_session.commit()

        assert summary == {"sent": 1, "retried": 0, "dlq": 0, "failed_no_retry": 0}
        message = db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).one()
        assert message.status == OutboxStatus.SENT
        assert message.sent_at is not None

    def test_requeue_resets_status_and_next_attempt(self, db_session, tenant_id):
        message = enqueue_outbox(
            db_session, tenant_id, target_system="erp", kind="test.whatever", payload={},
        )
        message.status = OutboxStatus.DLQ
        message.next_attempt_at = datetime.now(timezone.utc) + timedelta(hours=2)
        message.last_error = "boom"
        db_session.commit()

        requeue_outbox_message(message)
        db_session.commit()

        assert message.status == OutboxStatus.PENDING
        assert message.next_attempt_at is None
        assert message.last_error is None


class TestTenantIsolation:
    def test_process_outbox_only_touches_the_given_tenant(self, db_session, tenant_id):
        register_dispatcher("test.ok", lambda session, msg: OutboxResult.SENT)
        enqueue_outbox(
            db_session, tenant_id, target_system="erp", kind="test.ok", payload={"who": "tenant_a"},
        )
        db_session.commit()

        other_tenant = uuid.uuid4()
        set_tenant_context(db_session, other_tenant)
        enqueue_outbox(
            db_session, other_tenant, target_system="erp", kind="test.ok", payload={"who": "tenant_b"},
        )
        db_session.commit()

        summary = process_outbox(db_session, other_tenant)
        db_session.commit()
        assert summary["sent"] == 1

        set_tenant_context(db_session, tenant_id)
        db_session.commit()
        message_a = db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).one()
        assert message_a.status == OutboxStatus.PENDING  # tenant_bの処理では触られていない
