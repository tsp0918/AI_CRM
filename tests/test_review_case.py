"""review_case.py(見積作成/契約発行時のAI_TM審査起票)のテスト
(CRM_連携引き継ぎ書.md §5)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from crm_mvp.enums import ArtifactType, OutboxResult, OutboxStatus, ReviewCaseStatus, ReviewType
from crm_mvp.models import ActionItem, ErpMaterial, OutboxMessage, Product, ReviewCase
from crm_mvp.services import pricing as pr
from crm_mvp.services import quoting as qt
from crm_mvp.services import review_case as review_case_module
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
            tenant_id=tenant_id, kind="aitm.review.submit.provisional",
        ).one()
        assert message.target_system == "aitm"
        assert message.status == OutboxStatus.PENDING
        assert message.payload["crm_quote_id"] == str(quote.id)
        assert message.payload["products"] == [{"product_code": "MAT-0001", "quantity": 2.0}]

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


class _StubResponse:
    def __init__(self, status_code: int, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}
        self.content = b"{}" if data is not None else b""

    def json(self):
        return self._data


class _StubSignedClient:
    """`SignedClient`の代わり。レスポンスを固定して返すだけ。"""

    last_payload = None

    def __init__(self, *args, **kwargs):
        pass

    def post(self, path, payload, request_id=None):
        type(self).last_payload = payload
        return type(self).response

    def close(self):
        pass


class TestDispatchAitmReviewSubmitSmoothCompletion:
    """`_dispatch_aitm_review_submit`のB-4対応(2026-10-27)。
    仮審査から引き継げた正式審査は、IF-10を待たずにこの関数内で
    agent_judgment_status -> ReviewCaseStatus を写像して確定させる。"""

    def _make_case(self, db_session, tenant_id, engagement, **overrides) -> ReviewCase:
        defaults = dict(
            tenant_id=tenant_id, case_no="CRM-C-2026-0001",
            review_type=ReviewType.FORMAL, artifact_type=ArtifactType.CONTRACT,
            engagement_id=engagement.id, review_key_hash="deadbeef",
            status=ReviewCaseStatus.PENDING, revision=0, detail={"existing": "kept"},
        )
        defaults.update(overrides)
        case = ReviewCase(**defaults)
        db_session.add(case)
        db_session.flush()
        return case

    def _dispatch(self, monkeypatch, db_session, tenant_id, case, response_data):
        monkeypatch.setenv("AITM_REVIEW_URL", "http://stub")
        monkeypatch.setenv("AITM_REVIEW_BEARER", "stub-bearer")
        monkeypatch.setenv("AITM_REVIEW_SECRET", "stub-secret")
        _StubSignedClient.response = _StubResponse(200, response_data)
        monkeypatch.setattr(review_case_module, "SignedClient", _StubSignedClient)

        message = OutboxMessage(
            tenant_id=tenant_id, target_system="aitm", kind="aitm.review.submit.formal",
            payload={}, status=OutboxStatus.PENDING, ref_type="review_case", ref_id=str(case.id),
            written_by="test",
        )
        db_session.add(message)
        db_session.flush()
        result = review_case_module._dispatch_aitm_review_submit(db_session, message)
        db_session.commit()
        db_session.refresh(case)
        return result

    def test_smooth_completion_approved_maps_to_clear(self, monkeypatch, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_case(db_session, tenant_id, engagement)

        result = self._dispatch(monkeypatch, db_session, tenant_id, case, {
            "case_no": "CRM-FORMAL-000001", "smooth_completion": True,
            "inherited_from": "CRM-PROV-000001", "provisional_judgment": "approved",
        })

        assert result == OutboxResult.SENT
        assert case.provider_request_id == "CRM-FORMAL-000001"
        assert case.status == ReviewCaseStatus.CLEAR
        assert case.decided_at is not None
        assert case.detail["existing"] == "kept"  # 上書きではなくマージ
        assert case.detail["smooth_completion"] is True
        assert case.detail["inherited_from"] == "CRM-PROV-000001"

    def test_smooth_completion_blocked_maps_to_blocked(self, monkeypatch, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_case(db_session, tenant_id, engagement)

        self._dispatch(monkeypatch, db_session, tenant_id, case, {
            "case_no": "CRM-FORMAL-000002", "smooth_completion": True,
            "provisional_judgment": "blocked",
        })

        assert case.status == ReviewCaseStatus.BLOCKED

    def test_smooth_completion_with_unknown_judgment_stays_pending(
        self, monkeypatch, db_session, tenant_id,
    ):
        """fail-closed: 未知の値はPENDINGのまま(clearに倒さない)。"""
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_case(db_session, tenant_id, engagement)

        self._dispatch(monkeypatch, db_session, tenant_id, case, {
            "case_no": "CRM-FORMAL-000003", "smooth_completion": True,
            "provisional_judgment": None,
        })

        assert case.status == ReviewCaseStatus.PENDING
        assert case.decided_at is None

    def test_no_smooth_completion_leaves_status_untouched(self, monkeypatch, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        case = self._make_case(db_session, tenant_id, engagement)

        self._dispatch(monkeypatch, db_session, tenant_id, case, {
            "case_no": "CRM-FORMAL-000004", "smooth_completion": False,
        })

        assert case.status == ReviewCaseStatus.PENDING
        assert case.provider_request_id == "CRM-FORMAL-000004"


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
