"""lead_scoring.py のユニットテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crm_mvp.enums import TouchChannel
from crm_mvp.models import Account, Lead, Touch
from crm_mvp.services.lead_scoring import compute_lead_score

NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


def make_lead(**overrides) -> Lead:
    defaults = dict(company_name="テスト株式会社", full_name="山田 太郎", title=None)
    defaults.update(overrides)
    return Lead(**defaults)


def make_touch(channel: TouchChannel, days_ago: float = 1) -> Touch:
    return Touch(channel=channel, occurred_at=NOW - timedelta(days=days_ago))


class TestCompanyScore:
    def test_no_account_no_touches_scores_zero(self):
        score = compute_lead_score(make_lead(), None, [], now=NOW)
        assert score.company_score == 0

    def test_matched_account_adds_points(self):
        account = Account(name="テスト株式会社")
        score = compute_lead_score(make_lead(), account, [], now=NOW)
        assert score.company_score == 40
        assert any("名寄せ済み" in r for r in score.company_reasons)

    def test_high_intent_touches_add_more_than_low_intent(self):
        low = compute_lead_score(
            make_lead(), None, [make_touch(TouchChannel.EMAIL_OPEN)], now=NOW,
        )
        high = compute_lead_score(
            make_lead(), None, [make_touch(TouchChannel.CONTENT_DOWNLOAD)], now=NOW,
        )
        assert high.company_score > low.company_score

    def test_score_never_exceeds_100(self):
        account = Account(name="X")
        touches = [make_touch(TouchChannel.CONTENT_DOWNLOAD) for _ in range(20)]
        score = compute_lead_score(make_lead(), account, touches, now=NOW)
        assert score.company_score <= 100


class TestPersonScore:
    def test_no_title_no_touches_scores_zero(self):
        score = compute_lead_score(make_lead(title=None), None, [], now=NOW)
        assert score.person_score == 0

    def test_senior_title_adds_points(self):
        score = compute_lead_score(make_lead(title="製造技術部長"), None, [], now=NOW)
        assert score.person_score == 30
        assert any("製造技術部長" in r for r in score.person_reasons)

    def test_junior_title_adds_nothing(self):
        score = compute_lead_score(make_lead(title="担当"), None, [], now=NOW)
        assert score.person_score == 0

    def test_old_touches_outside_window_do_not_count(self):
        old_touch = make_touch(TouchChannel.CALL_CONNECTED, days_ago=200)
        score = compute_lead_score(make_lead(), None, [old_touch], now=NOW)
        assert not any("直近90日以内" in r for r in score.person_reasons)

    def test_recent_touches_increase_score(self):
        recent = [make_touch(TouchChannel.EMAIL_OPEN, days_ago=5) for _ in range(3)]
        score = compute_lead_score(make_lead(), None, recent, now=NOW)
        assert score.person_score > 0

    def test_score_never_exceeds_100(self):
        touches = [make_touch(TouchChannel.CONTENT_DOWNLOAD, days_ago=1) for _ in range(20)]
        score = compute_lead_score(make_lead(title="社長"), None, touches, now=NOW)
        assert score.person_score <= 100


class TestQuadrant:
    def test_hot_when_both_high(self):
        account = Account(name="X")
        touches = [make_touch(TouchChannel.CONTENT_DOWNLOAD) for _ in range(4)]
        score = compute_lead_score(
            make_lead(title="部長"), account, touches, now=NOW,
        )
        assert score.company_score >= 50
        assert score.person_score >= 50
        assert score.quadrant == "hot"

    def test_low_when_both_low(self):
        score = compute_lead_score(make_lead(), None, [], now=NOW)
        assert score.quadrant == "low"

    def test_watch_when_fit_but_not_engaged(self):
        account = Account(name="X")
        touches = [make_touch(TouchChannel.CALL_ATTEMPTED, days_ago=1)]
        score = compute_lead_score(make_lead(), account, touches, now=NOW)
        assert score.company_score >= 50
        assert score.person_score < 50
        assert score.quadrant == "watch"

    def test_nurture_when_engaged_but_not_fit(self):
        # 低関心度チャネル(email_open)は会社側の比率ボーナスを増やさないが、
        # 人物側は接点の頻度だけで十分にスコアが伸びる。
        touches = [make_touch(TouchChannel.EMAIL_OPEN, days_ago=1) for _ in range(4)]
        score = compute_lead_score(make_lead(title="社長"), None, touches, now=NOW)
        assert score.company_score < 50
        assert score.person_score >= 50
        assert score.quadrant == "nurture"
