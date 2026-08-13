"""Lead獲得UI(crm_mvp/api/web/leads.py)の統合テスト。"""

from __future__ import annotations

from crm_mvp.enums import LeadStatus
from crm_mvp.models import Engagement, Lead, Touch


def make_lead(db_session, tenant_id, **overrides) -> Lead:
    defaults = dict(
        tenant_id=tenant_id, company_name="山田電子工業株式会社",
        full_name="山田 太郎", title="購買部長", written_by="human:sdr-1",
    )
    defaults.update(overrides)
    lead = Lead(**defaults)
    db_session.add(lead)
    db_session.flush()
    return lead


class TestLeadsList:
    def test_lists_leads_for_current_tenant(self, ui_client, db_session, tenant_id):
        make_lead(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/leads")
        assert resp.status_code == 200
        assert "山田電子工業株式会社" in resp.text

    def test_status_facet_filters_and_shows_counts(self, ui_client, db_session, tenant_id):
        make_lead(db_session, tenant_id, company_name="新規社", status=LeadStatus.NEW)
        make_lead(db_session, tenant_id, company_name="接触中社", status=LeadStatus.WORKING)
        db_session.commit()

        resp = ui_client.get("/ui/leads?facet=status")
        assert resp.status_code == 200
        assert "新規社" in resp.text
        assert "接触中社" in resp.text

        resp_new = ui_client.get("/ui/leads?facet=status&status=new")
        assert resp_new.status_code == 200
        assert "新規社" in resp_new.text
        assert "接触中社" not in resp_new.text

    def test_quadrant_facet_filters(self, ui_client, db_session, tenant_id):
        # 接点も紐付くAccountも無い新規Leadはcompany_score=0,person_score=0 -> quadrant "low"
        make_lead(db_session, tenant_id, company_name="Low象限社")
        db_session.commit()

        resp = ui_client.get("/ui/leads?facet=quadrant&quadrant=low")
        assert resp.status_code == 200
        assert "Low象限社" in resp.text

        resp_hot = ui_client.get("/ui/leads?facet=quadrant&quadrant=hot")
        assert resp_hot.status_code == 200
        assert "Low象限社" not in resp_hot.text

    def test_facet_tabs_and_total_count_render(self, ui_client, db_session, tenant_id):
        make_lead(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/leads")
        assert resp.status_code == 200
        assert "ステータスで見る" in resp.text
        assert "優先度で見る" in resp.text
        assert "全体" in resp.text


class TestLeadCreation:
    def test_creates_lead_and_redirects_to_detail(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/leads/new",
            data={
                "company_name": "北陸精密機械株式会社", "full_name": "鈴木 一郎",
                "title": "生産技術課長", "source_channel": "inbound",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/ui/leads/" in resp.headers["location"]

        lead = db_session.query(Lead).filter_by(
            tenant_id=tenant_id, company_name="北陸精密機械株式会社",
        ).one()
        assert lead.full_name == "鈴木 一郎"
        assert lead.status == "new"

    def test_blank_fields_are_rejected(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/leads/new",
            data={"company_name": "  ", "full_name": "鈴木 一郎"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestLeadDetail:
    def test_shows_score_and_quadrant(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/leads/{lead.id}")
        assert resp.status_code == 200
        assert "Company Score" in resp.text
        assert "Person Score" in resp.text

    def test_other_tenant_lead_is_404(self, ui_client, db_session):
        import uuid
        resp = ui_client.get(f"/ui/leads/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestAddTouch:
    def test_records_touch_and_advances_status(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/touches",
            data={"channel": "form_submit", "note": "資料ダウンロード"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        touch = db_session.query(Touch).filter_by(
            tenant_id=tenant_id, lead_id=lead.id,
        ).one()
        assert touch.channel == "form_submit"
        assert touch.raw_payload.get("note") == "資料ダウンロード"

        db_session.refresh(lead)
        assert lead.status == "working"


class TestAdvanceStatus:
    def test_advances_new_to_working(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id, status=LeadStatus.NEW)
        db_session.commit()

        resp = ui_client.post(f"/ui/leads/{lead.id}/advance", follow_redirects=False)
        assert resp.status_code == 303
        db_session.refresh(lead)
        assert lead.status == "working"

    def test_no_next_status_from_sql_shows_error(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id, status=LeadStatus.SQL)
        db_session.commit()

        resp = ui_client.post(f"/ui/leads/{lead.id}/advance", follow_redirects=False)
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestConvertLead:
    def test_converts_and_redirects_to_engagement(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id, status=LeadStatus.SQL)
        db_session.commit()

        resp = ui_client.post(f"/ui/leads/{lead.id}/convert", follow_redirects=False)
        assert resp.status_code == 303
        assert "/ui/engagements/" in resp.headers["location"]

        db_session.refresh(lead)
        assert lead.status == "converted"
        engagement = db_session.query(Engagement).filter_by(
            tenant_id=tenant_id, id=lead.converted_engagement_id,
        ).one()
        assert engagement.originating_lead_id == lead.id

    def test_already_converted_shows_error(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id, status=LeadStatus.CONVERTED)
        db_session.commit()

        resp = ui_client.post(f"/ui/leads/{lead.id}/convert", follow_redirects=False)
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestDisqualifyLead:
    def test_disqualifies_with_reason(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/disqualify",
            data={"reason": "予算なし・対象業種外"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(lead)
        assert lead.status == "disqualified"
        assert lead.disqualify_reason == "予算なし・対象業種外"

    def test_blank_reason_is_rejected(self, ui_client, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/disqualify",
            data={"reason": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        db_session.refresh(lead)
        assert lead.status != "disqualified"


class TestEngagementLeadSummary:
    def test_engagement_detail_shows_lead_summary_after_conversion(
        self, ui_client, db_session, tenant_id,
    ):
        from crm_mvp.enums import LeadStatus, TouchChannel

        lead = make_lead(db_session, tenant_id, status=LeadStatus.SQL)
        db_session.commit()

        ui_client.post(
            f"/ui/leads/{lead.id}/touches",
            data={"channel": "content_download", "note": "資料DL"},
        )
        resp = ui_client.post(f"/ui/leads/{lead.id}/convert", follow_redirects=False)
        assert resp.status_code == 303
        engagement_url = resp.headers["location"]

        detail_resp = ui_client.get(engagement_url)
        assert detail_resp.status_code == 200
        assert "リード発生経緯" in detail_resp.text
        assert lead.full_name in detail_resp.text
        assert "案件化時点の温度" in detail_resp.text

    def test_no_lead_summary_for_directly_created_engagement(
        self, ui_client, db_session, tenant_id,
    ):
        from .conftest import create_account_and_engagement
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert "リード発生経緯" not in resp.text
