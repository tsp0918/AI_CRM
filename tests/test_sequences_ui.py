"""シーケンス自動化UI(sequences.py + leads.py の統合部分)の統合テスト。"""

from __future__ import annotations

from crm_mvp.models import Lead, Sequence, SequenceDraft, SequenceEnrollment, SequenceStep


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


class TestSequenceCreation:
    def test_creates_sequence_with_steps_from_fixed_rows(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/sequences/new",
            data={
                "name": "新規開拓3ステップ", "description": "製造業向け",
                "channel_0": "email", "delay_days_0": "0",
                "subject_0": "{{company_name}}様へのご案内",
                "body_0": "{{full_name}}様、お世話になっております。",
                "channel_1": "call_task", "delay_days_1": "3",
                "body_1": "{{full_name}}様へ架電する",
                # 2-4行目は空欄のまま(無視される)
                "channel_2": "", "body_2": "",
                "channel_3": "", "body_3": "",
                "channel_4": "", "body_4": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        sequence = db_session.query(Sequence).filter_by(
            tenant_id=tenant_id, name="新規開拓3ステップ",
        ).one()
        steps = db_session.query(SequenceStep).filter_by(
            tenant_id=tenant_id, sequence_id=sequence.id,
        ).order_by(SequenceStep.step_order).all()
        assert len(steps) == 2
        assert steps[0].channel == "email"
        assert steps[1].channel == "call_task"

    def test_creates_sequence_with_branching_config(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/sequences/new",
            data={
                "name": "分岐あり", "description": "",
                "channel_0": "email", "delay_days_0": "0",
                "body_0": "{{full_name}}様、初回メール",
                "reaction_channels_0": ["email_click", "content_download"],
                "on_reaction_next_0": "2", "on_no_reaction_next_0": "1",
                "channel_1": "call_task", "delay_days_1": "3",
                "body_1": "反応なしフォロー架電",
                "channel_2": "email", "delay_days_2": "1",
                "body_2": "ホットパスメール",
                "on_no_reaction_next_2": "end",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        sequence = db_session.query(Sequence).filter_by(
            tenant_id=tenant_id, name="分岐あり",
        ).one()
        steps = db_session.query(SequenceStep).filter_by(
            tenant_id=tenant_id, sequence_id=sequence.id,
        ).order_by(SequenceStep.step_order).all()

        assert steps[0].on_reaction_next_order == 2
        assert steps[0].on_no_reaction_next_order == 1
        assert set(steps[0].reaction_channels) == {"email_click", "content_download"}
        assert steps[2].on_no_reaction_next_order == -1

    def test_blank_name_is_rejected(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/sequences/new",
            data={"name": "  ", "channel_0": "email", "body_0": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_no_steps_filled_is_rejected(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/sequences/new",
            data={"name": "空シーケンス"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        assert db_session.query(Sequence).filter_by(
            tenant_id=tenant_id, name="空シーケンス",
        ).count() == 0


class TestLeadSequenceEnrollment:
    def _make_sequence(self, ui_client, name="S1"):
        ui_client.post(
            "/ui/sequences/new",
            data={
                "name": name,
                "channel_0": "email", "delay_days_0": "0",
                "body_0": "{{full_name}}様、こんにちは",
            },
        )

    def test_enroll_and_generate_and_review_draft(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client)
        lead = make_lead(db_session, tenant_id)
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="S1").one()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/sequences",
            data={"sequence_id": str(sequence.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        enrollment = db_session.query(SequenceEnrollment).filter_by(
            tenant_id=tenant_id, lead_id=lead.id,
        ).one()
        assert enrollment.status == "active"

        # 手動トリガーでドラフトを生成
        gen_resp = ui_client.post("/ui/sequences/generate-due", follow_redirects=False)
        assert gen_resp.status_code == 303

        draft = db_session.query(SequenceDraft).filter_by(
            tenant_id=tenant_id, enrollment_id=enrollment.id,
        ).one()
        assert "山田 太郎様" in draft.body
        assert draft.status == "draft"

        review_resp = ui_client.post(
            f"/ui/leads/{lead.id}/drafts/{draft.id}/review", follow_redirects=False,
        )
        assert review_resp.status_code == 303
        db_session.refresh(draft)
        assert draft.status == "reviewed"

    def test_double_enroll_shows_error(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client, name="S2")
        lead = make_lead(db_session, tenant_id)
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="S2").one()

        ui_client.post(
            f"/ui/leads/{lead.id}/sequences", data={"sequence_id": str(sequence.id)},
        )
        resp = ui_client.post(
            f"/ui/leads/{lead.id}/sequences",
            data={"sequence_id": str(sequence.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_opt_out_stops_enrollment(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client, name="S3")
        lead = make_lead(db_session, tenant_id)
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="S3").one()

        ui_client.post(
            f"/ui/leads/{lead.id}/sequences", data={"sequence_id": str(sequence.id)},
        )
        enrollment = db_session.query(SequenceEnrollment).filter_by(
            tenant_id=tenant_id, lead_id=lead.id,
        ).one()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/sequences/{enrollment.id}/opt-out",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(enrollment)
        assert enrollment.status == "opted_out"

    def test_dismiss_draft(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client, name="S4")
        lead = make_lead(db_session, tenant_id)
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="S4").one()

        ui_client.post(f"/ui/leads/{lead.id}/sequences", data={"sequence_id": str(sequence.id)})
        ui_client.post("/ui/sequences/generate-due")
        draft = db_session.query(SequenceDraft).join(
            SequenceEnrollment, SequenceDraft.enrollment_id == SequenceEnrollment.id,
        ).filter(SequenceEnrollment.lead_id == lead.id).one()

        resp = ui_client.post(
            f"/ui/leads/{lead.id}/drafts/{draft.id}/dismiss", follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(draft)
        assert draft.status == "dismissed"


class TestBulkEnroll:
    def _make_sequence(self, ui_client, name="Bulk1"):
        ui_client.post(
            "/ui/sequences/new",
            data={
                "name": name,
                "channel_0": "email", "delay_days_0": "0",
                "body_0": "{{full_name}}様、こんにちは",
            },
        )

    def test_enrolls_multiple_leads_at_once(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client)
        lead1 = make_lead(db_session, tenant_id, full_name="鈴木 一郎")
        lead2 = make_lead(db_session, tenant_id, full_name="佐藤 花子")
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="Bulk1").one()

        resp = ui_client.post(
            "/ui/leads/bulk-enroll",
            data={"lead_ids": [str(lead1.id), str(lead2.id)], "sequence_id": str(sequence.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        enrollments = db_session.query(SequenceEnrollment).filter_by(
            tenant_id=tenant_id, sequence_id=sequence.id,
        ).all()
        assert {e.lead_id for e in enrollments} == {lead1.id, lead2.id}

    def test_skips_already_enrolled_leads(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client, name="Bulk2")
        lead = make_lead(db_session, tenant_id)
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="Bulk2").one()

        ui_client.post(f"/ui/leads/{lead.id}/sequences", data={"sequence_id": str(sequence.id)})

        resp = ui_client.post(
            "/ui/leads/bulk-enroll",
            data={"lead_ids": [str(lead.id)], "sequence_id": str(sequence.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert db_session.query(SequenceEnrollment).filter_by(
            tenant_id=tenant_id, lead_id=lead.id,
        ).count() == 1

    def test_no_leads_selected_is_rejected(self, ui_client, db_session, tenant_id):
        self._make_sequence(ui_client, name="Bulk3")
        sequence = db_session.query(Sequence).filter_by(tenant_id=tenant_id, name="Bulk3").one()

        resp = ui_client.post(
            "/ui/leads/bulk-enroll",
            data={"sequence_id": str(sequence.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestPipelineView:
    def test_shows_step_positions_after_progressing(self, ui_client, db_session, tenant_id):
        ui_client.post(
            "/ui/sequences/new",
            data={
                "name": "3ステップ表示確認",
                "channel_0": "email", "delay_days_0": "0", "body_0": "{{full_name}}様1通目",
                "channel_1": "call_task", "delay_days_1": "3", "body_1": "架電",
                "channel_2": "email", "delay_days_2": "4", "body_2": "{{full_name}}様3通目",
            },
        )
        lead = make_lead(db_session, tenant_id)
        db_session.commit()
        sequence = db_session.query(Sequence).filter_by(
            tenant_id=tenant_id, name="3ステップ表示確認",
        ).one()

        ui_client.post(f"/ui/leads/{lead.id}/sequences", data={"sequence_id": str(sequence.id)})
        ui_client.post("/ui/sequences/generate-due")  # ステップ1生成

        resp = ui_client.get(f"/ui/leads/{lead.id}")
        assert resp.status_code == 200
        assert "実行中" in resp.text
        assert "未到達" in resp.text
