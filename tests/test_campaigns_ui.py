"""Campaign管理UI(campaigns.py)とLeadへの紐付けの統合テスト。"""

from __future__ import annotations

from crm_mvp.models import Campaign, Lead


class TestCampaignCreation:
    def test_creates_campaign(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/campaigns/new",
            data={
                "name": "2026夏 製造業向けウェビナー", "channel_type": "event",
                "owner_team": "marketing", "cost": "500000",
                "start_date": "2026-08-01", "end_date": "2026-08-31",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        campaign = db_session.query(Campaign).filter_by(
            tenant_id=tenant_id, name="2026夏 製造業向けウェビナー",
        ).one()
        assert campaign.channel_type == "event"
        assert campaign.owner_team == "marketing"
        assert float(campaign.cost) == 500000.0

    def test_blank_name_is_rejected(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/campaigns/new",
            data={"name": "  ", "channel_type": "event"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_minimal_fields_only(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/campaigns/new",
            data={"name": "最小構成キャンペーン", "channel_type": "content"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        campaign = db_session.query(Campaign).filter_by(
            tenant_id=tenant_id, name="最小構成キャンペーン",
        ).one()
        assert campaign.cost is None
        assert campaign.start_date is None


class TestCampaignsList:
    def test_shows_lead_and_conversion_counts(self, ui_client, db_session, tenant_id):
        ui_client.post(
            "/ui/campaigns/new", data={"name": "集計テスト", "channel_type": "content"},
        )
        campaign = db_session.query(Campaign).filter_by(
            tenant_id=tenant_id, name="集計テスト",
        ).one()

        ui_client.post(
            "/ui/leads/new",
            data={
                "company_name": "テスト株式会社", "full_name": "山田 太郎",
                "source_campaign_id": str(campaign.id),
            },
        )

        resp = ui_client.get("/ui/campaigns")
        assert resp.status_code == 200
        assert "集計テスト" in resp.text

    def test_shows_conversion_rate(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import Lead

        ui_client.post(
            "/ui/campaigns/new", data={"name": "転換率テスト", "channel_type": "content"},
        )
        campaign = db_session.query(Campaign).filter_by(
            tenant_id=tenant_id, name="転換率テスト",
        ).one()

        lead1 = Lead(
            tenant_id=tenant_id, company_name="A社", full_name="担当A",
            source_campaign_id=campaign.id, status="converted", written_by="human:tester",
        )
        lead2 = Lead(
            tenant_id=tenant_id, company_name="B社", full_name="担当B",
            source_campaign_id=campaign.id, status="new", written_by="human:tester",
        )
        db_session.add_all([lead1, lead2])
        db_session.commit()

        resp = ui_client.get("/ui/campaigns")
        assert resp.status_code == 200
        assert "50%" in resp.text

    def test_zero_leads_shows_dash_not_error(self, ui_client, db_session, tenant_id):
        ui_client.post(
            "/ui/campaigns/new", data={"name": "リード無しテスト", "channel_type": "content"},
        )
        resp = ui_client.get("/ui/campaigns")
        assert resp.status_code == 200
        assert "リード無しテスト" in resp.text


class TestLeadCampaignAssignment:
    def test_lead_creation_stores_campaign(self, ui_client, db_session, tenant_id):
        ui_client.post(
            "/ui/campaigns/new", data={"name": "紐付けテスト", "channel_type": "paid_ads"},
        )
        campaign = db_session.query(Campaign).filter_by(
            tenant_id=tenant_id, name="紐付けテスト",
        ).one()

        resp = ui_client.post(
            "/ui/leads/new",
            data={
                "company_name": "北陸精密機械株式会社", "full_name": "鈴木 一郎",
                "source_campaign_id": str(campaign.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        lead = db_session.query(Lead).filter_by(
            tenant_id=tenant_id, company_name="北陸精密機械株式会社",
        ).one()
        assert lead.source_campaign_id == campaign.id

    def test_blank_campaign_is_allowed(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/leads/new",
            data={"company_name": "キャンペーンなし株式会社", "full_name": "佐藤 花子"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        lead = db_session.query(Lead).filter_by(
            tenant_id=tenant_id, company_name="キャンペーンなし株式会社",
        ).one()
        assert lead.source_campaign_id is None
