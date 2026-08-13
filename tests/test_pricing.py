"""pricing.py のユニットテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crm_mvp.models import ErpMaterial, Product
from crm_mvp.services import pricing as pr

from .conftest import create_account_and_engagement


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


def make_erp_material(db_session, tenant_id, **overrides) -> ErpMaterial:
    defaults = dict(
        tenant_id=tenant_id, material_code="MAT-0001", description="検査装置 標準モデル",
        material_type="FERT", base_unit="PC", standard_price=Decimal("700000"),
        currency="JPY",
    )
    defaults.update(overrides)
    material = ErpMaterial(**defaults)
    db_session.add(material)
    db_session.flush()
    return material


class TestComputeUnitPrice:
    def test_zero_discount_equals_list_price(self):
        assert pr.compute_unit_price(Decimal("1000000"), Decimal("0")) == Decimal("1000000.00")

    def test_ten_percent_discount(self):
        assert pr.compute_unit_price(Decimal("1000000"), Decimal("10")) == Decimal("900000.00")

    def test_rounds_to_two_places(self):
        result = pr.compute_unit_price(Decimal("1000"), Decimal("33"))
        assert result == Decimal("670.00")


class TestAddLineItem:
    def test_adds_item_and_recalculates_engagement_amount(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)

        item = pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=2, discount_rate=Decimal("10"),
        )
        db_session.commit()

        assert item.unit_price == Decimal("900000.00")
        assert item.line_total == Decimal("1800000.00")
        assert engagement.amount == Decimal("1800000.00")
        assert engagement.currency == "JPY"

    def test_multiple_items_sum_into_amount(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product_a = make_product(db_session, tenant_id, name="A", list_price=Decimal("500000"))
        product_b = make_product(db_session, tenant_id, name="B", list_price=Decimal("300000"))

        pr.add_line_item(
            db_session, tenant_id, engagement, product=product_a,
            quantity=1, discount_rate=Decimal("0"),
        )
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product_b,
            quantity=3, discount_rate=Decimal("0"),
        )
        db_session.commit()

        assert engagement.amount == Decimal("500000.00") + Decimal("900000.00")

    def test_rejects_invalid_quantity(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        with pytest.raises(ValueError):
            pr.add_line_item(
                db_session, tenant_id, engagement, product=product,
                quantity=0, discount_rate=Decimal("0"),
            )

    def test_rejects_out_of_range_discount(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        with pytest.raises(ValueError):
            pr.add_line_item(
                db_session, tenant_id, engagement, product=product,
                quantity=1, discount_rate=Decimal("150"),
            )


class TestRemoveLineItem:
    def test_removing_last_item_clears_amount(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        item = pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()
        assert engagement.amount is not None

        pr.remove_line_item(db_session, tenant_id, engagement, item)
        db_session.commit()

        assert engagement.amount is None

    def test_removing_one_of_two_leaves_remaining_total(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id, list_price=Decimal("100000"))
        item1 = pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=2, discount_rate=Decimal("0"),
        )
        db_session.flush()
        assert engagement.amount == Decimal("300000.00")

        pr.remove_line_item(db_session, tenant_id, engagement, item1)
        db_session.commit()

        assert engagement.amount == Decimal("200000.00")


class TestComputeGrossMarginRate:
    def test_computes_margin_from_linked_erp_material(self, db_session, tenant_id):
        material = make_erp_material(db_session, tenant_id, standard_price=Decimal("700000"))
        product = make_product(db_session, tenant_id, list_price=Decimal("1000000"))
        product.erp_material = material
        db_session.flush()

        assert pr.compute_gross_margin_rate(product) == Decimal("30.00")

    def test_none_when_no_erp_material_linked(self, db_session, tenant_id):
        product = make_product(db_session, tenant_id)
        assert pr.compute_gross_margin_rate(product) is None

    def test_none_when_currency_mismatch(self, db_session, tenant_id):
        material = make_erp_material(db_session, tenant_id, currency="USD")
        product = make_product(db_session, tenant_id, currency="JPY")
        product.erp_material = material
        db_session.flush()

        assert pr.compute_gross_margin_rate(product) is None


class TestListLineItems:
    def test_scoped_to_engagement(self, db_session, tenant_id):
        _, engagement_a = create_account_and_engagement(db_session, tenant_id)
        _, engagement_b = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)

        pr.add_line_item(
            db_session, tenant_id, engagement_a, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.commit()

        assert len(pr.list_line_items(db_session, tenant_id, engagement_a.id)) == 1
        assert len(pr.list_line_items(db_session, tenant_id, engagement_b.id)) == 0
