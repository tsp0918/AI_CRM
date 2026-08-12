"""PipelineSnapshot 日次バッチのテスト(HANDOVER.md §5 item18)。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from crm_mvp.enums import Confidence, Criterion, Stage
from crm_mvp.models import PipelineSnapshot, QualificationSlot
from crm_mvp.services.snapshot import compute_evidence_score, create_daily_snapshots

from .conftest import create_account_and_engagement, make_slot


class TestComputeEvidenceScore:
    def test_no_slots_scores_zero(self):
        assert compute_evidence_score([]) == 0.0

    def test_all_verified_scores_one(self):
        slots = [make_slot(Criterion.BUDGET, Confidence.VERIFIED)]
        assert compute_evidence_score(slots) == 1.0

    def test_mixed_confidence_averages(self):
        slots = [
            make_slot(Criterion.BUDGET, Confidence.ASSERTED),
            make_slot(Criterion.TIMING, Confidence.VERIFIED),
        ]
        # (1 + 3) / (2 * 3) = 4/6
        assert compute_evidence_score(slots) == 4 / 6

    def test_expired_slots_are_excluded(self):
        past = date.today() - timedelta(days=1)
        past_dt = datetime.combine(past, time(), tzinfo=timezone.utc)
        slots = [make_slot(Criterion.BUDGET, Confidence.VERIFIED, decays_at=past_dt)]
        assert compute_evidence_score(slots) == 0.0


class TestCreateDailySnapshots:
    def test_creates_one_per_open_engagement(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROPOSAL)
        db_session.add(QualificationSlot(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.BUDGET, value={"amount": 100},
            confidence=Confidence.CORROBORATED,
        ))
        db_session.flush()

        created = create_daily_snapshots(db_session, tenant_id)
        assert created == 1

        snapshot = db_session.query(PipelineSnapshot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert snapshot.stage == "proposal"
        assert snapshot.evidence_score == 2 / 3

    def test_closed_engagements_are_skipped(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, Stage.CLOSED_WON)
        created = create_daily_snapshots(db_session, tenant_id)
        assert created == 0

    def test_is_idempotent_per_day(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, Stage.LEAD)
        first = create_daily_snapshots(db_session, tenant_id)
        second = create_daily_snapshots(db_session, tenant_id)
        assert first == 1
        assert second == 0
