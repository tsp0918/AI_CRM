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


def _main_content(resp_text: str) -> str:
    """サイドバーには常設クイック入力ウィジェットの案件一覧が独立して
    出るため(2026-08-14)、ページ本文だけを対象にした文字列比較をしたい
    テストはこれで <main> 以降だけを取り出す。"""
    return resp_text.split('<main class="page">', 1)[-1]


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
        main = _main_content(resp.text)
        assert engagement.name in main
        assert main.count("テスト案件") == 1

    def test_hides_closed_deals_by_default(self, ui_client, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id, stage=Stage.LEAD)
        _, closed = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        closed.name = "クローズ済み案件"
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "クローズ済み案件" not in _main_content(resp.text)

        resp_all = ui_client.get("/ui/?show_closed=true")
        assert resp_all.status_code == 200
        assert "クローズ済み案件" in _main_content(resp_all.text)

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
        main = _main_content(resp.text)
        assert "Aの案件" in main
        assert "Bの案件" not in main


class TestDashboardRenewalWarning:
    def test_no_warning_when_no_unworked_renewals(self, ui_client, db_session, tenant_id):
        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "更新未着手の契約が" not in resp.text

    def test_warning_shows_overdue_unworked_renewal(self, ui_client, db_session, tenant_id):
        from datetime import date, timedelta
        from decimal import Decimal

        from crm_mvp.enums import ContractStatus, Stage
        from crm_mvp.models import Contract

        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        contract = Contract(
            tenant_id=tenant_id, engagement_id=eng.id, contract_number="C-TEST-0001",
            status=ContractStatus.ACTIVE, total_amount=Decimal("1000000"), currency="JPY",
            end_date=date.today() - timedelta(days=10), written_by="human:tester",
        )
        db_session.add(contract)
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "更新未着手の契約が" in resp.text
        assert "1件は期限超過" in resp.text

    def test_no_warning_once_renewal_engagement_exists(self, ui_client, db_session, tenant_id):
        from datetime import date, timedelta
        from decimal import Decimal

        from crm_mvp.enums import ContractStatus, EngagementRelationshipType, Stage
        from crm_mvp.models import Contract
        from crm_mvp.services.engagement_relationships import create_child_engagement

        _, eng = create_account_and_engagement(db_session, tenant_id, stage=Stage.CLOSED_WON)
        contract = Contract(
            tenant_id=tenant_id, engagement_id=eng.id, contract_number="C-TEST-0001",
            status=ContractStatus.ACTIVE, total_amount=Decimal("1000000"), currency="JPY",
            end_date=date.today() - timedelta(days=10), written_by="human:tester",
        )
        db_session.add(contract)
        create_child_engagement(
            db_session, tenant_id, eng, relationship_type=EngagementRelationshipType.RENEWAL,
            name="更新交渉",
        )
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "更新未着手の契約が" not in resp.text


