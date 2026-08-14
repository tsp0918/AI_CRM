"""weekly_review.py のユニットテスト。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from crm_mvp.enums import ReviewStatus, Stage
from crm_mvp.models import PipelineSnapshot
from crm_mvp.services import weekly_review as wr
from crm_mvp.services.confidence_score import ConfidenceScore

from .conftest import create_account_and_engagement


def make_score(total: int = 60) -> ConfidenceScore:
    return ConfidenceScore(
        total=total, evidence_depth=0.5, governance_reach=0.5, freshness=0.5,
        single_threaded=False, decider_reachable=False, decider_engaged=False,
    )


class TestWeekStart:
    def test_rounds_down_to_monday(self):
        # 2026-08-14 は金曜日 -> その週の月曜は 2026-08-10
        assert wr.week_start(date(2026, 8, 14)) == date(2026, 8, 10)

    def test_monday_stays_monday(self):
        assert wr.week_start(date(2026, 8, 10)) == date(2026, 8, 10)


class TestGetOrCreateCurrentReview:
    def test_creates_review_for_current_week(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        review = wr.get_or_create_current_review(
            db_session, tenant_id, engagement.id,
            actor="human:tester", today=date(2026, 8, 14),
        )
        db_session.commit()

        assert review.week_start_date == date(2026, 8, 10)
        assert review.engagement_id == engagement.id
        assert review.written_by == "human:tester"

    def test_second_call_same_week_returns_same_row(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        first = wr.get_or_create_current_review(
            db_session, tenant_id, engagement.id, today=date(2026, 8, 14),
        )
        db_session.commit()
        second = wr.get_or_create_current_review(
            db_session, tenant_id, engagement.id, today=date(2026, 8, 12),
        )

        assert first.id == second.id

    def test_different_week_creates_new_row(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        this_week = wr.get_or_create_current_review(
            db_session, tenant_id, engagement.id, today=date(2026, 8, 14),
        )
        db_session.commit()
        next_week = wr.get_or_create_current_review(
            db_session, tenant_id, engagement.id, today=date(2026, 8, 21),
        )

        assert this_week.id != next_week.id


class TestUpdateReview:
    def test_updates_comments_and_status(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()
        review = wr.get_or_create_current_review(db_session, tenant_id, engagement.id)

        wr.update_review(
            review, rep_comment="来週デモ予定", manager_comment="順調",
            manager_status=ReviewStatus.ON_TRACK,
        )
        db_session.commit()

        assert review.rep_comment == "来週デモ予定"
        assert review.manager_comment == "順調"
        assert review.manager_status == "on_track"

    def test_blank_string_clears_field(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()
        review = wr.get_or_create_current_review(db_session, tenant_id, engagement.id)
        wr.update_review(review, rep_comment="下書き")
        db_session.commit()

        wr.update_review(review, rep_comment="")
        db_session.commit()
        assert review.rep_comment is None


class TestComputeWeekOverWeekDiff:
    def test_no_baseline_when_no_snapshot(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        diff = wr.compute_week_over_week_diff(
            db_session, tenant_id, engagement, make_score(), today=date(2026, 8, 14),
        )
        assert diff == {"has_baseline": False}

    def test_computes_deltas_against_snapshot_older_than_a_week(
        self, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id, stage=Stage.NEGOTIATION)
        engagement.amount = Decimal("1500000")
        db_session.add(PipelineSnapshot(
            tenant_id=tenant_id, engagement_id=engagement.id,
            snapshot_date=date(2026, 8, 5), stage=Stage.PROPOSAL,
            amount=Decimal("1000000"), evidence_score=0.4,
        ))
        db_session.commit()

        diff = wr.compute_week_over_week_diff(
            db_session, tenant_id, engagement, make_score(total=60), today=date(2026, 8, 14),
        )

        assert diff["has_baseline"] is True
        assert diff["score_delta"] == 20  # 60 - 40
        assert diff["amount_delta"] == Decimal("500000")
        assert diff["previous_stage"] == "proposal"
        assert diff["stage_changed"] is True

    def test_ignores_snapshot_within_last_7_days(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.add(PipelineSnapshot(
            tenant_id=tenant_id, engagement_id=engagement.id,
            snapshot_date=date(2026, 8, 12), stage=Stage.LEAD,
            amount=None, evidence_score=0.2,
        ))
        db_session.commit()

        diff = wr.compute_week_over_week_diff(
            db_session, tenant_id, engagement, make_score(), today=date(2026, 8, 14),
        )
        assert diff == {"has_baseline": False}
