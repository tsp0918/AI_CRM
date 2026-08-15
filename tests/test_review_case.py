"""review_case.py(見積作成/契約発行時のAI_TM審査起票)のテスト
(CRM_連携引き継ぎ書.md §5)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from crm_mvp.enums import ArtifactType, OutboxStatus, ReviewType
from crm_mvp.models import ActionItem, ErpMaterial, OutboxMessage, Product, ReviewCase
from crm_mvp.services import pricing as pr
from crm_mvp.services import quoting as qt
from crm_mvp.services.review_case import (
    build_review_key_hash, check_review_clearance, submit_formal_review,
    submit_provisional_review,
)

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


def make_mapped_product(db_session, tenant_id) -> Product:
    material = make_erp_material(db_session, tenant_id)
    return make_product(db_session, tenant_id, erp_material_id=material.id)


class TestBuildReviewKeyHash:
    def _base_kwargs(self) -> dict:
        return dict(
            line_item_codes=[("MAT-0001", 2.0)], destination_country="US",
            end_user_account_id=None, end_use="製造用",
            total_amount=Decimal("1000000"), currency="JPY",
        )

    def test_deterministic_for_same_inputs(self):
        kwargs = self._base_kwargs()
        assert build_review_key_hash(**kwargs) == build_review_key_hash(**kwargs)

    def test_different_currency_yields_different_hash(self):
        a = build_review_key_hash(**self._base_kwargs())
        kwargs = self._base_kwargs()
        kwargs["currency"] = "USD"
        b = build_review_key_hash(**kwargs)
        assert a != b

    def test_same_bucket_amount_yields_same_hash(self, monkeypatch):
        monkeypatch.setenv("REVIEW_KEY_VALUE_BUCKET", "100000")
        kwargs1 = self._base_kwargs()
        kwargs1["total_amount"] = Decimal("1000000")
        kwargs2 = self._base_kwargs()
        kwargs2["total_amount"] = Decimal("1050000")
        assert build_review_key_hash(**kwargs1) == build_review_key_hash(**kwargs2)

    def test_crossing_bucket_yields_different_hash(self, monkeypatch):
        monkeypatch.setenv("REVIEW_KEY_VALUE_BUCKET", "100000")
        kwargs1 = self._base_kwargs()
        kwargs1["total_amount"] = Decimal("1000000")
        kwargs2 = self._base_kwargs()
        kwargs2["total_amount"] = Decimal("1100000")
        assert build_review_key_hash(**kwargs1) != build_review_key_hash(**kwargs2)


class TestSubmitProvisionalReview:
    def test_creates_review_case_and_outbox_message_for_mapped_product(
        self, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_mapped_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product, quantity=2,
            discount_rate=Decimal("0"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )

        review_case = submit_provisional_review(
            db_session, tenant_id, quote, engagement, actor="human:ae-1",
        )
        db_session.commit()

        assert review_case is not None
        assert review_case.review_type == ReviewType.PROVISIONAL
        assert review_case.artifact_type == ArtifactType.QUOTE
        assert review_case.quote_id == quote.id
        assert review_case.case_no == f"CRM-{quote.quote_number}"

        message = db_session.query(OutboxMessage).filter_by(
            tenant_id=tenant_id, kind="aitm.review.submit",
        ).one()
        assert message.target_system == "aitm"
        assert message.status == OutboxStatus.PENDING
        assert message.payload["case_no"] == review_case.case_no
        assert message.payload["line_items"] == [{"erp_material_code": "MAT-0001", "quantity": 2.0}]

    def test_unmapped_product_creates_action_item_instead(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)  # erp_material_id 未設定
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product, quantity=1,
            discount_rate=Decimal("0"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )

        review_case = submit_provisional_review(
            db_session, tenant_id, quote, engagement, actor="human:ae-1",
        )
        db_session.commit()

        assert review_case is None
        assert db_session.query(ReviewCase).filter_by(tenant_id=tenant_id).count() == 0
        assert db_session.query(OutboxMessage).filter_by(tenant_id=tenant_id).count() == 0
        action_item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert "品目マッピング" in action_item.reason


class TestSubmitFormalReview:
    def test_independent_case_when_no_matching_provisional(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_mapped_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product, quantity=1,
            discount_rate=Decimal("0"),
        )
        db_session.flush()
        contract = qt.create_contract(
            db_session, tenant_id, engagement, actor="human:ae-1",
        )

        review_case = submit_formal_review(
            db_session, tenant_id, contract, engagement, actor="human:ae-1",
        )
        db_session.commit()

        assert review_case is not None
        assert review_case.review_type == ReviewType.FORMAL
        assert review_case.parent_case_no is None
        assert review_case.contract_id == contract.id

    def test_inherits_parent_case_no_when_hash_matches_and_not_expired(
        self, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_mapped_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product, quantity=1,
            discount_rate=Decimal("0"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        provisional = submit_provisional_review(
            db_session, tenant_id, quote, engagement, actor="human:ae-1",
        )
        provisional.valid_until = datetime.now(timezone.utc) + timedelta(days=30)
        db_session.flush()

        contract = qt.create_contract(
            db_session, tenant_id, engagement, quote=quote, actor="human:ae-1",
        )
        formal = submit_formal_review(
            db_session, tenant_id, contract, engagement, actor="human:ae-1",
        )
        db_session.commit()

        assert formal is not None
        assert formal.parent_case_no == provisional.case_no

    def test_does_not_inherit_when_provisional_expired(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_mapped_product(db_session, tenant_id)
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product, quantity=1,
            discount_rate=Decimal("0"),
        )
        db_session.flush()
        quote = qt.create_quote_from_engagement(
            db_session, tenant_id, engagement, valid_until=None, actor="human:ae-1",
        )
        provisional = submit_provisional_review(
            db_session, tenant_id, quote, engagement, actor="human:ae-1",
        )
        provisional.valid_until = datetime.now(timezone.utc) - timedelta(days=1)  # 期限切れ
        db_session.flush()

        contract = qt.create_contract(
            db_session, tenant_id, engagement, quote=quote, actor="human:ae-1",
        )
        formal = submit_formal_review(
            db_session, tenant_id, contract, engagement, actor="human:ae-1",
        )
        db_session.commit()

        assert formal is not None
        assert formal.parent_case_no is None

    def test_unmapped_product_creates_action_item_instead(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        product = make_product(db_session, tenant_id)  # erp_material_id 未設定
        pr.add_line_item(
            db_session, tenant_id, engagement, product=product, quantity=1,
            discount_rate=Decimal("0"),
        )
        db_session.flush()
        contract = qt.create_contract(
            db_session, tenant_id, engagement, actor="human:ae-1",
        )

        review_case = submit_formal_review(
            db_session, tenant_id, contract, engagement, actor="human:ae-1",
        )
        db_session.commit()

        assert review_case is None
        assert db_session.query(ReviewCase).filter_by(tenant_id=tenant_id).count() == 0
        action_item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert "品目マッピング" in action_item.reason


class TestCheckReviewClearance:
    def _make_case(self, db_session, tenant_id, engagement, **overrides) -> ReviewCase:
        defaults = dict(
            tenant_id=tenant_id, case_no="CRM-Q-2026-0001",
            review_type=ReviewType.PROVISIONAL, artifact_type=ArtifactType.QUOTE,
            quote_id=None, engagement_id=engagement.id, review_key_hash="deadbeef",
            status="pending",
        )
        defaults.update(overrides)
        case = ReviewCase(**defaults)
        db_session.add(case)
        db_session.flush()
        return case

    def test_no_review_case_blocks(self, db_session, tenant_id):
        reason = check_review_clearance(db_session, tenant_id, quote_id=uuid.uuid4())
        assert reason is not None
        assert "未起票" in reason

    def test_pending_blocks(self, db_session, tenant_id):
        from crm_mvp.models import Quote

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        quote = Quote(
            tenant_id=tenant_id, engagement_id=engagement.id, quote_number="Q-2026-0095",
            status="draft", total_amount=Decimal("0"), currency="JPY",
        )
        db_session.add(quote)
        db_session.flush()
        self._make_case(db_session, tenant_id, engagement, quote_id=quote.id, status="pending")

        reason = check_review_clearance(db_session, tenant_id, quote_id=quote.id)
        assert reason is not None
        assert "未クリア" in reason

    def test_hit_blocks(self, db_session, tenant_id):
        from crm_mvp.models import Quote

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        quote = Quote(
            tenant_id=tenant_id, engagement_id=engagement.id, quote_number="Q-2026-0099",
            status="draft", total_amount=Decimal("0"), currency="JPY",
        )
        db_session.add(quote)
        db_session.flush()
        self._make_case(db_session, tenant_id, engagement, quote_id=quote.id, status="hit")

        reason = check_review_clearance(db_session, tenant_id, quote_id=quote.id)
        assert reason is not None
        assert "未クリア" in reason

    def test_expired_blocks(self, db_session, tenant_id):
        from crm_mvp.models import Quote

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        quote = Quote(
            tenant_id=tenant_id, engagement_id=engagement.id, quote_number="Q-2026-0098",
            status="draft", total_amount=Decimal("0"), currency="JPY",
        )
        db_session.add(quote)
        db_session.flush()
        self._make_case(
            db_session, tenant_id, engagement, quote_id=quote.id, status="clear",
            valid_until=datetime.now(timezone.utc) - timedelta(days=1),
        )

        reason = check_review_clearance(db_session, tenant_id, quote_id=quote.id)
        assert reason is not None
        assert "有効期限" in reason

    def test_clear_and_not_expired_allows(self, db_session, tenant_id):
        from crm_mvp.models import Quote

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        quote = Quote(
            tenant_id=tenant_id, engagement_id=engagement.id, quote_number="Q-2026-0097",
            status="draft", total_amount=Decimal("0"), currency="JPY",
        )
        db_session.add(quote)
        db_session.flush()
        self._make_case(
            db_session, tenant_id, engagement, quote_id=quote.id, status="clear",
            valid_until=datetime.now(timezone.utc) + timedelta(days=30),
        )

        assert check_review_clearance(db_session, tenant_id, quote_id=quote.id) is None

    def test_clear_with_no_expiry_allows(self, db_session, tenant_id):
        from crm_mvp.models import Quote

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        quote = Quote(
            tenant_id=tenant_id, engagement_id=engagement.id, quote_number="Q-2026-0096",
            status="draft", total_amount=Decimal("0"), currency="JPY",
        )
        db_session.add(quote)
        db_session.flush()
        self._make_case(
            db_session, tenant_id, engagement, quote_id=quote.id, status="clear",
            valid_until=None,
        )

        assert check_review_clearance(db_session, tenant_id, quote_id=quote.id) is None
