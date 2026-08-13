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
                        rationale="t", evidence_quote="発言",
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
            data={"to_stage": "closed_lost"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(engagement)
        assert engagement.stage == "closed_lost"

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
