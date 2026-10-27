"""アカウントのコンプライアンスチェック起票と Webhook 受信のテスト
(HANDOVER.md §5 Phase5 item19-21)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from crm_mvp.enums import (
    ArtifactType, ComplianceCheckType, ComplianceOutcome, ReviewType, Stage,
)
from crm_mvp.models import Account, ActionItem, ComplianceStatus, ReviewCase

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

    def test_sanctions_check_enqueues_aitm_party_resolve(
        self, api_client, db_session, tenant_id,
    ):
        """2026-10-27(契約書§4-1 A案): 取引先にサンクション系チェックを
        初めて走らせるタイミングでparty解決をAI_TMに依頼する(B-3の依存:
        party.linkedが来るまでAccount.aitm_party_idが埋まらずscreening.alertの
        取引先解決が成立しないため)。"""
        from crm_mvp.models import OutboxMessage

        account, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        api_client.post(
            f"/accounts/{account.id}/compliance-checks",
            json={"check_type": "sanctions"},
        )

        message = db_session.query(OutboxMessage).filter_by(
            tenant_id=tenant_id, kind="aitm.party.resolve",
        ).one()
        assert message.payload["external_id"] == str(account.id)
        assert message.payload["legal_name"] == account.name
        assert message.payload["enable_watch"] is True

    def test_credit_check_does_not_enqueue_party_resolve(
        self, api_client, db_session, tenant_id,
    ):
        """SANCTIONS以外のチェック種別ではparty解決は不要。"""
        from crm_mvp.models import OutboxMessage

        account, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        api_client.post(
            f"/accounts/{account.id}/compliance-checks",
            json={"check_type": "credit"},
        )

        assert db_session.query(OutboxMessage).filter_by(
            tenant_id=tenant_id, kind="aitm.party.resolve",
        ).count() == 0

    def test_does_not_enqueue_again_once_aitm_party_id_known(
        self, api_client, db_session, tenant_id,
    ):
        from crm_mvp.models import OutboxMessage

        account, _ = create_account_and_engagement(db_session, tenant_id)
        account.aitm_party_id = "PLAT-PARTY-001"
        db_session.commit()

        api_client.post(
            f"/accounts/{account.id}/compliance-checks",
            json={"check_type": "sanctions"},
        )

        assert db_session.query(OutboxMessage).filter_by(
            tenant_id=tenant_id, kind="aitm.party.resolve",
        ).count() == 0


class TestComplianceJudgmentWebhook:
    BEARER = "aitm-compliance-bearer"
    SECRET = "aitm-compliance-secret"

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AITM_COMPLIANCE_WEBHOOK_BEARER", self.BEARER)
        monkeypatch.setenv("AITM_COMPLIANCE_WEBHOOK_SECRET", self.SECRET)

    def test_upserts_compliance_status(self, api_client, db_session, tenant_id):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/compliance-judgment", {
                "account_id": str(account.id), "check_type": "export_control",
                "outcome": "hit", "provider": "ai_tm",
                "provider_request_id": "MOCK-EVT-1",
            },
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 204

        status = db_session.query(ComplianceStatus).filter_by(
            tenant_id=tenant_id, account_id=account.id,
            check_type=ComplianceCheckType.EXPORT_CONTROL,
        ).one()
        assert status.outcome == ComplianceOutcome.HIT
        assert status.provider_request_id == "MOCK-EVT-1"


class TestSanctionsListUpdatedWebhook:
    BEARER = "aitm-sanctions-bearer"
    SECRET = "aitm-sanctions-secret"

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AITM_SANCTIONS_WEBHOOK_BEARER", self.BEARER)
        monkeypatch.setenv("AITM_SANCTIONS_WEBHOOK_SECRET", self.SECRET)

    def test_flags_account_and_creates_action_item_for_open_engagement(
        self, api_client, db_session, tenant_id,
    ):
        account, engagement = create_account_and_engagement(
            db_session, tenant_id, Stage.NEGOTIATION,
        )
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/sanctions-list-updated", {
                "hits": [{
                    "account_id": str(account.id), "matched_list": "OFAC_SDN",
                    "matched_entity_name": "テスト株式会社",
                }],
            },
            bearer=self.BEARER, secret=self.SECRET,
        )
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

        resp = _signed_post(
            api_client, "/webhooks/sanctions-list-updated", {
                "hits": [{
                    "account_id": str(account.id), "matched_list": "OFAC_SDN",
                    "matched_entity_name": "テスト株式会社",
                }],
            },
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "processed", "hits_processed": 1}


def _signed_post(api_client, path: str, payload: dict, *, bearer: str, secret: str):
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(
        secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256,
    ).hexdigest()
    return api_client.post(
        path, content=body,
        headers={
            "Authorization": f"Bearer {bearer}", "X-Signature": sig,
            "X-Timestamp": ts, "Content-Type": "application/json",
        },
    )


class TestAitmReviewResultWebhook:
    BEARER = "aitm-review-bearer"
    SECRET = "aitm-review-secret"

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AITM_REVIEW_WEBHOOK_BEARER", self.BEARER)
        monkeypatch.setenv("AITM_REVIEW_WEBHOOK_SECRET", self.SECRET)

    def _make_review_case(self, db_session, tenant_id, engagement, **overrides) -> ReviewCase:
        # AI_TMはcase_noとして自分の採番値(provider_request_id)を送ってくる
        # (receive_aitm_review_resultはprovider_request_idで引く、2026-10-27
        # の取り違え修正)。テストでも実際の送受信契約と同じ値を使う。
        defaults = dict(
            tenant_id=tenant_id, case_no="CRM-Q-2026-0001",
            provider_request_id="CRM-PROV-000001",
            review_type=ReviewType.PROVISIONAL, artifact_type=ArtifactType.QUOTE,
            engagement_id=engagement.id, review_key_hash="deadbeef",
            status="pending", revision=0,
        )
        defaults.update(overrides)
        case = ReviewCase(**defaults)
        db_session.add(case)
        db_session.flush()
        return case

    def test_invalid_signature_is_rejected(self, api_client, db_session, tenant_id):
        resp = _signed_post(
            api_client, "/webhooks/aitm/review-result", {"case_no": "x", "revision": 1},
            bearer=self.BEARER, secret="wrong-secret",
        )
        assert resp.status_code == 401

    def test_clear_judgment_updates_review_case_without_action_item(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement)
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/aitm/review-result",
            {"case_no": case.provider_request_id, "revision": 1, "status": "clear",
             "valid_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()},
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "processed"}

        db_session.refresh(case)
        assert case.status == "clear"
        assert case.revision == 1
        assert case.decided_at is not None
        assert db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count() == 0

    def test_hit_judgment_creates_action_item(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement)
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/aitm/review-result",
            {"case_no": case.provider_request_id, "revision": 1, "status": "hit"},
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200

        db_session.refresh(case)
        assert case.status == "hit"
        action_item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert action_item.assigned_to == "輸出管理チーム"
        assert case.provider_request_id in action_item.reason

    def test_duplicate_event_is_a_no_op(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement)
        db_session.commit()

        payload = {"case_no": case.provider_request_id, "revision": 1, "status": "hit"}
        first = _signed_post(
            api_client, "/webhooks/aitm/review-result", payload,
            bearer=self.BEARER, secret=self.SECRET,
        )
        second = _signed_post(
            api_client, "/webhooks/aitm/review-result", payload,
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == {"status": "duplicate"}
        assert db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count() == 1  # 2回目で重複起票されていない

    def test_stale_revision_is_discarded(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement, revision=5, status="clear")
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/aitm/review-result",
            {"case_no": case.provider_request_id, "revision": 2, "status": "hit", "event_id": "stale-evt"},
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200

        db_session.refresh(case)
        assert case.status == "clear"  # 古いrevisionは適用されない
        assert case.revision == 5

    def test_unknown_case_no_does_not_error(self, api_client, db_session, tenant_id):
        resp = _signed_post(
            api_client, "/webhooks/aitm/review-result",
            {"case_no": "CRM-Q-9999", "revision": 1, "status": "hit", "event_id": "unknown-evt"},
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "processed"}

    def test_crm_case_no_does_not_match_lookup(self, api_client, db_session, tenant_id):
        """2026-10-27の取り違え修正の回帰テスト: AI_TMはprovider_request_id
        (AI_TM採番値)をcase_noとして送る。CRM採番のcase_no列(fallback無し)
        を送っても該当ReviewCaseが見つかってはならない。"""
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement)
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/aitm/review-result",
            {"case_no": case.case_no, "revision": 1, "status": "clear", "event_id": "crm-case-no-evt"},
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200

        db_session.refresh(case)
        assert case.status == "pending"  # 誤ってマッチして更新されていない
        assert case.revision == 0


class TestOverrideAppliedWebhook:
    BEARER = "dev-crm-outbound-bearer-000"
    SECRET = "dev-crm-signing-secret-000"

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AITM_OVERRIDE_WEBHOOK_BEARER", self.BEARER)
        monkeypatch.setenv("AITM_OVERRIDE_WEBHOOK_SECRET", self.SECRET)

    def _make_review_case(self, db_session, tenant_id, engagement, **overrides) -> ReviewCase:
        defaults = dict(
            tenant_id=tenant_id, case_no="CRM-C-2026-0001",
            provider_request_id="CRM-FORMAL-000001",
            review_type=ReviewType.FORMAL, artifact_type=ArtifactType.CONTRACT,
            engagement_id=engagement.id, review_key_hash="deadbeef",
            status="needs_review", revision=1,
        )
        defaults.update(overrides)
        case = ReviewCase(**defaults)
        db_session.add(case)
        db_session.flush()
        return case

    def test_override_applied_clears_case_by_provider_request_id(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement)
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/override-applied",
            {
                "case_no": case.provider_request_id, "transaction_id": "T-1",
                "override_id": 5000, "scope": "single_transaction",
                "overridden_by": "officer-1", "approver_name": "部長 山田",
                "valid_until": "2027-03-31", "department_head_approval": True,
            },
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200

        db_session.refresh(case)
        assert case.status == "clear"
        assert case.detail["override"]["approver_name"] == "部長 山田"
        action_item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert case.provider_request_id in action_item.reason

    def test_crm_case_no_does_not_match_lookup(self, api_client, db_session, tenant_id):
        """IF-10と同じ取り違えがIF-15にも存在した。回帰テスト。"""
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_review_case(db_session, tenant_id, engagement)
        db_session.commit()

        resp = _signed_post(
            api_client, "/webhooks/override-applied",
            {"case_no": case.case_no, "override_id": 1},
            bearer=self.BEARER, secret=self.SECRET,
        )
        assert resp.status_code == 200

        db_session.refresh(case)
        assert case.status == "needs_review"  # 誤ってマッチして更新されていない
