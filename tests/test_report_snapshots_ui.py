"""crm_mvp/api/web/report_snapshots.py の統合テスト。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from crm_mvp.enums import Stage
from crm_mvp.models import Product
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.report_snapshot import capture_snapshot

from .conftest import create_account_and_engagement


class TestReportHistoryList:
    def test_renders_with_no_snapshots(self, ui_client):
        resp = ui_client.get("/ui/reports/history")
        assert resp.status_code == 200
        assert "まだスナップショットがありません" in resp.text

    def test_lists_saved_snapshots(self, ui_client, db_session, tenant_id):
        capture_snapshot(db_session, tenant_id, label="テスト回", actor="human:t")
        db_session.commit()

        resp = ui_client.get("/ui/reports/history")
        assert resp.status_code == 200
        assert "テスト回" in resp.text
        assert str(date.today()) in resp.text


class TestReportHistoryCapture:
    def test_saves_snapshot_and_redirects(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/reports/history/capture", data={"label": "手動保存"}, follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/ui/reports/history" in resp.headers["location"]

        resp_list = ui_client.get("/ui/reports/history")
        assert "手動保存" in resp_list.text

    def test_capturing_twice_same_day_overwrites_not_duplicates(self, ui_client, db_session, tenant_id):
        ui_client.post("/ui/reports/history/capture", data={"label": "1回目"})
        ui_client.post("/ui/reports/history/capture", data={"label": "2回目"})

        resp = ui_client.get("/ui/reports/history")
        assert "2回目" in resp.text
        assert resp.text.count(str(date.today())) == 1 or "1回目" not in resp.text


class TestReportHistoryDetail:
    def test_shows_revenue_breakdown(self, ui_client, db_session, tenant_id):
        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng.currency = "USD"
        product = Product(
            tenant_id=tenant_id, name="テスト商品", list_price=Decimal("50000"), currency="USD",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        from crm_mvp.models import StageTransition
        from datetime import datetime, timezone
        db_session.add(StageTransition(
            tenant_id=tenant_id, engagement_id=eng.id, from_stage=Stage.NEGOTIATION,
            to_stage=Stage.CLOSED_WON, occurred_at=datetime.now(timezone.utc), written_by="human:t",
        ))
        db_session.commit()

        snap = capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        resp = ui_client.get(f"/ui/reports/history/{snap.id}")
        assert resp.status_code == 200
        assert "50,000 USD" in resp.text

    def test_missing_snapshot_redirects_with_error(self, ui_client):
        import uuid

        resp = ui_client.get(f"/ui/reports/history/{uuid.uuid4()}", follow_redirects=False)
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestReportHistoryCompare:
    def test_renders_prompt_when_no_selection(self, ui_client, db_session, tenant_id):
        capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        resp = ui_client.get("/ui/reports/history/compare")
        assert resp.status_code == 200
        assert "起点・終点を選んで" in resp.text

    def test_compare_route_is_not_shadowed_by_snapshot_id_route(self, ui_client, db_session, tenant_id):
        """/compareが/{snapshot_id}にUUIDとして誤解釈されないことの回帰テスト。"""
        resp = ui_client.get("/ui/reports/history/compare")
        assert resp.status_code == 200

    def test_shows_diff_between_two_snapshots(self, ui_client, db_session, tenant_id):
        old_date = date.today() - timedelta(days=30)
        snap_old = capture_snapshot(db_session, tenant_id, snapshot_date=old_date, actor="human:t")

        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        eng.currency = "USD"
        product = Product(
            tenant_id=tenant_id, name="テスト商品", list_price=Decimal("70000"), currency="USD",
        )
        db_session.add(product)
        db_session.flush()
        add_line_item(db_session, tenant_id, eng, product=product, quantity=1, discount_rate=Decimal("0"))
        from crm_mvp.models import StageTransition
        from datetime import datetime, timezone
        db_session.add(StageTransition(
            tenant_id=tenant_id, engagement_id=eng.id, from_stage=Stage.NEGOTIATION,
            to_stage=Stage.CLOSED_WON, occurred_at=datetime.now(timezone.utc), written_by="human:t",
        ))
        db_session.flush()
        snap_new = capture_snapshot(db_session, tenant_id, actor="human:t")
        db_session.commit()

        resp = ui_client.get(
            f"/ui/reports/history/compare?from_id={snap_old.id}&to_id={snap_new.id}",
        )
        assert resp.status_code == 200
        assert "70,000" in resp.text
        assert "受注商談数: 0" in resp.text
