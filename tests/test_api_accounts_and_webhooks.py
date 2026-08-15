"""アカウントのコンプライアンスチェック起票と Webhook 受信のテスト
(HANDOVER.md §5 Phase5 item19-21)。
"""

from __future__ import annotations

import uuid

from crm_mvp.enums import ComplianceCheckType, ComplianceOutcome, Stage
from crm_mvp.models import Account, ActionItem, ComplianceStatus

from .conftest import create_account_and_engagement


class TestSubmitComplianceCheck:
    def test_mock_screening_returns_clear_and_persists(
        self, api_client, db_session, tenant_id,
    ):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = api_client.post(
            f"/accounts/{account.id}/compliance-checks",
            json={"check_type": "sanctions"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["outcome"] == "clear"
        assert body["provider"] == "mock"
        assert len(body["idempotency_key"]) == 64

        status = db_session.query(ComplianceStatus).filter_by(
            tenant_id=tenant_id, account_id=account.id,
            check_type=ComplianceCheckType.SANCTIONS,
        ).one()
        assert status.outcome == ComplianceOutcome.CLEAR
        assert status.is_fresh is True

    def test_unknown_account_returns_404(self, api_client, tenant_id):
        resp = api_client.post(
            f"/accounts/{uuid.uuid4()}/compliance-checks",
            json={"check_type": "sanctions"},
        )
        assert resp.status_code == 404

    def test_idempotency_key_is_stable_for_same_subject(
        self, api_client, db_session, tenant_id,
    ):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        first = api_client.post(
            f"/accounts/{account.id}/compliance-checks",
            json={"check_type": "sanctions"},
        )
        second = api_client.post(
            f"/accounts/{account.id}/compliance-checks",
            json={"check_type": "sanctions"},
        )
        assert first.json()["idempotency_key"] == second.json()["idempotency_key"]


class TestComplianceJudgmentWebhook:
    def test_upserts_compliance_status(self, api_client, db_session, tenant_id):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = api_client.post("/webhooks/compliance-judgment", json={
            "account_id": str(account.id), "check_type": "export_control",
            "outcome": "hit", "provider": "ai_tm",
            "provider_request_id": "MOCK-EVT-1",
        })
        assert resp.status_code == 204

        status = db_session.query(ComplianceStatus).filter_by(
            tenant_id=tenant_id, account_id=account.id,
            check_type=ComplianceCheckType.EXPORT_CONTROL,
        ).one()
        assert status.outcome == ComplianceOutcome.HIT
        assert status.provider_request_id == "MOCK-EVT-1"


class TestSanctionsListUpdatedWebhook:
    def test_flags_account_and_creates_action_item_for_open_engagement(
        self, api_client, db_session, tenant_id,
    ):
        account, engagement = create_account_and_engagement(
            db_session, tenant_id, Stage.NEGOTIATION,
        )
        db_session.commit()

        resp = api_client.post("/webhooks/sanctions-list-updated", json={
            "hits": [{
                "account_id": str(account.id), "matched_list": "OFAC_SDN",
                "matched_entity_name": "テスト株式会社",
            }],
        })
        assert resp.status_code == 200
        body = resp.json()
        # レスポンスには業務データ(対象アカウント・商談等)を含めない —
        # 通知はActionItemとしてCRM側に永続化する(webhooks.py参照)。
        assert body == {"status": "processed", "hits_processed": 1}

        status = db_session.query(ComplianceStatus).filter_by(
            tenant_id=tenant_id, account_id=account.id,
            check_type=ComplianceCheckType.SANCTIONS,
        ).one()
        assert status.outcome == ComplianceOutcome.HIT

        action_item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert action_item.assigned_to == "輸出管理チーム"
        assert "テスト株式会社" in action_item.reason
        assert action_item.written_by == "system:sanctions-webhook"

    def test_closed_engagements_do_not_get_an_action_item(
        self, api_client, db_session, tenant_id,
    ):
        account, engagement = create_account_and_engagement(
            db_session, tenant_id, Stage.CLOSED_WON,
        )
        db_session.commit()

        resp = api_client.post("/webhooks/sanctions-list-updated", json={
            "hits": [{
                "account_id": str(account.id), "matched_list": "OFAC_SDN",
                "matched_entity_name": "テスト株式会社",
            }],
        })
        assert resp.status_code == 200
        assert resp.json() == {"status": "processed", "hits_processed": 1}

        assert db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count() == 0
