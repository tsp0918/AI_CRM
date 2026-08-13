"""継続/Upsell/Cross-sell と契約更新管理UIの統合テスト。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import create_account_and_engagement

from crm_mvp.enums import ContractStatus, Stage
from crm_mvp.models import Contract, Engagement


def make_contract(db_session, tenant_id, engagement, **overrides) -> Contract:
    defaults = dict(
        tenant_id=tenant_id, engagement_id=engagement.id, contract_number="C-TEST-0001",
        status=ContractStatus.ACTIVE, total_amount=Decimal("1000000"), currency="JPY",
        written_by="human:tester",
    )
    defaults.update(overrides)
    contract = Contract(**defaults)
    db_session.add(contract)
    db_session.flush()
    return contract


class TestChildEngagementCreation:
    def test_creates_child_and_redirects_to_it(self, ui_client, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{parent.id}/child-engagements",
            data={"name": "テスト案件(更新)", "relationship_type": "renewal"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/ui/engagements/" in resp.headers["location"]

        child = db_session.query(Engagement).filter_by(
            tenant_id=tenant_id, name="テスト案件(更新)",
        ).one()
        assert child.parent_engagement_id == parent.id
        assert child.relationship_type == "renewal"

    def test_engagement_detail_shows_child(self, ui_client, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{parent.id}/child-engagements",
            data={"name": "Upsell案件A", "relationship_type": "upsell"},
        )

        resp = ui_client.get(f"/ui/engagements/{parent.id}")
        assert resp.status_code == 200
        assert "Upsell案件A" in resp.text

    def test_child_detail_shows_parent_link(self, ui_client, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{parent.id}/child-engagements",
            data={"name": "Cross-sell案件B", "relationship_type": "cross_sell"},
        )
        child = db_session.query(Engagement).filter_by(
            tenant_id=tenant_id, name="Cross-sell案件B",
        ).one()

        resp = ui_client.get(f"/ui/engagements/{child.id}")
        assert resp.status_code == 200
        assert parent.name in resp.text
        assert "Cross-sell" in resp.text

    def test_blank_name_is_rejected(self, ui_client, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{parent.id}/child-engagements",
            data={"name": "  ", "relationship_type": "renewal"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestRenewalsPage:
    def test_lists_contract_nearing_end_date(self, ui_client, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        make_contract(db_session, tenant_id, eng, end_date=date.today() + timedelta(days=20))
        db_session.commit()

        resp = ui_client.get("/ui/renewals")
        assert resp.status_code == 200
        assert eng.name in resp.text

    def test_start_renewal_creates_child_and_removes_from_list(
        self, ui_client, db_session, tenant_id,
    ):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        contract = make_contract(
            db_session, tenant_id, eng, end_date=date.today() + timedelta(days=20),
        )
        db_session.commit()

        resp = ui_client.post(f"/ui/renewals/{contract.id}/start", follow_redirects=False)
        assert resp.status_code == 303

        child = db_session.query(Engagement).filter_by(
            tenant_id=tenant_id, parent_engagement_id=eng.id,
        ).one()
        assert child.relationship_type == "renewal"

        resp = ui_client.get("/ui/renewals")
        assert eng.name not in resp.text