class TestDashboardReviewBadge:
    def test_no_review_shows_unreviewed_badge(self, ui_client, db_session, tenant_id):
        create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        main = _main_content(resp.text)
        assert "未レビュー" in main
        assert "レビュー済み" not in main

    def test_review_with_comment_this_week_shows_reviewed_badge(
        self, ui_client, db_session, tenant_id,
    ):
        from crm_mvp.services.weekly_review import get_or_create_current_review, update_review

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.flush()
        review = get_or_create_current_review(
            db_session, tenant_id, engagement.id, actor="human:manager-1",
        )
        update_review(review, rep_comment="デモ実施完了")
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        main = _main_content(resp.text)
        assert "レビュー済み" in main
        assert "未レビュー" not in main

    def test_empty_review_row_still_counts_as_unreviewed(
        self, ui_client, db_session, tenant_id,
    ):
        from crm_mvp.services.weekly_review import get_or_create_current_review

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.flush()
        get_or_create_current_review(
            db_session, tenant_id, engagement.id, actor="human:manager-1",
        )
        db_session.commit()

        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert "未レビュー" in _main_content(resp.text)


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

    def test_section_nav_ids_match_anchors_rep_tab(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert 'class="section-nav"' in resp.text
        for section_id in ["line-items", "quotes", "contracts",
                            "child-engagements", "stage", "qualification", "proposals",
                            "sources"]:
            assert f'href="#{section_id}"' in resp.text
            assert f'id="{section_id}"' in resp.text
        # 固定エリアのidは常に出るが、section-navのリンクとしては出ない
        assert 'id="score"' in resp.text
        assert 'id="weekly-review"' in resp.text
        assert 'id="actions"' in resp.text

    def test_section_nav_ids_match_anchors_manager_tab(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}?tab=manager")
        assert resp.status_code == 200
        for section_id in ["activity", "graph"]:
            assert f'href="#{section_id}"' in resp.text
            assert f'id="{section_id}"' in resp.text

    def test_tab_switch_shows_different_cards(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        rep_resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert 'id="line-items"' in rep_resp.text
        assert 'id="graph"' not in rep_resp.text

        manager_resp = ui_client.get(f"/ui/engagements/{engagement.id}?tab=manager")
        assert 'id="graph"' in manager_resp.text
        assert 'id="line-items"' not in manager_resp.text


class TestWeeklyReviewUi:
    def test_visiting_detail_page_shows_current_week_card(
        self, ui_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert "週次レビュー" in resp.text
        assert f'action="/ui/engagements/{engagement.id}/review"' in resp.text

    def test_saves_comments_and_status(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/review",
            data={
                "rep_comment": "来週デモ予定",
                "manager_comment": "順調に進んでいる",
                "manager_status": "on_track",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        from crm_mvp.models import WeeklyReview
        review = db_session.query(WeeklyReview).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert review.rep_comment == "来週デモ予定"
        assert review.manager_comment == "順調に進んでいる"
        assert review.manager_status == "on_track"

        resp2 = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert "来週デモ予定" in resp2.text
        assert "順調" in resp2.text

    def test_no_baseline_message_when_no_snapshot(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert "前週との比較データがまだありません" in resp.text

    def test_manual_action_task_shows_in_todo_table(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            f"/ui/engagements/{engagement.id}/actions/manual",
            data={"assigned_to": "鈴木 花子", "task": "1on1タスクA", "due_at": "2020-01-01"},
        )

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert "1on1タスクA" in resp.text
        assert "超過" in resp.text  # 期限が過去なので超過バッジが出る


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

    def test_no_owner_filter_shows_recent_engagements(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/quick-note")
        assert resp.status_code == 200
        assert engagement.name in resp.text

    def test_set_owner_endpoint_sets_cookie_and_redirects_to_next(
        self, ui_client, db_session, tenant_id,
    ):
        from crm_mvp.models import User

        owner = User(
            tenant_id=tenant_id, name="担当A", email="qn-a@example.com",
            function="Sales", role="BDM",
        )
        db_session.add(owner)
        db_session.commit()

        resp = ui_client.post(
            "/ui/quick-note/owner",
            data={"owner_user_id": str(owner.id), "next": "/ui/leads"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/leads"
        assert ui_client.cookies.get("crm_quicknote_owner_id") == str(owner.id)

    def test_set_owner_rejects_unsafe_next(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/quick-note/owner",
            data={"owner_user_id": "", "next": "https://evil.example.com/"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/"

    def test_owner_cookie_truly_filters_engagement_list(self, ui_client, db_session, tenant_id):
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

        ui_client.post(
            "/ui/quick-note/owner",
            data={"owner_user_id": str(owner.id), "next": "/ui/quick-note"},
        )

        resp = ui_client.get("/ui/quick-note")
        assert resp.status_code == 200
        assert "自分の案件" in resp.text
        assert "他人の案件" not in resp.text

    def test_owner_select_marks_selected_option(self, ui_client, db_session, tenant_id):
        from crm_mvp.models import User

        owner = User(
            tenant_id=tenant_id, name="担当B", email="qn-b@example.com",
            function="Sales", role="AM",
        )
        db_session.add(owner)
        db_session.flush()
        create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        ui_client.post(
            "/ui/quick-note/owner",
            data={"owner_user_id": str(owner.id), "next": "/ui/quick-note"},
        )

        resp = ui_client.get("/ui/quick-note")
        assert resp.status_code == 200
        assert f'<option value="{owner.id}" selected>担当B</option>' in resp.text

    def test_post_redirects_to_next(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/quick-note",
            data={
                "engagement_id": str(engagement.id), "raw_text": "電話メモ",
                "next": "/ui/leads",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/ui/leads")

    def test_post_rejects_unsafe_next(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            "/ui/quick-note",
            data={
                "engagement_id": str(engagement.id), "raw_text": "電話メモ",
                "next": "https://evil.example.com/",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/ui/quick-note")

    def test_sidebar_widget_appears_on_other_pages(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get("/ui/leads")
        assert resp.status_code == 200
        assert 'class="sidebar-quicknote"' in resp.text
        assert engagement.name in resp.text


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

    def test_assign_with_due_at_stores_it(self, ui_client, db_session, tenant_id):
        self._seed_policy_with_missing_criterion(db_session, tenant_id)
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions",
            data={"assigned_to": "佐藤 健", "due_at": "2026-08-20"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert item.due_at is not None
        assert item.due_at.date().isoformat() == "2026-08-20"

    def test_manual_task_creates_open_item(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions/manual",
            data={"assigned_to": "鈴木 花子", "task": "1on1で決めたタスク", "due_at": "2026-08-21"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        item = db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert item.field_path == "manual"
        assert item.reason == "1on1で決めたタスク"
        assert item.status == "open"

    def test_manual_task_blank_fields_rejected(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/actions/manual",
            data={"assigned_to": "  ", "task": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
        assert db_session.query(ActionItem).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).count() == 0


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

    def test_quick_note_link_has_no_query_params(self, ui_client):
        # 担当者の選択はもう常設サイドバーウィジェット側のCookieで管理する
        # ため(2026-08-14)、専用ページへのリンク自体は常に固定URL。
        resp = ui_client.get("/ui/")
        assert resp.status_code == 200
        assert 'href="/ui/quick-note"' in resp.text
