"""lead_lifecycle.py のユニットテスト。"""

from __future__ import annotations

import pytest

from crm_mvp.enums import LeadStatus, TouchChannel
from crm_mvp.models import Account, Contact, EngagementRole, Lead
from crm_mvp.services import lead_lifecycle as ll
from crm_mvp.services.lead_scoring import LeadScore


def make_lead(tenant_id, **overrides) -> Lead:
    defaults = dict(
        tenant_id=tenant_id, company_name="山田電子工業株式会社",
        full_name="山田 太郎", title="購買部長", written_by="human:sdr-1",
    )
    defaults.update(overrides)
    return Lead(**defaults)


class TestRecordTouch:
    def test_creates_touch_and_moves_new_to_working(self, db_session, tenant_id):
        lead = make_lead(tenant_id)
        db_session.add(lead)
        db_session.flush()

        touch = ll.record_touch(
            db_session, tenant_id, lead, channel=TouchChannel.FORM_SUBMIT,
        )
        db_session.commit()

        assert touch.lead_id == lead.id
        assert lead.status == LeadStatus.WORKING

    def test_does_not_regress_status_from_mql(self, db_session, tenant_id):
        lead = make_lead(tenant_id, status=LeadStatus.MQL)
        db_session.add(lead)
        db_session.flush()

        ll.record_touch(db_session, tenant_id, lead, channel=TouchChannel.EMAIL_OPEN)
        db_session.commit()

        assert lead.status == LeadStatus.MQL


class TestMaybePromoteToMql:
    def test_true_when_working_and_both_scores_above_threshold(self):
        lead = Lead(status=LeadStatus.WORKING)
        score = LeadScore(company_score=60, person_score=60)
        assert ll.maybe_promote_to_mql(lead, score) is True

    def test_false_when_not_working(self):
        lead = Lead(status=LeadStatus.NEW)
        score = LeadScore(company_score=90, person_score=90)
        assert ll.maybe_promote_to_mql(lead, score) is False

    def test_false_when_only_one_score_above_threshold(self):
        lead = Lead(status=LeadStatus.WORKING)
        score = LeadScore(company_score=90, person_score=10)
        assert ll.maybe_promote_to_mql(lead, score) is False


class TestDisqualifyLead:
    def test_sets_status_and_reason(self, db_session, tenant_id):
        lead = make_lead(tenant_id)
        db_session.add(lead)
        db_session.flush()

        ll.disqualify_lead(db_session, lead, reason="予算なし・対象外業種")
        db_session.commit()

        assert lead.status == LeadStatus.DISQUALIFIED
        assert lead.disqualify_reason == "予算なし・対象外業種"


class TestConvertLead:
    def test_creates_account_contact_and_engagement(self, db_session, tenant_id):
        lead = make_lead(tenant_id)
        db_session.add(lead)
        db_session.flush()

        engagement = ll.convert_lead(
            db_session, tenant_id, lead, actor="human:ae-1",
        )
        db_session.commit()

        assert engagement.originating_lead_id == lead.id
        assert lead.status == LeadStatus.CONVERTED
        assert lead.converted_engagement_id == engagement.id
        assert lead.matched_account_id == engagement.account_id

        account = db_session.get(Account, engagement.account_id)
        assert account.name == "山田電子工業株式会社"

        contact = db_session.query(Contact).filter_by(
            tenant_id=tenant_id, account_id=account.id,
        ).one()
        assert contact.full_name == "山田 太郎"
        assert contact.title == "購買部長"

        role = db_session.query(EngagementRole).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert role is not None

    def test_stores_conversion_snapshot(self, db_session, tenant_id):
        lead = make_lead(tenant_id)
        db_session.add(lead)
        db_session.flush()
        ll.record_touch(
            db_session, tenant_id, lead, channel=TouchChannel.CONTENT_DOWNLOAD,
        )
        db_session.flush()

        ll.convert_lead(db_session, tenant_id, lead, actor="human:ae-1")
        db_session.commit()

        snapshot = lead.conversion_snapshot
        assert snapshot["touch_count"] == 1
        assert snapshot["touch_channel_counts"] == {"content_download": 1}
        assert snapshot["company_score"] >= 0
        assert snapshot["person_score"] >= 0
        assert snapshot["quadrant"] in ("hot", "watch", "nurture", "low")
        assert "days_as_lead" in snapshot

    def test_reuses_matched_account_if_already_set(self, db_session, tenant_id):
        account = Account(tenant_id=tenant_id, name="既存取引先")
        db_session.add(account)
        db_session.flush()

        lead = make_lead(tenant_id, matched_account_id=account.id)
        db_session.add(lead)
        db_session.flush()

        engagement = ll.convert_lead(db_session, tenant_id, lead, actor="human:ae-1")
        db_session.commit()

        assert engagement.account_id == account.id
        assert db_session.query(Account).filter_by(
            tenant_id=tenant_id, name="既存取引先",
        ).count() == 1

    def test_already_converted_raises(self, db_session, tenant_id):
        lead = make_lead(tenant_id, status=LeadStatus.CONVERTED)
        db_session.add(lead)
        db_session.flush()

        with pytest.raises(ValueError):
            ll.convert_lead(db_session, tenant_id, lead, actor="human:ae-1")


class TestListTouches:
    def test_returns_touches_sorted_desc(self, db_session, tenant_id):
        from datetime import datetime, timedelta, timezone

        lead = make_lead(tenant_id)
        db_session.add(lead)
        db_session.flush()

        now = datetime.now(timezone.utc)
        older = ll.record_touch(
            db_session, tenant_id, lead, channel=TouchChannel.EMAIL_OPEN,
            occurred_at=now - timedelta(days=5),
        )
        newer = ll.record_touch(
            db_session, tenant_id, lead, channel=TouchChannel.CALL_CONNECTED,
            occurred_at=now,
        )
        db_session.commit()

        result = ll.list_touches(db_session, tenant_id, lead.id)
        assert [t.id for t in result] == [newer.id, older.id]
