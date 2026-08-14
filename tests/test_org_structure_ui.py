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

    def test_account_detail_shows_revenue_by_product(self, ui_client, db_session, tenant_id):
        account, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = Product(
            tenant_id=tenant_id, name="ドリルダウン商品", list_price=Decimal("100000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get(f"/ui/accounts/{account.id}")
        assert resp.status_code == 200
        assert "ドリルダウン商品" in resp.text
        assert "100,000" in resp.text

    def test_account_detail_rolls_up_children_revenue(self, ui_client, db_session, tenant_id):
        parent_account = Account(tenant_id=tenant_id, name="親会社")
        db_session.add(parent_account)
        db_session.flush()
        child_account, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        child_account.parent_account_id = parent_account.id
        product = Product(
            tenant_id=tenant_id, name="子会社経由商品", list_price=Decimal("50000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get(f"/ui/accounts/{parent_account.id}")
        assert resp.status_code == 200
        assert "子会社経由商品" in resp.text


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

    def test_report_shows_actual_currency_not_hardcoded_jpy(self, ui_client, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = Product(
            tenant_id=tenant_id, name="テスト商品", list_price=Decimal("100000"), currency="USD",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        eng.currency = "USD"
        db_session.commit()

        resp = ui_client.get("/ui/reports/revenue")
        assert resp.status_code == 200
        assert "100,000 USD" in resp.text
        assert "JPY" not in resp.text

    def test_report_shows_separate_totals_for_mixed_currencies(self, ui_client, db_session, tenant_id):
        _, eng_usd = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng_usd.currency = "USD"
        product_usd = Product(
            tenant_id=tenant_id, name="USD商品", list_price=Decimal("100000"), currency="USD",
        )
        db_session.add(product_usd)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng_usd, product=product_usd, quantity=1, discount_rate=Decimal("0"))

        _, eng_jpy = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product_jpy = Product(
            tenant_id=tenant_id, name="JPY商品", list_price=Decimal("50000"), currency="JPY",
        )
        db_session.add(product_jpy)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng_jpy, product=product_jpy, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/revenue")
        assert resp.status_code == 200
        assert "100,000 USD" in resp.text
        assert "50,000 JPY" in resp.text
        assert "複数の通貨が混在しています" in resp.text

    def test_report_links_to_drilldown_and_account(self, ui_client, db_session, tenant_id):
        account, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product_group = db_session.query(SalesGroup).filter_by(tenant_id=tenant_id).first()
        product = Product(
            tenant_id=tenant_id, name="テスト商品", list_price=Decimal("100000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/revenue")
        assert resp.status_code == 200
        assert "/ui/reports/revenue/drill-down?relationship_type=" in resp.text
        assert f"/ui/accounts/{account.id}" in resp.text


class TestRevenueDrilldownUi:
    def _seed_closed_won_deal(self, db_session, tenant_id, *, product_name="ドリル商品"):
        account, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = Product(
            tenant_id=tenant_id, name=product_name, list_price=Decimal("100000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()
        return account, eng

    def test_drilldown_renders_with_no_filter(self, ui_client, db_session, tenant_id):
        account, eng = self._seed_closed_won_deal(db_session, tenant_id)

        resp = ui_client.get("/ui/reports/revenue/drill-down")
        assert resp.status_code == 200
        assert "ドリル商品" in resp.text
        assert eng.name in resp.text
        assert account.name in resp.text

    def test_drilldown_filters_by_relationship_type(self, ui_client, db_session, tenant_id):
        self._seed_closed_won_deal(db_session, tenant_id, product_name="新規商品")

        resp = ui_client.get("/ui/reports/revenue/drill-down?relationship_type=new_business")
        assert resp.status_code == 200
        assert "新規商品" in resp.text

        resp_renewal = ui_client.get("/ui/reports/revenue/drill-down?relationship_type=renewal")
        assert resp_renewal.status_code == 200
        assert "該当する商談がありません" in resp_renewal.text

    def test_drilldown_filters_by_sales_group(self, ui_client, db_session, tenant_id):
        _, eng = self._seed_closed_won_deal(db_session, tenant_id, product_name="グループ限定商品")
        group = SalesGroup(tenant_id=tenant_id, name="ドリルダウングループ")
        db_session.add(group)
        db_session.flush()
        eng.sales_group_id = group.id
        db_session.commit()

        resp = ui_client.get(f"/ui/reports/revenue/drill-down?sales_group_id={group.id}")
        assert resp.status_code == 200
        assert "グループ限定商品" in resp.text
        assert "ドリルダウングループ" in resp.text

    def test_drilldown_renders_with_no_data(self, ui_client):
        resp = ui_client.get("/ui/reports/revenue/drill-down")
        assert resp.status_code == 200


class TestReportBuilderUi:
    def test_renders_with_no_data(self, ui_client):
        resp = ui_client.get("/ui/reports/builder")
        assert resp.status_code == 200

    def test_single_axis_shows_closed_won_only_by_default(self, ui_client, db_session, tenant_id):
        _, closed = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = Product(
            tenant_id=tenant_id, name="受注商品", list_price=Decimal("100000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, closed, product=product, quantity=1, discount_rate=Decimal("0"))

        _, pipeline = create_account_and_engagement(db_session, tenant_id, stage=Stage.PROPOSAL)
        pipeline_product = Product(
            tenant_id=tenant_id, name="未受注商品", list_price=Decimal("50000"), currency="JPY",
        )
        db_session.add(pipeline_product)
        db_session.flush()
        add_line_item(db_session, tenant_id, pipeline, product=pipeline_product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/builder?rows=product")
        assert resp.status_code == 200
        assert "受注商品" in resp.text
        assert "未受注商品" not in resp.text

    def test_scope_all_includes_pipeline(self, ui_client, db_session, tenant_id):
        _, pipeline = create_account_and_engagement(db_session, tenant_id, stage=Stage.PROPOSAL)
        product = Product(
            tenant_id=tenant_id, name="パイプライン商品", list_price=Decimal("50000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, pipeline, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/builder?rows=product&scope=all")
        assert resp.status_code == 200
        assert "パイプライン商品" in resp.text

    def test_two_axis_renders_pivot_table(self, ui_client, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = Product(
            tenant_id=tenant_id, name="ピボット商品", list_price=Decimal("100000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/builder?rows=product_group&cols=relationship_type")
        assert resp.status_code == 200
        assert "100,000" in resp.text

    def test_filters_by_stage(self, ui_client, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.NEGOTIATION)
        product = Product(
            tenant_id=tenant_id, name="交渉中商品", list_price=Decimal("70000"), currency="JPY",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        resp = ui_client.get("/ui/reports/builder?rows=product&scope=all&stage=negotiation")
        assert resp.status_code == 200
        assert "交渉中商品" in resp.text

        resp_other = ui_client.get("/ui/reports/builder?rows=product&scope=all&stage=proposal")
        assert resp_other.status_code == 200
        assert "交渉中商品" not in resp_other.text
