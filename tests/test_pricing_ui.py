"""商品(価格表) / 見積もり / 契約 UI(crm_mvp/api/web/products.py,
quotes.py, engagements.py の商品構成・見積・契約ルート)の統合テスト。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from tests.conftest import create_account_and_engagement

from crm_mvp.models import Product


def make_product(db_session, tenant_id, **overrides) -> Product:
    defaults = dict(
        tenant_id=tenant_id, name="検査装置 標準モデル", sku="INSP-100",
        list_price=Decimal("1000000"), currency="JPY",
    )
    defaults.update(overrides)
    product = Product(**defaults)
    db_session.add(product)
    db_session.flush()
    return product


class TestProductsPage:
    def test_lists_active_products(self, ui_client, db_session, tenant_id):
        make_product(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/products")
        assert resp.status_code == 200
        assert "検査装置 標準モデル" in resp.text

    def test_creates_product(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/products/new",
            data={"name": "保守サポート(年間)", "list_price": "200000", "currency": "JPY"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        product = db_session.query(Product).filter_by(
            tenant_id=tenant_id, name="保守サポート(年間)",
        ).one()
        assert product.list_price == Decimal("200000")

    def test_blank_name_is_rejected(self, ui_client, db_session):
        resp = ui_client.post(
            "/ui/products/new",
            data={"name": "  ", "list_price": "100"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_deactivate_product(self, ui_client, db_session, tenant_id):
        product = make_product(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(f"/ui/products/{product.id}/deactivate", follow_redirects=False)
        assert resp.status_code == 303

        db_session.refresh(product)
        assert product.is_active is False


class TestEngagementLineItems:
    def test_add_line_item_recalculates_amount(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "2", "discount_rate": "10"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.refresh(engagement)
        assert engagement.amount == Decimal("1800000.00")

    def test_manual_amount_rejected_when_line_items_exist(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/amount",
            data={"amount": "500", "currency": "JPY"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_remove_line_item_clears_amount(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )
        from crm_mvp.models import EngagementLineItem
        item = db_session.query(EngagementLineItem).filter_by(engagement_id=engagement.id).one()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items/{item.id}/remove",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.refresh(engagement)
        assert engagement.amount is None

    def test_engagement_detail_renders_line_items(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert "検査装置 標準モデル" in resp.text


class TestQuotesAndContracts:
    def test_create_quote_requires_line_items(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/quotes",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_create_quote_and_change_status(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )

        resp = ui_client.post(f"/ui/engagements/{engagement.id}/quotes", data={}, follow_redirects=False)
        assert resp.status_code == 303

        from crm_mvp.models import Quote
        quote = db_session.query(Quote).filter_by(engagement_id=engagement.id).one()
        assert quote.quote_number.startswith("Q-")

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/quotes/{quote.id}/status",
            data={"status": "sent"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(quote)
        assert quote.status == "sent"
        assert quote.issued_at is not None

    def test_create_contract_from_quote(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )
        ui_client.post(f"/ui/engagements/{engagement.id}/quotes", data={})

        from crm_mvp.models import Quote
        quote = db_session.query(Quote).filter_by(engagement_id=engagement.id).one()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/contracts",
            data={"quote_id": str(quote.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        from crm_mvp.models import Contract
        contract = db_session.query(Contract).filter_by(engagement_id=engagement.id).one()
        assert contract.contract_number.startswith("C-")
        assert contract.quote_id == quote.id

    def test_create_contract_without_line_items_fails(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/contracts",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_cross_engagement_quotes_list(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )
        ui_client.post(f"/ui/engagements/{engagement.id}/quotes", data={})

        resp = ui_client.get("/ui/quotes")
        assert resp.status_code == 200
        assert "Q-" in resp.text

    def test_default_view_shows_facet_tabs(self, ui_client):
        resp = ui_client.get("/ui/quotes")
        assert resp.status_code == 200
        assert "担当別" in resp.text
        assert "商品別" in resp.text
        assert "取引先別" in resp.text

    def test_dim_account_filters_to_selected_account(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import Account

        account_a = Account(tenant_id=tenant_id, name="フィルタA社")
        db_session.add(account_a)
        db_session.flush()
        _, engagement_a = create_account_and_engagement(db_session, tenant_id)
        engagement_a.account_id = account_a.id
        _, engagement_b = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        for eng in (engagement_a, engagement_b):
            ui_client.post(
                f"/ui/engagements/{eng.id}/line-items",
                data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
            )
            ui_client.post(f"/ui/engagements/{eng.id}/quotes", data={})

        resp = ui_client.get(f"/ui/quotes?dim=account&account_id={account_a.id}")
        assert resp.status_code == 200
        assert "見積もり(1件)" in resp.text

    def test_dim_product_filters_by_product_group(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import ProductGroup

        group_a = ProductGroup(tenant_id=tenant_id, name="フィルタグループA")
        group_b = ProductGroup(tenant_id=tenant_id, name="フィルタグループB")
        db_session.add_all([group_a, group_b])
        db_session.flush()
        product_a = make_product(db_session, tenant_id, name="商品A", product_group_id=group_a.id)
        product_b = make_product(db_session, tenant_id, name="商品B", product_group_id=group_b.id)
        _, engagement_a = create_account_and_engagement(db_session, tenant_id)
        _, engagement_b = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement_a.id}/line-items",
            data={"product_id": str(product_a.id), "quantity": "1", "discount_rate": "0"},
        )
        ui_client.post(f"/ui/engagements/{engagement_a.id}/quotes", data={})
        ui_client.post(
            f"/ui/engagements/{engagement_b.id}/line-items",
            data={"product_id": str(product_b.id), "quantity": "1", "discount_rate": "0"},
        )
        ui_client.post(f"/ui/engagements/{engagement_b.id}/quotes", data={})

        resp = ui_client.get(f"/ui/quotes?dim=product&product_group_id={group_a.id}")
        assert resp.status_code == 200
        assert "見積もり(1件)" in resp.text

    def test_status_section_groups_quotes(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/line-items",
            data={"product_id": str(product.id), "quantity": "1", "discount_rate": "0"},
        )
        ui_client.post(f"/ui/engagements/{engagement.id}/quotes", data={})

        resp = ui_client.get("/ui/quotes")
        assert resp.status_code == 200
        assert "下書き" in resp.text
