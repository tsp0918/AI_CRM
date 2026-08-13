"""services/revenue_report.py のドリルダウン機能(line_item_facts/
filter_facts/facts_by_engagement)と、関連するUIのテスト。"""

from __future__ import annotations

from decimal import Decimal

from tests.conftest import create_account_and_engagement

from crm_mvp.enums import EngagementRelationshipType, Stage
from crm_mvp.models import Product, ProductGroup, SalesGroup
from crm_mvp.services import revenue_report as rr
from crm_mvp.services.engagement_relationships import create_child_engagement
from crm_mvp.services.pricing import add_line_item


def make_product(db_session, tenant_id, **overrides) -> Product:
    defaults = dict(
        tenant_id=tenant_id, name="テスト商品", list_price=Decimal("100000"), currency="JPY",
    )
    defaults.update(overrides)
    product = Product(**defaults)
    db_session.add(product)
    db_session.flush()
    return product


class TestLineItemFacts:
    def test_returns_one_fact_per_line_item(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        group = ProductGroup(tenant_id=tenant_id, name="グループA")
        db_session.add(group)
        db_session.flush()
        product = make_product(db_session, tenant_id, product_group_id=group.id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=2, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.line_item_facts(db_session, tenant_id)
        assert len(facts) == 1
        assert facts[0]["product"].id == product.id
        assert facts[0]["product_group"].id == group.id
        assert facts[0]["amount"] == Decimal("200000.00")

    def test_empty_when_no_closed_won(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, stage=Stage.PROSPECT)
        db_session.commit()
        assert rr.line_item_facts(db_session, tenant_id) == []


class TestFilterFacts:
    def test_filters_by_product_group(self, db_session, tenant_id):
        _, eng1 = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        _, eng2 = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        group_a = ProductGroup(tenant_id=tenant_id, name="A")
        group_b = ProductGroup(tenant_id=tenant_id, name="B")
        db_session.add_all([group_a, group_b])
        db_session.flush()
        product_a = make_product(db_session, tenant_id, name="PA", product_group_id=group_a.id)
        product_b = make_product(db_session, tenant_id, name="PB", product_group_id=group_b.id)
        add_line_item(db_session, tenant_id, eng1, product=product_a, quantity=1, discount_rate=Decimal("0"))
        add_line_item(db_session, tenant_id, eng2, product=product_b, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.line_item_facts(db_session, tenant_id)
        filtered = rr.filter_facts(facts, product_group_id=group_a.id)
        assert len(filtered) == 1
        assert filtered[0]["product"].name == "PA"

    def test_filters_by_relationship_type(self, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, parent, product=product, quantity=1, discount_rate=Decimal("0"))

        child = create_child_engagement(
            db_session, tenant_id, parent,
            relationship_type=EngagementRelationshipType.RENEWAL, name="更新商談",
        )
        child.stage = Stage.CLOSED_WON
        add_line_item(db_session, tenant_id, child, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.line_item_facts(db_session, tenant_id)
        renewals = rr.filter_facts(facts, relationship_type="renewal")
        assert len(renewals) == 1
        assert renewals[0]["engagement"].id == child.id

    def test_filters_by_sales_group(self, db_session, tenant_id):
        _, eng1 = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        _, eng2 = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        sg = SalesGroup(tenant_id=tenant_id, name="テストグループ")
        db_session.add(sg)
        db_session.flush()
        eng1.sales_group_id = sg.id
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng1, product=product, quantity=1, discount_rate=Decimal("0"))
        add_line_item(db_session, tenant_id, eng2, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.line_item_facts(db_session, tenant_id)
        filtered = rr.filter_facts(facts, sales_group_id=sg.id)
        assert len(filtered) == 1
        assert filtered[0]["engagement"].id == eng1.id


class TestFactsByEngagement:
    def test_sums_multiple_line_items_per_engagement(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product_a = make_product(db_session, tenant_id, name="PA", list_price=Decimal("10000"))
        product_b = make_product(db_session, tenant_id, name="PB", list_price=Decimal("5000"))
        add_line_item(db_session, tenant_id, eng, product=product_a, quantity=1, discount_rate=Decimal("0"))
        add_line_item(db_session, tenant_id, eng, product=product_b, quantity=2, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.line_item_facts(db_session, tenant_id)
        deals = rr.facts_by_engagement(facts)
        assert len(deals) == 1
        assert deals[0]["amount"] == Decimal("20000.00")


class TestAggregateByWithId:
    def test_includes_id_when_id_fn_given(self):
        rows = [{"amount": Decimal("100"), "key": "a", "id": 1}]
        result = rr.aggregate_by(rows, lambda r: r["key"], lambda r: r["id"])
        assert result[0]["id"] == 1

    def test_id_none_when_no_id_fn(self):
        rows = [{"amount": Decimal("100"), "key": "a"}]
        result = rr.aggregate_by(rows, lambda r: r["key"])
        assert result[0]["id"] is None
