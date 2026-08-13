"""services/revenue_report.py のドリルダウン機能(line_item_facts/
filter_facts/facts_by_engagement)と、関連するUIのテスト。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import create_account_and_engagement

from crm_mvp.enums import EngagementRelationshipType, Stage
from crm_mvp.models import Product, ProductGroup, SalesGroup, User
from crm_mvp.services import revenue_report as rr
from crm_mvp.services.engagement_relationships import create_child_engagement
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.stage_transitions import apply_stage_transition


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


class TestAllStageLineItemFacts:
    def test_includes_pipeline_deals(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.QUALIFIED)
        product = make_product(db_session, tenant_id, name="パイプライン商品")
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        assert rr.line_item_facts(db_session, tenant_id) == []
        all_facts = rr.all_stage_line_item_facts(db_session, tenant_id)
        assert len(all_facts) == 1
        assert all_facts[0]["stage"] == Stage.QUALIFIED
        assert all_facts[0]["product"].name == "パイプライン商品"

    def test_period_date_from_stage_transition_for_closed_won(self, db_session, tenant_id):
        from datetime import datetime, timezone

        from crm_mvp.models import StageTransition

        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.add(StageTransition(
            tenant_id=tenant_id, engagement_id=eng.id,
            from_stage=Stage.NEGOTIATION, to_stage=Stage.CLOSED_WON,
            occurred_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
            written_by="human:tester",
        ))
        db_session.commit()

        facts = rr.all_stage_line_item_facts(db_session, tenant_id)
        assert facts[0]["period_date"] == date(2026, 3, 15)

    def test_period_date_uses_expected_close_date_for_pipeline(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.PROPOSAL)
        eng.expected_close_date = date.today() + timedelta(days=30)
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.all_stage_line_item_facts(db_session, tenant_id)
        assert facts[0]["period_date"] == eng.expected_close_date

    def test_owner_user_resolved(self, db_session, tenant_id):
        user = User(
            tenant_id=tenant_id, name="担当 太郎", email="tanaka@example.com",
            function="Sales", role="BDM",
        )
        db_session.add(user)
        db_session.flush()
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng.owner_user_id = user.id
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        db_session.commit()

        facts = rr.all_stage_line_item_facts(db_session, tenant_id)
        assert facts[0]["owner_user"].id == user.id


class TestBuildDimensions:
    def test_period_label_month(self):
        dims = rr.build_dimensions(period_granularity="month")
        fact = {"period_date": date(2026, 3, 15)}
        assert dims["period"].key_fn(fact) == "2026-03"

    def test_period_label_quarter(self):
        dims = rr.build_dimensions(period_granularity="quarter")
        fact = {"period_date": date(2026, 8, 1)}
        assert dims["period"].key_fn(fact) == "2026-Q3"

    def test_period_label_none(self):
        dims = rr.build_dimensions()
        assert dims["period"].key_fn({"period_date": None}) == "未設定"

    def test_stage_dimension_uses_report_labels(self):
        dims = rr.build_dimensions()
        assert dims["stage"].key_fn({"stage": "closed_won"}) == "受注"

    def test_owner_dimension_unassigned(self):
        dims = rr.build_dimensions()
        assert dims["owner"].key_fn({"owner_user": None}) == "未割当"


class TestPivot:
    def test_single_axis_matches_aggregate_by(self):
        dims = rr.build_dimensions()
        facts = [
            {"amount": Decimal("100"), "product_group": None},
            {"amount": Decimal("50"), "product_group": None},
        ]
        result = rr.pivot(facts, dims["product_group"])
        assert "rows" in result
        assert result["rows"][0]["amount"] == Decimal("150")
        assert result["rows"][0]["count"] == 2

    def test_two_axis_cross_tab(self):
        dims = rr.build_dimensions()
        group_a = ProductGroup(name="A")
        group_b = ProductGroup(name="B")
        facts = [
            {"amount": Decimal("100"), "product_group": group_a, "relationship_type": "new_business"},
            {"amount": Decimal("30"), "product_group": group_a, "relationship_type": "renewal"},
            {"amount": Decimal("20"), "product_group": group_b, "relationship_type": "renewal"},
        ]
        result = rr.pivot(facts, dims["product_group"], dims["relationship_type"])
        assert set(result["row_labels"]) == {"A", "B"}
        assert set(result["col_labels"]) == {"新規", "更新(Renewal)"}
        assert result["cells"]["A"]["新規"]["amount"] == Decimal("100")
        assert result["cells"]["A"]["更新(Renewal)"]["amount"] == Decimal("30")
        assert result["row_totals"]["A"] == Decimal("130")
        assert result["col_totals"]["更新(Renewal)"] == Decimal("50")
        assert result["grand_total"] == Decimal("150")
