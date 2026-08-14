"""services/revenue_report.py のユニットテスト。"""

from __future__ import annotations

from decimal import Decimal

from tests.conftest import create_account_and_engagement

from crm_mvp.enums import EngagementRelationshipType, Stage
from crm_mvp.models import Product, ProductGroup
from crm_mvp.services import account_hierarchy as ah
from crm_mvp.services import revenue_report as rr
from crm_mvp.services import sales_groups as sg
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.quoting import create_contract, create_quote_from_engagement


def make_product(db_session, tenant_id, **overrides) -> Product:
    defaults = dict(
        tenant_id=tenant_id, name="テスト商品", list_price=Decimal("100000"), currency="JPY",
    )
    defaults.update(overrides)
    product = Product(**defaults)
    db_session.add(product)
    db_session.flush()
    return product


def make_group(db_session, tenant_id, name) -> ProductGroup:
    group = ProductGroup(tenant_id=tenant_id, name=name)
    db_session.add(group)
    db_session.flush()
    return group


class TestClosedWonRevenueRows:
    def test_uses_contract_total_amount_when_no_realized_amount(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=2, discount_rate=Decimal("0"))
        quote = create_quote_from_engagement(db_session, tenant_id, eng, valid_until=None, actor="human:t")
        create_contract(db_session, tenant_id, eng, quote=quote, actor="human:t")
        db_session.commit()

        rows = rr.closed_won_revenue_rows(db_session, tenant_id)
        assert len(rows) == 1
        assert rows[0]["amount"] == Decimal("200000.00")
        assert rows[0]["has_contract"] is True

    def test_prefers_realized_amount_over_total_amount(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        quote = create_quote_from_engagement(db_session, tenant_id, eng, valid_until=None, actor="human:t")
        contract = create_contract(db_session, tenant_id, eng, quote=quote, actor="human:t")
        contract.realized_amount = Decimal("55000")
        db_session.commit()

        rows = rr.closed_won_revenue_rows(db_session, tenant_id)
        assert rows[0]["amount"] == Decimal("55000")

    def test_falls_back_to_engagement_amount_without_contract(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        rows = rr.closed_won_revenue_rows(db_session, tenant_id)
        assert rows[0]["has_contract"] is False
        assert rows[0]["amount"] == eng.amount

    def test_rolls_up_account_to_root(self, db_session, tenant_id):
        root = ah.create_grouping_account(db_session, tenant_id, name="親グループ")
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng_account = db_session.get(type(root), eng.account_id)
        ah.set_parent_account(db_session, tenant_id, eng_account, root.id)
        db_session.commit()

        rows = rr.closed_won_revenue_rows(db_session, tenant_id)
        assert rows[0]["root_account"].id == root.id

    def test_relationship_type_defaults_to_new_business(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.commit()

        rows = rr.closed_won_revenue_rows(db_session, tenant_id)
        assert rows[0]["relationship_type"] == "new_business"

    def test_row_includes_engagement_currency(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng.currency = "USD"
        db_session.commit()

        rows = rr.closed_won_revenue_rows(db_session, tenant_id)
        assert rows[0]["currency"] == "USD"


class TestTotalsByCurrency:
    def test_single_currency_sums_to_one_entry(self):
        rows = [
            {"amount": Decimal("100"), "currency": "USD"},
            {"amount": Decimal("50"), "currency": "USD"},
        ]
        result = rr.totals_by_currency(rows)
        assert result == [("USD", Decimal("150"))]

    def test_mixed_currencies_kept_separate_not_summed_together(self):
        rows = [
            {"amount": Decimal("100"), "currency": "USD"},
            {"amount": Decimal("200000"), "currency": "JPY"},
            {"amount": Decimal("50"), "currency": "USD"},
        ]
        result = rr.totals_by_currency(rows)
        by_currency = dict(result)
        assert by_currency["USD"] == Decimal("150")
        assert by_currency["JPY"] == Decimal("200000")
        # 金額降順(この場合はJPYの200000が先頭)
        assert result[0][0] == "JPY"

    def test_missing_currency_falls_back_to_jpy(self):
        rows = [{"amount": Decimal("10")}]
        result = rr.totals_by_currency(rows)
        assert result == [("JPY", Decimal("10"))]


class TestAggregateBy:
    def test_groups_and_sums(self):
        rows = [
            {"amount": Decimal("100")}, {"amount": Decimal("50")}, {"amount": Decimal("30")},
        ]
        labels = iter(["A", "A", "B"])
        result = rr.aggregate_by(rows, lambda r: next(labels))
        by_label = {r["label"]: r for r in result}
        assert by_label["A"]["amount"] == Decimal("150")
        assert by_label["A"]["count"] == 2
        assert by_label["B"]["amount"] == Decimal("30")


class TestProductGroupRevenue:
    def test_sums_line_items_by_group(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        group = make_group(db_session, tenant_id, "テストグループ")
        product = make_product(db_session, tenant_id, product_group_id=group.id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=3, discount_rate=Decimal("0"))
        db_session.commit()

        result = rr.product_group_revenue(db_session, tenant_id)
        assert len(result) == 1
        assert result[0]["label"] == "テストグループ"
        assert result[0]["amount"] == Decimal("300000.00")

    def test_uncategorized_product_shown_as_unclassified(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        result = rr.product_group_revenue(db_session, tenant_id)
        assert result[0]["label"] == "未分類"
