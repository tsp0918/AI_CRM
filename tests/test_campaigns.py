"""services/campaigns.py のユニットテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crm_mvp.enums import CampaignChannelType, LeadStatus
from crm_mvp.models import Campaign, Lead
from crm_mvp.services.campaigns import list_campaign_effectiveness


def make_campaign(db_session, tenant_id, **overrides) -> Campaign:
    defaults = dict(
        tenant_id=tenant_id, name="テストキャンペーン", channel_type=CampaignChannelType.EVENT,
    )
    defaults.update(overrides)
    campaign = Campaign(**defaults)
    db_session.add(campaign)
    db_session.flush()
    return campaign


def make_lead(db_session, tenant_id, campaign, **overrides) -> Lead:
    defaults = dict(
        tenant_id=tenant_id, company_name="テスト株式会社", full_name="担当者",
        source_campaign_id=campaign.id, status=LeadStatus.NEW, written_by="human:t",
    )
    defaults.update(overrides)
    lead = Lead(**defaults)
    db_session.add(lead)
    db_session.flush()
    return lead


class TestListCampaignEffectiveness:
    def test_counts_leads_and_conversions(self, db_session, tenant_id):
        campaign = make_campaign(db_session, tenant_id)
        make_lead(db_session, tenant_id, campaign, status=LeadStatus.CONVERTED)
        make_lead(db_session, tenant_id, campaign, status=LeadStatus.NEW)
        db_session.commit()

        rows = list_campaign_effectiveness(db_session, tenant_id)
        assert len(rows) == 1
        assert rows[0]["lead_count"] == 2
        assert rows[0]["converted_count"] == 1
        assert rows[0]["conversion_rate"] == 50

    def test_as_of_excludes_leads_created_after_that_date(self, db_session, tenant_id):
        campaign = make_campaign(db_session, tenant_id)
        old_lead = make_lead(db_session, tenant_id, campaign)
        old_lead.created_at = datetime.now(timezone.utc) - timedelta(days=100)
        new_lead = make_lead(db_session, tenant_id, campaign)
        new_lead.created_at = datetime.now(timezone.utc)
        db_session.commit()

        as_of = datetime.now(timezone.utc) - timedelta(days=50)
        rows = list_campaign_effectiveness(db_session, tenant_id, as_of=as_of)
        assert rows[0]["lead_count"] == 1

        rows_now = list_campaign_effectiveness(db_session, tenant_id)
        assert rows_now[0]["lead_count"] == 2

    def test_as_of_excludes_conversions_after_that_date(self, db_session, tenant_id):
        campaign = make_campaign(db_session, tenant_id)
        lead = make_lead(db_session, tenant_id, campaign, status=LeadStatus.CONVERTED)
        lead.converted_at = datetime.now(timezone.utc)
        db_session.commit()

        as_of = datetime.now(timezone.utc) - timedelta(days=1)
        rows = list_campaign_effectiveness(db_session, tenant_id, as_of=as_of)
        assert rows[0]["converted_count"] == 0

    def test_no_campaigns_returns_empty_list(self, db_session, tenant_id):
        assert list_campaign_effectiveness(db_session, tenant_id) == []
