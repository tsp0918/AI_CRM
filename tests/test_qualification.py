"""QualificationSlot.meets() — ゲートエンジンが依存する証拠強度判定のロジック。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crm_mvp.enums import Confidence, Criterion

from .conftest import make_slot


class TestQualificationSlotMeets:
    def test_higher_confidence_meets_lower_requirement(self):
        slot = make_slot(Criterion.BUDGET, Confidence.VERIFIED)
        assert slot.meets(Confidence.ASSERTED) is True

    def test_lower_confidence_does_not_meet_higher_requirement(self):
        slot = make_slot(Criterion.BUDGET, Confidence.ASSERTED)
        assert slot.meets(Confidence.VERIFIED) is False

    def test_empty_value_never_meets(self):
        slot = make_slot(Criterion.BUDGET, Confidence.VERIFIED, value={})
        assert slot.meets(Confidence.ASSERTED) is False

    def test_expired_slot_does_not_meet(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        slot = make_slot(Criterion.BUDGET, Confidence.VERIFIED, decays_at=past)
        assert slot.meets(Confidence.ASSERTED) is False

    def test_future_decay_still_meets(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        slot = make_slot(Criterion.BUDGET, Confidence.VERIFIED, decays_at=future)
        assert slot.meets(Confidence.ASSERTED) is True

    def test_no_decay_date_never_expires(self):
        slot = make_slot(Criterion.BUDGET, Confidence.ASSERTED, decays_at=None)
        assert slot.meets(Confidence.ASSERTED) is True
