"""§7.6 PipelineSnapshot ロールアップのテスト。"""

from __future__ import annotations

from datetime import date, timedelta

from crm_mvp.enums import Stage
from crm_mvp.models import PipelineSnapshot
from crm_mvp.services.snapshot_rollup import rollup_snapshots

from .conftest import create_account_and_engagement


def _add_snapshot(session, tenant_id, engagement_id, snapshot_date):
    session.add(PipelineSnapshot(
        tenant_id=tenant_id, engagement_id=engagement_id,
        snapshot_date=snapshot_date, stage=Stage.PROPOSAL, evidence_score=0.5,
    ))


class TestDailyToWeeklyCollapse:
    def test_recent_daily_rows_are_untouched(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        today = date(2026, 8, 13)
        for offset in range(5):  # 直近5日分(90日以内)
            _add_snapshot(
                db_session, tenant_id, engagement.id, today - timedelta(days=offset),
            )
        db_session.flush()

        outcome = rollup_snapshots(db_session, tenant_id, today)
        assert outcome.daily_rows_collapsed == 0

        count = db_session.query(PipelineSnapshot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count()
        assert count == 5

    def test_old_daily_rows_in_same_week_collapse_to_one(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        today = date(2026, 8, 13)
        # ISO週(月〜日)の境界内に収まる7日分(全て90日超)
        base = date(2026, 5, 4)  # 月曜日
        for offset in range(7):
            _add_snapshot(db_session, tenant_id, engagement.id, base + timedelta(days=offset))
        db_session.flush()

        outcome = rollup_snapshots(db_session, tenant_id, today)
        assert outcome.daily_rows_collapsed == 6  # 7件 -> 1件

        remaining = db_session.query(PipelineSnapshot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).all()
        assert len(remaining) == 1
        assert remaining[0].snapshot_date == base + timedelta(days=6)  # 週内最新日を残す

    def test_is_idempotent(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        today = date(2026, 8, 13)
        base = today - timedelta(days=100)
        for offset in range(7):
            _add_snapshot(db_session, tenant_id, engagement.id, base + timedelta(days=offset))
        db_session.flush()

        rollup_snapshots(db_session, tenant_id, today)
        second = rollup_snapshots(db_session, tenant_id, today)
        assert second.daily_rows_collapsed == 0


class TestWeeklyToMonthlyCollapse:
    def test_rows_older_than_a_year_collapse_to_one_per_month(
        self, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        today = date(2026, 8, 13)
        # 400日前を含む同月の4件(全て365日超)
        old_month_days = [
            date(2025, 6, 5), date(2025, 6, 12), date(2025, 6, 19), date(2025, 6, 26),
        ]
        for d in old_month_days:
            _add_snapshot(db_session, tenant_id, engagement.id, d)
        db_session.flush()

        outcome = rollup_snapshots(db_session, tenant_id, today)
        assert outcome.weekly_rows_collapsed == 3  # 4件 -> 1件

        remaining = db_session.query(PipelineSnapshot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).all()
        assert len(remaining) == 1
        assert remaining[0].snapshot_date == date(2025, 6, 26)


class TestRollupIsTenantScoped:
    def test_only_touches_current_tenant(self, db_session, tenant_id):
        from .conftest import set_tenant_context

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        today = date(2026, 8, 13)
        base = today - timedelta(days=100)
        for offset in range(7):
            _add_snapshot(db_session, tenant_id, engagement.id, base + timedelta(days=offset))
        db_session.flush()

        import uuid
        other_tenant = uuid.uuid4()
        set_tenant_context(db_session, other_tenant)
        outcome = rollup_snapshots(db_session, other_tenant, today)
        assert outcome.daily_rows_collapsed == 0
