"""forecast_risk.py のテスト。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from crm_mvp.enums import Stage
from crm_mvp.models import PipelineSnapshot
from crm_mvp.services.forecast_risk import assess_forecast_risk

from .conftest import create_account_and_engagement

TODAY = date(2026, 8, 13)


class TestAssessForecastRisk:
    def test_engagement_with_no_signals_is_not_at_risk(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, Stage.QUALIFIED)
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert assessments == []

    def test_closed_engagements_are_excluded(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, Stage.CLOSED_WON)
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert assessments == []

    def test_declining_snapshot_trend_is_flagged(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROPOSAL)
        db_session.add_all([
            PipelineSnapshot(
                tenant_id=tenant_id, engagement_id=engagement.id,
                snapshot_date=TODAY - timedelta(days=2), stage=Stage.PROPOSAL,
                evidence_score=0.7,
            ),
            PipelineSnapshot(
                tenant_id=tenant_id, engagement_id=engagement.id,
                snapshot_date=TODAY - timedelta(days=1), stage=Stage.PROPOSAL,
                evidence_score=0.5,
            ),
        ])
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert len(assessments) == 1
        assert any("低下傾向" in r for r in assessments[0].reasons)

    def test_improving_snapshot_trend_is_not_flagged(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROPOSAL)
        db_session.add_all([
            PipelineSnapshot(
                tenant_id=tenant_id, engagement_id=engagement.id,
                snapshot_date=TODAY - timedelta(days=2), stage=Stage.PROPOSAL,
                evidence_score=0.4,
            ),
            PipelineSnapshot(
                tenant_id=tenant_id, engagement_id=engagement.id,
                snapshot_date=TODAY - timedelta(days=1), stage=Stage.PROPOSAL,
                evidence_score=0.6,
            ),
        ])
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert assessments == []

    def test_close_date_soon_with_low_score_is_flagged(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        engagement.expected_close_date = TODAY + timedelta(days=10)
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert len(assessments) == 1
        assert any("クローズ想定" in r for r in assessments[0].reasons)

    def test_close_date_far_away_is_not_flagged_for_close_date_reason(
        self, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        engagement.expected_close_date = TODAY + timedelta(days=200)
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert not any(
            "クローズ想定" in r for a in assessments for r in a.reasons
        )

    def test_weighted_amount_none_when_amount_missing(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        engagement.expected_close_date = TODAY + timedelta(days=5)
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert assessments[0].weighted_amount is None

    def test_weighted_amount_scales_by_score(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        engagement.expected_close_date = TODAY + timedelta(days=5)
        engagement.amount = Decimal("1000000")
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        a = assessments[0]
        assert a.weighted_amount == a.engagement.amount * (Decimal(a.score.total) / 100)

    def test_sorted_by_weighted_amount_descending(self, db_session, tenant_id):
        _, big = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        big.expected_close_date = TODAY + timedelta(days=5)
        big.amount = Decimal("9000000")
        _, small = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        small.expected_close_date = TODAY + timedelta(days=5)
        small.amount = Decimal("100000")
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert [a.engagement.id for a in assessments] == [big.id, small.id]

    def test_currency_reflects_engagement_currency_not_hardcoded_jpy(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        engagement.expected_close_date = TODAY + timedelta(days=5)
        engagement.amount = Decimal("1000000")
        engagement.currency = "USD"
        db_session.flush()

        assessments = assess_forecast_risk(db_session, tenant_id, today=TODAY)
        assert assessments[0].currency == "USD"
