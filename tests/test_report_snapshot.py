"""services/report_snapshot.py のユニットテスト。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from crm_mvp.enums import Stage
from crm_mvp.models import Product, StageTransition
from crm_mvp.services import report_snapshot as rs
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.snapshot import create_daily_snapshots

from .conftest import create_account_and_engagement


def make_product(db_session, tenant_id, **overrides) -> Product:
    defaults = dict(
        tenant_id=tenant_id, name="テスト商品", list_price=Decimal("100000"), currency="USD",
    )
    defaults.update(overrides)
    product = Product(**defaults)
    db_session.add(product)
    db_session.flush()
    return product


def close_won(db_session, tenant_id, engagement, *, occurred_at=None):
    """closed_won商談として扱われるにはStageTransition(to_stage=CLOSED_WON)が
    必要(as_of時点での「受注済みだったか」判定の根拠になるため)。"""
    db_session.add(StageTransition(
        tenant_id=tenant_id, engagement_id=engagement.id, from_stage=Stage.NEGOTIATION,
        to_stage=Stage.CLOSED_WON, occurred_at=occurred_at or datetime.now(timezone.utc),
        written_by="human:t",
    ))
    db_session.flush()


class TestCaptureSnapshot:
    def test_captures_revenue_totals(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng.currency = "USD"
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        close_won(db_session, tenant_id, eng)
        db_session.commit()

        snap = rs.capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        assert snap.snapshot_date == date.today()
        assert snap.payload["revenue"]["deal_count"] == 1
        assert snap.payload["revenue"]["totals_by_currency"] == [["USD", "100000.00"]]

    def test_idempotent_per_date_overwrites_existing_row(self, db_session, tenant_id):
        rs.capture_snapshot(db_session, tenant_id, actor="human:t", label="1回目")
        db_session.commit()

        snap2 = rs.capture_snapshot(db_session, tenant_id, actor="human:t", label="2回目")
        db_session.commit()

        all_snaps = rs.list_snapshots(db_session, tenant_id)
        assert len(all_snaps) == 1
        assert all_snaps[0].label == "2回目"
        assert snap2.label == "2回目"

    def test_pipeline_uses_live_stage_counts_for_today(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, stage=Stage.QUALIFIED)
        db_session.commit()

        snap = rs.capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        assert snap.payload["pipeline"]["available"] is True
        assert snap.payload["pipeline"]["source"] == "live"
        assert snap.payload["pipeline"]["stage_counts"].get("qualified") == 1

    def test_pipeline_reconstructs_past_date_from_pipeline_snapshot_table(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, stage=Stage.PROPOSAL)
        db_session.commit()

        past = date.today() - timedelta(days=10)
        create_daily_snapshots(db_session, tenant_id, past)
        db_session.commit()

        snap = rs.capture_snapshot(db_session, tenant_id, snapshot_date=past, actor="human:t")
        db_session.commit()

        assert snap.payload["pipeline"]["available"] is True
        assert snap.payload["pipeline"]["source"] == "pipeline_snapshot"
        assert snap.payload["pipeline"]["stage_counts"].get("proposal") == 1

    def test_pipeline_unavailable_for_past_date_without_pipeline_snapshot(self, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, stage=Stage.PROPOSAL)
        db_session.commit()

        past = date.today() - timedelta(days=10)
        snap = rs.capture_snapshot(db_session, tenant_id, snapshot_date=past, actor="human:t")
        db_session.commit()

        assert snap.payload["pipeline"] == {"available": False}

    def test_revenue_excludes_deals_closed_after_snapshot_date(self, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        close_won(db_session, tenant_id, eng)
        db_session.commit()

        past = date.today() - timedelta(days=365)
        snap = rs.capture_snapshot(db_session, tenant_id, snapshot_date=past, actor="human:t")
        db_session.commit()
        assert snap.payload["revenue"]["deal_count"] == 0


class TestGetAndListSnapshots:
    def test_list_orders_by_date_descending(self, db_session, tenant_id):
        rs.capture_snapshot(
            db_session, tenant_id, snapshot_date=date.today() - timedelta(days=30), actor="human:t",
        )
        rs.capture_snapshot(db_session, tenant_id, snapshot_date=date.today(), actor="human:t")
        db_session.commit()

        snaps = rs.list_snapshots(db_session, tenant_id)
        assert [s.snapshot_date for s in snaps] == [date.today(), date.today() - timedelta(days=30)]

    def test_get_snapshot_for_date_returns_none_when_missing(self, db_session, tenant_id):
        assert rs.get_snapshot_for_date(db_session, tenant_id, date.today()) is None

    def test_get_snapshot_by_id(self, db_session, tenant_id):
        snap = rs.capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        found = rs.get_snapshot(db_session, tenant_id, snap.id)
        assert found is not None
        assert found.id == snap.id


class TestDiffSnapshots:
    def test_revenue_delta_between_two_snapshots(self, db_session, tenant_id):
        old = date.today() - timedelta(days=90)
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng.currency = "USD"
        product = make_product(db_session, tenant_id)
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        close_won(db_session, tenant_id, eng)
        db_session.commit()

        snap_old = rs.capture_snapshot(db_session, tenant_id, snapshot_date=old, actor="human:t")
        snap_new = rs.capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        diff = rs.diff_snapshots(snap_old, snap_new)
        usd_diff = next(d for d in diff["revenue"] if d["currency"] == "USD")
        assert usd_diff["from"] == Decimal("0")
        assert usd_diff["to"] == Decimal("100000.00")
        assert usd_diff["delta"] == Decimal("100000.00")
        assert diff["deal_count_from"] == 0
        assert diff["deal_count_to"] == 1
