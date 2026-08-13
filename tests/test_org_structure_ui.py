"""取引先ディレクトリ・セールスグループ・売上レポートUIの統合テスト。"""

from __future__ import annotations

from decimal import Decimal

from tests.conftest import create_account_and_engagement

from crm_mvp.enums import Stage
from crm_mvp.models import Account, Engagement, Lead, Product, SalesGroup
from crm_mvp.services.pricing import add_line_item


class TestAccountsUi:
    def test_create_grouping_account(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/accounts/new", data={"name": "NSC Group"}, follow_redirects=False,
        )
        assert resp.status_code == 303

        account = db_session.query(Account).filter_by(tenant_id=tenant_id, name="NSC Group").one()
        assert account.parent_account_id is None

    def test_set_parent_account(self, ui_client, db_session, tenant_id):
        ui_client.post("/ui/accounts/new", data={"name": "親"})
        parent = db_session.query(Account).filter_by(tenant_id=tenant_id, name="親").one()
        child, _ = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/accounts/{child.id}/parent",
            data={"parent_account_id": str(parent.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(child)
        assert child.parent_account_id == parent.id

    def test_account_detail_shows_engagements(self, ui_client, db_session, tenant_id):
        account, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/accounts/{account.id}")
        assert resp.status_code == 200
        assert engagement.name in resp.text

    def test_account_detail_shows_matched_leads(self, ui_client, db_session, tenant_id):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        lead = Lead(
            tenant_id=tenant_id, company_name="テスト企業", full_name="鈴木一郎",
            matched_account_id=account.id, written_by="human:tester",
        )
        db_session.add(lead)
        db_session.commit()

        resp = ui_client.get(f"/ui/accounts/{account.id}")
        assert resp.status_code == 200
        assert "鈴木一郎" in resp.text


class TestLeadAccountMatching:
    def test_set_matched_account(self, ui_client, db_session, tenant_id):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        lead = Lead(
            tenant_id=tenant_id, company_name="テスト企業", full_name="山田太郎",
            written_by="human:tester",
        )
        db_session.add(lead)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/account",
            data={"account_id": str(account.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(lead)
        assert lead.matched_account_id == account.id

    def test_lead_detail_shows_matched_account(self, ui_client, db_session, tenant_id):
        account, _ = create_account_and_engagement(db_session, tenant_id)
        lead = Lead(
            tenant_id=tenant_id, company_name="テスト企業", full_name="山田太郎",
            matched_account_id=account.id, written_by="human:tester",
        )
        db_session.add(lead)
        db_session.commit()

        resp = ui_client.get(f"/ui/leads/{lead.id}")
        assert resp.status_code == 200
        assert account.name in resp.text


class TestSalesGroupsUi:
    def test_create_sales_group(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/sales-groups/new", data={"name": "東日本営業部"}, follow_redirects=False,
        )
        assert resp.status_code == 303

        group = db_session.query(SalesGroup).filter_by(
            tenant_id=tenant_id, name="東日本営業部",
        ).one()
        assert group.parent_group_id is None

    def test_list_page_renders(self, ui_client, db_session, tenant_id):
        ui_client.post("/ui/sales-groups/new", data={"name": "西日本営業部"})
        resp = ui_client.get("/ui/sales-groups")
        assert resp.status_code == 200
        assert "西日本営業部" in resp.text


class TestEngagementSalesGroup:
    def test_new_engagement_with_sales_group(self, ui_client, db_session, tenant_id):
        ui_client.post("/ui/sales-groups/new", data={"name": "東日本営業部"})
        group = db_session.query(SalesGroup).filter_by(
            tenant_id=tenant_id, name="東日本営業部",
        ).one()

        resp = ui_client.post(
            "/ui/engagements/new",
            data={
                "account_name": "テスト商事", "engagement_name": "テスト案件",
                "sales_group_id": str(group.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        engagement = db_session.query(Engagement).filter_by(
            tenant_id=tenant_id, name="テスト案件",
        ).one()
        assert engagement.sales_group_id == group.id

    def test_update_sales_group_on_existing_engagement(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        ui_client.post("/ui/sales-groups/new", data={"name": "西日本営業部"})
        group = db_session.query(SalesGroup).filter_by(
            tenant_id=tenant_id, name="西日本営業部",
        ).one()
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/sales-group",
            data={"sales_group_id": str(group.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(engagement)
        assert engagement.sales_group_id == group.id


class TestRevenueReportUi:
    def test_report_renders_with_data(self, ui_client, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = Product(
            tenant_id=tenant_id, name="テスト商品", list_price=Decimal("100000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/revenue")
        assert resp.status_code == 200
        assert "100,000" in resp.text

    def test_report_renders_with_no_data(self, ui_client):
        resp = ui_client.get("/ui/reports/revenue")
        assert resp.status_code == 200
