"""SSR UI(骨格 CRM 機能)の統合テスト。

/ui/graph 単体は tests/test_api_engagements.py 等で別途カバーしているため
ここではワークスペース〜ダッシュボード〜案件詳細〜提案承認〜情報投入〜
ステージ遷移〜Waiver〜VERIFIED昇格の一連の骨格を検証する。
"""

from __future__ import annotations

import uuid

from crm_mvp.enums import (
    AutonomyMode, Confidence, Criterion, GateKind, GateStrength, Stage,
)
from crm_mvp.models import (
    ActionItem, ExtractionProposal, FieldAutonomyPolicy, GatePolicy, GraphNode,
    IngestionSource, QualificationSlot, StageTransition, Waiver,
)

from .conftest import create_account_and_engagement


class TestWorkspaceGating:
    def test_dashboard_without_session_redirects_to_workspace(self, db_session):
        from fastapi.testclient import TestClient

        from crm_mvp.api import deps
        from crm_mvp.api.app import app

        app.dependency_overrides[deps.get_session] = lambda: db_session
        try:
            client = TestClient(app)  # Cookie 無しの素の状態
            resp = client.get("/ui/", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/ui/workspace")
        finally:
            app.dependency_overrides.pop(deps.get_session, None)

    def test_workspace_submit_sets_cookies_and_redirects(
        self, db_session, tenant_id,
    ):
        from fastapi.testclient import TestClient

        from crm_mvp.api import deps
        from crm_mvp.api.app import app

        app.dependency_overrides[deps.get_session] = lambda: db_session
        try:
            client = TestClient(app)
            resp = client.post(
                "/ui/workspace",
                data={"tenant_id": str(tenant_id), "actor_name": "田中"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/ui/"
            assert "crm_tenant_id" in resp.cookies
            assert "crm_actor_id" in resp.cookies
        finally:
            app.dependency_overrides.pop(deps.get_session, None)

    def test_workspace_submit_rejects_invalid_uuid(self, db_session):
        from fastapi.testclient import TestClient

        from crm_mvp.api import deps
        from crm_mvp.api.app import app

        app.dependency_overrides[deps.get_session] = lambda: db_session
        try:
            client = TestClient(app)
            resp = client.post(
                "/ui/workspace", data={"tenant_id": "not-a-uuid"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "workspace" in resp.headers["location"]
        finally:
            app.dependency_overrides.pop(deps.get_session, None)


class TestDashboard:
    def test_lists_engagements_for_current_tenant_only(
        self, ui_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        other_tenant = uuid.uuid4()
        from .conftest import set_tenant_context
        set_tenant_context(db_session, other_tenant)
        create_account_and_engagement(db_session, other_tenant)
        set_tenant_context(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert engagement.name in resp.text
        assert resp.text.count("テスト案件") == 1

    def test_hides_closed_deals_by_default(self, ui_client, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, stage=Stage.LEAD)
        _, closed = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        closed.name = "クローズ済み案件"
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "クローズ済み案件" not in resp.text

        resp_all = ui_client.get("/ui/?show_closed=true")
        assert resp_all.status_code == 200
        assert "クローズ済み案件" in resp_all.text

    def test_filters_by_owner_user_id(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import User

        user_a = User(
            tenant_id=tenant_id, name="担当A", email="a@example.com",
            function="Sales", role="BDM",
        )
        user_b = User(
            tenant_id=tenant_id, name="担当B", email="b@example.com",
            function="Sales", role="AM",
        )
        db_session.add_all([user_a, user_b])
        db_session.flush()
        _, eng_a = create_account_and_engagement(db_session, tenant_id)
        eng_a.name = "Aの案件"
        eng_a.owner_user_id = user_a.id
        _, eng_b = create_account_and_engagement(db_session, tenant_id)
        eng_b.name = "Bの案件"
        eng_b.owner_user_id = user_b.id
        db_session.commit()

        resp = ui_client.get(f"/ui/?owner_user_id={user_a.id}")
        assert resp.status_code == 200
        assert "Aの案件" in resp.text
        assert "Bの案件" not in resp.text


class TestEngagementCreation:
    def test_new_engagement_form_renders(self, ui_client):
        resp = ui_client.get("/ui/engagements/new")
        assert resp.status_code == 200

    def test_creates_account_and_engagement(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/engagements/new",
            data={"account_name": "新規取引先", "engagement_name": "新規案件"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/ui/engagements/" in resp.headers["location"]

        from crm_mvp.models import Engagement
        engagement = db_session.query(Engagement).filter_by(
            tenant_id=tenant_id, name="新規案件",
        ).one()
        assert engagement.stage == Stage.LEAD

    def test_blank_names_are_rejected(self, ui_client):
        resp = ui_client.post(
            "/ui/engagements/new",
            data={"account_name": "  ", "engagement_name": " "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "engagements/new" in resp.headers["location"]


class TestEngagementDetail:
    def test_renders_gate_and_criteria(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert engagement.name in resp.text
        assert "クオリフィケーション状況" in resp.text

    def test_other_tenant_engagement_is_404(self, ui_client, db_session):
        other_tenant = uuid.uuid4()
        from .conftest import set_tenant_context
        set_tenant_context(db_session, other_tenant)
        _, engagement = create_account_and_engagement(db_session, other_tenant)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 404

    def test_section_nav_ids_match_anchors(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert 'class="section-nav"' in resp.text
        for section_id in ["score", "activity", "line-items", "quotes", "contracts",
                            "child-engagements", "stage", "qualification", "proposals",
                            "sources", "graph"]:
            assert f'href="#{section_id}"' in resp.text
            assert f'id="{section_id}"' in resp.text


class TestSourceIntake:
    def test_submits_and_processes_source(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/sources/new",
            data={
                "engagement_id": str(engagement.id), "kind": "free_note",
                "raw_text": "テストメモ",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith(
            f"/ui/engagements/{engagement.id}"
        )

        source = db_session.query(IngestionSource).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert source.processed_at is not None

    def test_blank_text_is_rejected(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/sources/new",
            data={"engagement_id": str(engagement.id), "kind": "free_note", "raw_text": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "sources/new" in resp.headers["location"]

    def test_calendar_sync_attendees_create_placeholder_nodes(
        self, ui_client, db_session, tenant_id,
    ):
        account, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/sources/new",
            data={
                "engagement_id": str(engagement.id), "kind": "calendar_sync",
                "raw_text": "定例会議の議事録",
                "attendees": "山田 太郎, yamada@example.com\n鈴木 花子\n",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        source = db_session.query(IngestionSource).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert source.kind == "calendar_sync"
        assert len(source.participants) == 2

        nodes = db_session.query(GraphNode).filter_by(
            tenant_id=tenant_id, account_id=account.id,
        ).all()
        labels = {n.placeholder_label for n in nodes}
        assert "山田 太郎(氏名未確認)" in labels
        assert "鈴木 花子(氏名未確認)" in labels

    def test_non_calendar_kind_ignores_attendees_field(
        self, ui_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            "/ui/sources/new",
            data={
                "engagement_id": str(engagement.id), "kind": "free_note",
                "raw_text": "テストメモ", "attendees": "山田 太郎",
            },
            follow_redirects=False,
        )
        source = db_session.query(IngestionSource).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert source.participants == []


class TestQuickNote:
    def test_form_lists_recent_engagements(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/quick-note")
        assert resp.status_code == 200
        assert engagement.name in resp.text

    def test_submits_as_free_note_and_redirects_to_quick_note(
        self, ui_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/quick-note",
            data={"engagement_id": str(engagement.id), "raw_text": "電話でのメモ"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/ui/quick-note")

        source = db_session.query(IngestionSource).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert source.kind == "free_note"
        assert source.processed_at is not None

    def test_blank_text_is_rejected(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/quick-note",
            data={"engagement_id": str(engagement.id), "raw_text": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        assert db_session.query(IngestionSource).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count() == 0

    def test_no_owner_filter_is_flat_list(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/quick-note")
        assert resp.status_code == 200
        assert "<optgroup" not in resp.text
        assert engagement.name in resp.text

    def test_owner_filter_groups_into_optgroups(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import User

        owner = User(
            tenant_id=tenant_id, name="担当A", email="qn-a@example.com",
            function="Sales", role="BDM",
        )
        db_session.add(owner)
        db_session.flush()
        _, mine = create_account_and_engagement(db_session, tenant_id)
        mine.name = "自分の案件"
        mine.owner_user_id = owner.id
        _, others = create_account_and_engagement(db_session, tenant_id)
        others.name = "他人の案件"
        db_session.commit()

        resp = ui_client.get(f"/ui/quick-note?owner_user_id={owner.id}")
        assert resp.status_code == 200
        assert '<optgroup label="自分の担当">' in resp.text
        assert '<optgroup label="その他(直近更新)">' in resp.text
        own_section = resp.text.split('その他(直近更新)')[0]
        assert "自分の案件" in own_section
        assert "他人の案件" not in own_section

    def test_owner_select_shows_all_users_and_marks_selected(
        self, ui_client, db_session, tenant_id,
    ):
        from crm_mvp.models import User

        owner = User(
            tenant_id=tenant_id, name="担当B", email="qn-b@example.com",
            function="Sales", role="AM",
        )
        db_session.add(owner)
        db_session.flush()
        create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/quick-note?owner_user_id={owner.id}")
        assert resp.status_code == 200
        assert 'id="owner_user_id"' in resp.text
        assert f'<option value="{owner.id}" selected>担当B</option>' in resp.text

    def test_post_redirect_preserves_owner_user_id(
        self, ui_client, db_session, tenant_id,
    ):
        from crm_mvp.models import User

        owner = User(
            tenant_id=tenant_id, name="担当C", email="qn-c@example.com",
            function="Sales", role="CS",
        )
        db_session.add(owner)
        db_session.flush()
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/quick-note",
            data={
                "engagement_id": str(engagement.id), "raw_text": "電話メモ",
                "owner_user_id": str(owner.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith(f"/ui/quick-note?owner_user_id={owner.id}")


class TestProposalInbox:
    def _make_pending_proposal(self, db_session, tenant_id, engagement):
        source = IngestionSource(
            tenant_id=tenant_id, engagement_id=engagement.id, kind="free_note",
            raw_text="メモ",
        )
        db_session.add(source)
        db_session.flush()
        proposal = ExtractionProposal(
            tenant_id=tenant_id, source_id=source.id, engagement_id=engagement.id,
            target_type="qualification_slot", field_path="criterion:budget",
            proposed_value={"amount": 1000000}, model_score=0.8,
            rationale="test", evidence_quote="発言の引用", status="pending",
        )
        db_session.add(proposal)
        db_session.flush()
        return proposal

    def test_inbox_lists_pending_proposals(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        self._make_pending_proposal(db_session, tenant_id, engagement)
        db_session.commit()

        resp = ui_client.get("/ui/proposals")
        assert resp.status_code == 200
        assert "budget" in resp.text

    def test_accept_applies_and_redirects(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = self._make_pending_proposal(db_session, tenant_id, engagement)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/proposals/{proposal.id}/accept",
            data={"redirect_to": "/ui/proposals"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        slot = db_session.query(QualificationSlot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.BUDGET,
        ).one()
        assert slot.value == {"amount": 1000000}

    def test_reject_records_status(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = self._make_pending_proposal(db_session, tenant_id, engagement)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/proposals/{proposal.id}/reject",
            data={"redirect_to": "/ui/proposals"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(proposal)
        assert proposal.status == "rejected"

    def test_accept_stores_rep_comment(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = self._make_pending_proposal(db_session, tenant_id, engagement)
        db_session.commit()

        ui_client.post(
            f"/ui/proposals/{proposal.id}/accept",
            data={"redirect_to": "/ui/proposals",
                  "rep_comment": "抽出は合っているが、実際は先方から追加で確認あり"},
            follow_redirects=False,
        )
        db_session.refresh(proposal)
        assert proposal.rep_comment == "抽出は合っているが、実際は先方から追加で確認あり"

    def test_reject_stores_rep_comment(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = self._make_pending_proposal(db_session, tenant_id, engagement)
        db_session.commit()

        ui_client.post(
            f"/ui/proposals/{proposal.id}/reject",
            data={"redirect_to": "/ui/proposals", "rep_comment": "誤抽出"},
            follow_redirects=False,
        )
        db_session.refresh(proposal)
        assert proposal.rep_comment == "誤抽出"

    def test_calendar_sync_proposal_bypasses_auto_apply_policy(
        self, ui_client, db_session, tenant_id,
    ):
        """route_proposals の force_confirm が process_source 経由でも効くこと
        (ingestion_runner.process_source -> route_proposals(source_kind=...))。"""
        from crm_mvp.ports.extractor import ExtractorPort
        from crm_mvp.schemas.extraction import ExtractedClaim, ExtractionResult
        from crm_mvp.services.ingestion_runner import process_source

        class _AlwaysAppliesExtractor(ExtractorPort):
            def extract(self, request):
                return ExtractionResult(
                    claims=[ExtractedClaim(
                        target_type="qualification_slot",
                        field_path="criterion:budget",
                        value={"amount": 1}, model_score=0.99,
                        rationale="発言から抽出", evidence_quote="発言の引用文",
                    )],
                    extractor_version="test-v1",
                )

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.add(FieldAutonomyPolicy(
            tenant_id=tenant_id, target_type="qualification_slot",
            field_path="criterion:budget", mode=AutonomyMode.ALWAYS_AUTO,
        ))
        source = IngestionSource(
            tenant_id=tenant_id, engagement_id=engagement.id, kind="calendar_sync",
            raw_text="会議録",
        )
        db_session.add(source)
        db_session.flush()

        outcome = process_source(
            db_session, tenant_id, source, extractor=_AlwaysAppliesExtractor(),
        )
        assert outcome.auto_applied == 0
        assert outcome.pending == 1


class TestStageTransitionUi:
    def test_advances_stage_when_unblocked(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/stage",
            data={"to_stage": "prospect"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(engagement)
        assert engagement.stage == "prospect"

    def test_mark_as_lost_bypasses_gate(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/stage",
            data={"to_stage": "closed_lost", "lost_reason": "競合A社に決定"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(engagement)
        assert engagement.stage == "closed_lost"
        assert engagement.lost_reason == "競合A社に決定"

    def test_mark_as_lost_without_reason_is_rejected(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.NEGOTIATION)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/stage",
            data={"to_stage": "closed_lost"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        db_session.refresh(engagement)
        assert engagement.stage == "negotiation"

    def test_blocked_transition_without_waiver_shows_error(
        self, ui_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)
        db_session.add(GatePolicy(
            tenant_id=tenant_id, code="stage.qualified", version=1,
            industry_template="manufacturing", kind=GateKind.STAGE,
            strength=GateStrength.REQUIRE_APPROVAL, to_stage=Stage.QUALIFIED,
            conditions={"slots": [{"criterion": "budget", "min_confidence": "verified"}]},
            is_active=True,
        ))
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/stage",
            data={"to_stage": "qualified"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        db_session.refresh(engagement)
        assert engagement.stage == "prospect"


class TestWaiverUi:
    def test_creates_waiver(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        policy = GatePolicy(
            tenant_id=tenant_id, code="stage.qualified", version=1,
            kind=GateKind.STAGE, strength=GateStrength.REQUIRE_APPROVAL,
            to_stage=Stage.QUALIFIED, conditions={}, is_active=True,
        )
        db_session.add(policy)
        db_session.flush()
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/waivers",
            data={"policy_id": str(policy.id), "reason": "経営判断により先行"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        waiver = db_session.query(Waiver).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert waiver.reason == "経営判断により先行"


class TestVerifySlotUi:
    def test_customer_document_promotes_to_verified(
        self, ui_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        slot = QualificationSlot(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.BUDGET, value={"amount": 1},
            confidence=Confidence.CORROBORATED,
        )
        db_session.add(slot)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/slots/budget/verify",
            data={"method": "customer_document", "evidence_uri": "s3://x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(slot)
        assert slot.confidence == Confidence.VERIFIED

    def test_missing_evidence_shows_error(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        slot = QualificationSlot(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.BUDGET, value={"amount": 1},
            confidence=Confidence.CORROBORATED,
        )
        db_session.add(slot)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/slots/budget/verify",
            data={"method": "customer_document"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        db_session.refresh(slot)
        assert slot.confidence == Confidence.CORROBORATED


class TestActionItemUi:
    def _seed_policy_with_missing_criterion(self, db_session, tenant_id):
        policy = GatePolicy(
            tenant_id=tenant_id, code="stage.prospect", version=1,
            kind=GateKind.STAGE, strength=GateStrength.WARN,
            to_stage=Stage.PROSPECT, industry_template="manufacturing",
            conditions={"slots": [
                {"criterion": "identified_pain", "min_confidence": "asserted"},
            ]},
            is_active=True,
        )
        db_session.add(policy)
        db_session.flush()
        return policy

    def test_assign_creates_open_item_from_current_gate(
        self, ui_client, db_session, tenant_id,
    ):
        self._seed_policy_with_missing_criterion(db_session, tenant_id)
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions",
            data={"assigned_to": "佐藤 健"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert item.assigned_to == "佐藤 健"
        assert item.status == "open"
        assert item.field_path == "criterion:identified_pain"

    def test_assign_blank_name_is_rejected(self, ui_client, db_session, tenant_id):
        self._seed_policy_with_missing_criterion(db_session, tenant_id)
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions",
            data={"assigned_to": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        assert db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count() == 0

    def test_complete_marks_done(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = ActionItem(
            tenant_id=tenant_id, engagement_id=engagement.id,
            field_path="criterion:champion", reason="reason", play="play",
            assigned_to="鈴木 花子", written_by="human:manager",
        )
        db_session.add(item)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions/{item.id}/complete",
            data={"note": "完了しました"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.status == "done"
        assert item.completed_note == "完了しました"

    def test_dismiss_marks_dismissed(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = ActionItem(
            tenant_id=tenant_id, engagement_id=engagement.id,
            field_path="criterion:champion", reason="reason", play="play",
            assigned_to="鈴木 花子", written_by="human:manager",
        )
        db_session.add(item)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions/{item.id}/dismiss",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.status == "dismissed"


class TestSidebarNav:
    def test_active_page_gets_active_class(self, ui_client):
        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert 'href="/ui/" class="active"' in resp.text
        assert 'href="/ui/accounts" class="' in resp.text
        assert 'href="/ui/accounts" class="active"' not in resp.text

    def test_master_data_section_auto_expands_when_active(self, ui_client):
        resp = ui_client.get("/ui/products")
        assert resp.status_code == 200
        assert "<details open>" in resp.text
        assert 'href="/ui/products" class="active"' in resp.text

    def test_master_data_section_collapsed_elsewhere(self, ui_client):
        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "<details open>" not in resp.text

    def test_quick_note_link_has_no_owner_param_by_default(self, ui_client):
        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert 'href="/ui/quick-note"' in resp.text

    def test_quick_note_link_propagates_owner_param(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import User

        owner = User(
            tenant_id=tenant_id, name="担当A", email="nav-a@example.com",
            function="Sales", role="BDM",
        )
        db_session.add(owner)
        db_session.commit()

        resp = ui_client.get(f"/ui/?owner_user_id={owner.id}")
        assert resp.status_code == 200
        assert f'href="/ui/quick-note?owner_user_id={owner.id}"' in resp.text
