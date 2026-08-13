"""PipelineSnapshot 日次バッチのテスト(HANDOVER.md §5 item18)。

§7.5 解消により evidence_score は confidence_score.py のスコアを
100 で正規化した値になった。数式そのものは test_confidence_score.py で
検証済みのため、ここでは配線(compute_evidence_score / create_daily_snapshots
が正しく confidence_score を呼び、DB に反映すること)だけを確認する。
"""

from __future__ import annotations

import uuid

from crm_mvp.enums import AccessLevel, Confidence, Criterion, Stage
from crm_mvp.models import PipelineSnapshot, QualificationSlot
from crm_mvp.services import confidence_score as cs
from crm_mvp.services.snapshot import compute_evidence_score, create_daily_snapshots

from .conftest import create_account_and_engagement, make_slot


class TestComputeEvidenceScore:
    def test_no_data_scores_zero(self):
        assert compute_evidence_score({}, {}, [], {}) == 0.0

    def test_matches_confidence_score_normalized_by_100(self):
        slots = {
            Criterion.BUDGET: make_slot(Criterion.BUDGET, Confidence.VERIFIED),
        }
        roles = {uuid.uuid4(): {"access_level": AccessLevel.ENGAGED, "roles": []}}
        expected = cs.compute_confidence_score(slots, {}, [], roles).total / 100.0
        assert compute_evidence_score(slots, {}, [], roles) == expected


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
        assert 0.0 < snapshot.evidence_score <= 1.0

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
