"""quoting.py のユニットテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crm_mvp.enums import ContractStatus, QuoteStatus
from crm_mvp.models import Product
from crm_mvp.services import pricing as pr
from crm_mvp.services import quoting as qt

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


class TestCreateQuoteFromEngagement:
    def test_snapshots_line_items(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=2, discount_rate=Decimal("10"),
        )
        db_session.flush()

        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        db_session.commit()

        assert quote.status == QuoteStatus.DRAFT
        assert quote.total_amount == Decimal("1800000.00")
        assert quote.quote_number.startswith("Q-")

        items = qt.list_quote_line_items(db_session, tenant_id, quote.id)
        assert len(items) == 1
        assert items[0].product_name_snapshot == "検査装置 標準モデル"

    def test_number_increments(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()

        quote1 = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        quote2 = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        assert quote1.quote_number != quote2.quote_number

    def test_rejects_engagement_without_line_items(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        with pytest.raises(ValueError):
            qt.create_quote_from_engagement(
                db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
            )

    def test_quote_line_items_survive_engagement_line_item_changes(
        self, db_session, tenant_id,
    ):
        """発行済み見積もりの明細は、その後の商品構成変更の影響を受けない
        (スナップショットとして凍結される)。"""
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        item = pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        db_session.flush()

        pr.remove_line_item(db_session, tenant_id, engagement, item)
        db_session.commit()

        items = qt.list_quote_line_items(db_session, tenant_id, quote.id)
        assert len(items) == 1


class TestUpdateQuoteStatus:
    def test_sent_sets_issued_at(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        assert quote.issued_at is None

        qt.update_quote_status(quote, QuoteStatus.SENT)
        assert quote.status == QuoteStatus.SENT
        assert quote.issued_at is not None


class TestCreateContract:
    def test_from_quote_copies_quote_line_items(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("5"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        db_session.flush()

        contract = qt.create_contract(
            db_session, tenant_id, engagement, quote=quote, actor="human:ae-1",
        )
        db_session.commit()

        assert contract.quote_id == quote.id
        assert contract.contract_number.startswith("C-")
        assert contract.total_amount == quote.total_amount
        items = qt.list_contract_line_items(db_session, tenant_id, contract.id)
        assert len(items) == 1
        assert items[0].discount_rate == Decimal("5.00")

    def test_direct_from_engagement_line_items(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()

        contract = qt.create_contract(
            db_session, tenant_id, engagement, quote=None, actor="human:ae-1",
        )
        db_session.commit()

        assert contract.quote_id is None
        items = qt.list_contract_line_items(db_session, tenant_id, contract.id)
        assert len(items) == 1

    def test_rejects_when_no_items_and_no_quote(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        with pytest.raises(ValueError):
            qt.create_contract(db_session, tenant_id, engagement, quote=None, actor="human:ae-1")


class TestUpdateContractStatus:
    def test_signed_sets_signed_at(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()
        contract = qt.create_contract(
            db_session, tenant_id, engagement, quote=None, actor="human:ae-1",
        )
        assert contract.signed_at is None

        qt.update_contract_status(contract, ContractStatus.SIGNED)
        assert contract.status == ContractStatus.SIGNED
        assert contract.signed_at is not None


class TestListQuotesAndContracts:
    def test_list_quotes_scoped_by_engagement(self, db_session, tenant_id):
        _, engagement_a = create_account_and_engagement(db_session, tenant_id)
        _, engagement_b = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement_a, product=product,
            quantity=1, discount_rate=Decimal("0"),
        )
        db_session.flush()
        qt.create_quote_from_engagement(
            db_session, tenant_id, engagement_a, valid_until=None, actor="human:ae-1",
        )
        db_session.commit()

        assert len(qt.list_quotes(db_session, tenant_id, engagement_a.id)) == 1
        assert len(qt.list_quotes(db_session, tenant_id, engagement_b.id)) == 0
        assert len(qt.list_quotes(db_session, tenant_id)) == 1
