"""sequences.py のユニットテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crm_mvp.enums import (
    SequenceDraftStatus, SequenceEnrollmentStatus, SequenceStepChannel, TouchChannel,
)
from crm_mvp.models import Lead, SequenceDraft, SequenceEnrollment, SequenceStep, Touch
from crm_mvp.services import sequences as sq
from crm_mvp.services.lead_lifecycle import record_touch

NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)

DEMO_STEPS = [
    {"channel": SequenceStepChannel.EMAIL, "delay_days": 0,
     "subject_template": "{{company_name}}様へのご案内",
     "body_template": "{{full_name}}様\n\nお世話になっております。"},
    {"channel": SequenceStepChannel.CALL_TASK, "delay_days": 3,
     "body_template": "{{full_name}}様へ架電する。"},
    {"channel": SequenceStepChannel.EMAIL, "delay_days": 4,
     "subject_template": "フォローアップ",
     "body_template": "{{full_name}}様\n\n{{recent_signal}}を拝見しました。"},
]


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


class TestRenderStep:
    def test_substitutes_known_placeholders(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="テスト", description=None,
            steps=DEMO_STEPS, actor="human:m",
        )
        steps = db_session.query(SequenceStep).filter_by(
            tenant_id=tenant_id, sequence_id=sequence.id,
        ).order_by(SequenceStep.step_order).all()

        subject, body, note = sq.render_step(steps[0], lead, [])
        assert subject == "山田電子工業株式会社様へのご案内"
        assert "山田 太郎様" in body
        assert note is None

    def test_recent_signal_is_pulled_from_high_intent_touch(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        touch = Touch(
            channel=TouchChannel.CONTENT_DOWNLOAD,
            occurred_at=NOW - timedelta(days=2),
        )
        subject, body, note = sq.render_step(
            sq.SequenceStep(
                channel=SequenceStepChannel.EMAIL, delay_days=0,
                subject_template=None, body_template="{{recent_signal}}について",
            ),
            lead, [touch],
        )
        assert "content_download" in body
        assert note is not None

    def test_no_touches_leaves_recent_signal_blank(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        _, body, note = sq.render_step(
            sq.SequenceStep(
                channel=SequenceStepChannel.EMAIL, delay_days=0,
                subject_template=None, body_template="[{{recent_signal}}]",
            ),
            lead, [],
        )
        assert body == "[]"
        assert note is None


class TestCreateSequenceAndEnroll:
    def test_create_sequence_persists_ordered_steps(self, db_session, tenant_id):
        sequence = sq.create_sequence(
            db_session, tenant_id, name="新規開拓3ステップ", description="製造業向け",
            steps=DEMO_STEPS, actor="human:m",
        )
        db_session.commit()

        steps = db_session.query(SequenceStep).filter_by(
            tenant_id=tenant_id, sequence_id=sequence.id,
        ).order_by(SequenceStep.step_order).all()
        assert [s.step_order for s in steps] == [0, 1, 2]
        assert steps[1].channel == "call_task"

    def test_enroll_sets_next_action_at_from_first_step_delay(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=DEMO_STEPS, actor="human:m",
        )
        enrollment = sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.commit()

        assert enrollment.status == SequenceEnrollmentStatus.ACTIVE
        assert enrollment.current_step_order is None  # まだ1件も生成していない
        # 1件目の delay_days=0 なのですぐ発火可能
        assert enrollment.next_action_at == NOW

    def test_double_enroll_raises(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=DEMO_STEPS, actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()

        with pytest.raises(ValueError):
            sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)


class TestGenerateDueDrafts:
    def test_generates_first_step_and_advances_enrollment(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=DEMO_STEPS, actor="human:m",
        )
        enrollment = sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()

        drafts = sq.generate_due_drafts(db_session, tenant_id, now=NOW)
        db_session.commit()

        assert len(drafts) == 1
        assert drafts[0].channel == "email"
        db_session.refresh(enrollment)
        assert enrollment.current_step_order == 0  # 直近に生成したステップの order
        assert enrollment.next_action_at == NOW + timedelta(days=3)  # ステップ2(call_task)のdelay

    def test_not_due_yet_generates_nothing(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=[{"channel": SequenceStepChannel.EMAIL, "delay_days": 10,
                    "body_template": "{{full_name}}"}],
            actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()

        drafts = sq.generate_due_drafts(db_session, tenant_id, now=NOW)
        assert drafts == []

    def test_completes_enrollment_after_last_step(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=[{"channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                    "body_template": "{{full_name}}"}],
            actor="human:m",
        )
        enrollment = sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()

        sq.generate_due_drafts(db_session, tenant_id, now=NOW)
        db_session.commit()

        db_session.refresh(enrollment)
        assert enrollment.status == SequenceEnrollmentStatus.COMPLETED
        assert enrollment.next_action_at is None

    def test_uses_real_touches_for_personalization(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        record_touch(
            db_session, tenant_id, lead, channel=TouchChannel.CONTENT_DOWNLOAD,
            occurred_at=NOW - timedelta(days=1),
        )
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=[{"channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                    "body_template": "{{recent_signal}}"}],
            actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()

        drafts = sq.generate_due_drafts(db_session, tenant_id, now=NOW)
        assert "content_download" in drafts[0].body


class TestBranching:
    def _branching_steps(self):
        return [
            {  # order 0: 高関心度シグナルがあれば order2(ホットパス)へ、無ければ order1へ
                "channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                "body_template": "{{full_name}}様、初回メール",
                "reaction_channels": [TouchChannel.EMAIL_CLICK, TouchChannel.CONTENT_DOWNLOAD],
                "on_reaction_next_order": 2, "on_no_reaction_next_order": 1,
            },
            {  # order 1: 反応が無かった場合のデフォルトフォロー(架電)
                "channel": SequenceStepChannel.CALL_TASK, "delay_days": 3,
                "body_template": "{{full_name}}様へ架電する(反応なしフォロー)。",
            },
            {  # order 2: 反応があった場合のホットパス
                "channel": SequenceStepChannel.EMAIL, "delay_days": 1,
                "subject_template": "ぜひ一度お話ししましょう",
                "body_template": "{{full_name}}様、ご関心ありがとうございます(ホットパス)。",
            },
        ]

    def test_reacted_jumps_to_reaction_target(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="分岐S", description=None,
            steps=self._branching_steps(), actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()
        sq.generate_due_drafts(db_session, tenant_id, now=NOW)  # order0生成

        record_touch(
            db_session, tenant_id, lead, channel=TouchChannel.CONTENT_DOWNLOAD,
            occurred_at=NOW + timedelta(hours=1),
        )
        db_session.flush()

        drafts = sq.generate_due_drafts(db_session, tenant_id, now=NOW + timedelta(days=3))
        assert len(drafts) == 1
        assert "ホットパス" in drafts[0].body  # order2(反応ありパス)に飛んだ

    def test_no_reaction_takes_default_no_reaction_path(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="分岐S", description=None,
            steps=self._branching_steps(), actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()
        sq.generate_due_drafts(db_session, tenant_id, now=NOW)  # order0生成、反応なし

        drafts = sq.generate_due_drafts(db_session, tenant_id, now=NOW + timedelta(days=3))
        assert len(drafts) == 1
        assert "反応なしフォロー" in drafts[0].body  # order1(反応なしパス)に飛んだ

    def test_sequence_end_sentinel_completes_immediately(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="即終了S", description=None,
            steps=[{
                "channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                "body_template": "{{full_name}}",
                "reaction_channels": [TouchChannel.EMAIL_CLICK],
                "on_no_reaction_next_order": sq.SEQUENCE_END,
            }],
            actor="human:m",
        )
        enrollment = sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()
        sq.generate_due_drafts(db_session, tenant_id, now=NOW)  # order0生成(delay_days=0固有ステップのみ→即next_action_atなし想定)

        # order0以降に既定ステップが無いため、order0自身のdelay_days(0)で
        # 次回チェックがスケジュールされる。反応なしのままチェックすると終了する。
        db_session.refresh(enrollment)
        assert enrollment.next_action_at is not None

        drafts = sq.generate_due_drafts(db_session, tenant_id, now=NOW + timedelta(hours=1))
        assert drafts == []
        db_session.refresh(enrollment)
        assert enrollment.status == SequenceEnrollmentStatus.COMPLETED


class TestReviewAndDismiss:
    def test_mark_reviewed_sets_status_and_reviewer(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=[{"channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                    "body_template": "x"}],
            actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()
        draft = sq.generate_due_drafts(db_session, tenant_id, now=NOW)[0]

        sq.mark_draft_reviewed(draft, reviewed_by="human:is-1", now=NOW)
        db_session.commit()

        assert draft.status == SequenceDraftStatus.REVIEWED
        assert draft.reviewed_by == "human:is-1"

    def test_dismiss_sets_status(self, db_session, tenant_id):
        lead = make_lead(db_session, tenant_id)
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=[{"channel": SequenceStepChannel.EMAIL, "delay_days": 0,
                    "body_template": "x"}],
            actor="human:m",
        )
        sq.enroll_lead(db_session, tenant_id, lead, sequence, actor="human:m", now=NOW)
        db_session.flush()
        draft = sq.generate_due_drafts(db_session, tenant_id, now=NOW)[0]

        sq.dismiss_draft(draft, reviewed_by="human:is-1", now=NOW)
        db_session.commit()

        assert draft.status == SequenceDraftStatus.DISMISSED


class TestSequenceFunnel:
    def test_computes_reach_percentage_per_step(self, db_session, tenant_id):
        sequence = sq.create_sequence(
            db_session, tenant_id, name="S", description=None,
            steps=DEMO_STEPS, actor="human:m",
        )
        lead_a = make_lead(db_session, tenant_id, full_name="A")
        lead_b = make_lead(db_session, tenant_id, full_name="B")

        sq.enroll_lead(db_session, tenant_id, lead_a, sequence, actor="human:m", now=NOW)
        sq.enroll_lead(db_session, tenant_id, lead_b, sequence, actor="human:m", now=NOW)
        db_session.flush()

        sq.generate_due_drafts(db_session, tenant_id, now=NOW)  # 両方ともstep0到達
        sq.generate_due_drafts(db_session, tenant_id, now=NOW + timedelta(days=3))  # 両方ともstep1到達
        db_session.commit()

        funnel = sq.sequence_funnel(db_session, tenant_id, sequence.id)
        assert funnel["total_enrolled"] == 2
        assert funnel["steps"][0]["reached_count"] == 2
        assert funnel["steps"][0]["reached_pct"] == 100
        assert funnel["steps"][1]["reached_count"] == 2
        assert funnel["steps"][2]["reached_count"] == 0
        assert funnel["steps"][2]["reached_pct"] == 0
        assert len(funnel["steps"][0]["entries"]) == 2

    def test_empty_sequence_has_zero_percent_not_division_error(self, db_session, tenant_id):
        sequence = sq.create_sequence(
            db_session, tenant_id, name="空S", description=None,
            steps=DEMO_STEPS, actor="human:m",
        )
        funnel = sq.sequence_funnel(db_session, tenant_id, sequence.id)
        assert funnel["total_enrolled"] == 0
        assert all(s["reached_pct"] == 0 for s in funnel["steps"])
