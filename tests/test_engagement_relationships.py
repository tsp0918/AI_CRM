"""services/engagement_relationships.py のユニットテスト。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from crm_mvp.enums import ContractStatus, EngagementRelationshipType, Stage
from crm_mvp.models import Contract
from crm_mvp.services import engagement_relationships as er

from .conftest import create_account_and_engagement


def make_contract(db_session, tenant_id, engagement, **overrides) -> Contract:
    defaults = dict(
        tenant_id=tenant_id, engagement_id=engagement.id, contract_number="C-TEST-0001",
        status=ContractStatus.ACTIVE, total_amount=Decimal("1000000"), currency="JPY",
        written_by="human:tester",
    )
    defaults.update(overrides)
    contract = Contract(**defaults)
    db_session.add(contract)
    db_session.flush()
    return contract


class TestCreateChildEngagement:
    def test_creates_renewal_linked_to_parent(self, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)

        child = er.create_child_engagement(
            db_session, tenant_id, parent,
            relationship_type=EngagementRelationshipType.RENEWAL, name="更新商談",
        )
        db_session.commit()

        assert child.parent_engagement_id == parent.id
        assert child.relationship_type == EngagementRelationshipType.RENEWAL
        assert child.account_id == parent.account_id
        assert child.stage == Stage.LEAD

    def test_rejects_blank_name(self, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id)
        with pytest.raises(ValueError):
            er.create_child_engagement(
                db_session, tenant_id, parent,
                relationship_type=EngagementRelationshipType.UPSELL, name="  ",
            )

    def test_renewal_carries_over_parent_qualification_slots(self, db_session, tenant_id):
        from crm_mvp.enums import Confidence, Criterion
        from crm_mvp.models import QualificationSlot

        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.add(QualificationSlot(
            tenant_id=tenant_id, engagement_id=parent.id, criterion=Criterion.BUDGET,
            value={"amount": 24000, "fiscal_period": "FY2026"}, confidence=Confidence.VERIFIED,
            written_by="human:tester",
        ))
        db_session.flush()

        child = er.create_child_engagement(
            db_session, tenant_id, parent,
            relationship_type=EngagementRelationshipType.RENEWAL, name="更新商談",
        )
        db_session.commit()

        child_slots = db_session.query(QualificationSlot).filter_by(
            tenant_id=tenant_id, engagement_id=child.id,
        ).all()
        assert len(child_slots) == 1
        slot = child_slots[0]
        assert slot.criterion == Criterion.BUDGET
        assert slot.value == {"amount": 24000, "fiscal_period": "FY2026"}
        assert slot.confidence == Confidence.VERIFIED
        assert slot.written_by == "system:renewal-carryover"
        assert slot.decays_at is not None

    def test_upsell_does_not_carry_over_qualification_slots(self, db_session, tenant_id):
        from crm_mvp.enums import Confidence, Criterion
        from crm_mvp.models import QualificationSlot

        _, parent = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        db_session.add(QualificationSlot(
            tenant_id=tenant_id, engagement_id=parent.id, criterion=Criterion.BUDGET,
            value={"amount": 24000}, confidence=Confidence.VERIFIED, written_by="human:tester",
        ))
        db_session.flush()

        child = er.create_child_engagement(
            db_session, tenant_id, parent,
            relationship_type=EngagementRelationshipType.UPSELL, name="Upsell商談",
        )
        db_session.commit()

        child_slots = db_session.query(QualificationSlot).filter_by(
            tenant_id=tenant_id, engagement_id=child.id,
        ).all()
        assert child_slots == []


class TestListChildEngagements:
    def test_scoped_to_parent(self, db_session, tenant_id):
        _, parent = create_account_and_engagement(db_session, tenant_id)
        _, other = create_account_and_engagement(db_session, tenant_id)

        er.create_child_engagement(
            db_session, tenant_id, parent,
            relationship_type=EngagementRelationshipType.RENEWAL, name="更新1",
        )
        er.create_child_engagement(
            db_session, tenant_id, parent,
            relationship_type=EngagementRelationshipType.UPSELL, name="Upsell1",
        )
        db_session.commit()

        assert len(er.list_child_engagements(db_session, tenant_id, parent.id)) == 2
        assert len(er.list_child_engagements(db_session, tenant_id, other.id)) == 0


class TestListRenewalCandidates:
    def test_finds_active_contract_nearing_end_date(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        contract = make_contract(
            db_session, tenant_id, eng, end_date=date.today() + timedelta(days=30),
        )
        db_session.commit()

        candidates = er.list_renewal_candidates(db_session, tenant_id, within_days=90)
        assert [c.id for c in candidates] == [contract.id]

    def test_excludes_contract_far_from_end_date(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        make_contract(db_session, tenant_id, eng, end_date=date.today() + timedelta(days=400))
        db_session.commit()

        assert er.list_renewal_candidates(db_session, tenant_id, within_days=90) == []

    def test_excludes_contract_without_end_date(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        make_contract(db_session, tenant_id, eng, end_date=None)
        db_session.commit()

        assert er.list_renewal_candidates(db_session, tenant_id, within_days=90) == []

    def test_excludes_when_renewal_already_created(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        make_contract(db_session, tenant_id, eng, end_date=date.today() + timedelta(days=10))
        er.create_child_engagement(
            db_session, tenant_id, eng,
            relationship_type=EngagementRelationshipType.RENEWAL, name="更新中",
        )
        db_session.commit()

        assert er.list_renewal_candidates(db_session, tenant_id, within_days=90) == []

    def test_does_not_exclude_for_upsell_child(self, db_session, tenant_id):
        """RENEWAL以外の子商談(Upsell等)があっても更新候補からは除外しない。"""
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        contract = make_contract(
            db_session, tenant_id, eng, end_date=date.today() + timedelta(days=10),
        )
        er.create_child_engagement(
            db_session, tenant_id, eng,
            relationship_type=EngagementRelationshipType.UPSELL, name="Upsell商談",
        )
        db_session.commit()

        candidates = er.list_renewal_candidates(db_session, tenant_id, within_days=90)
        assert [c.id for c in candidates] == [contract.id]
