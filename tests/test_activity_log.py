"""案件の活動ログ(StageTransition/Proposal/Waiver/Verification/Source の
マージ)のテスト。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from crm_mvp.enums import (
    Confidence, Criterion, GateKind, GateStrength, ProposalStatus, Stage,
)
from crm_mvp.models import (
    ExtractionProposal, GatePolicy, IngestionSource, QualificationSlot,
    StageTransition, Waiver,
)
from crm_mvp.services.activity_log import load_activity_log

from .conftest import create_account_and_engagement


class TestLoadActivityLog:
    def test_merges_all_sources_sorted_by_time_desc(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)

        db_session.add(StageTransition(
            tenant_id=tenant_id, engagement_id=engagement.id,
            from_stage=Stage.LEAD, to_stage=Stage.PROSPECT,
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            gate_snapshot={"satisfied": True, "missing": []},
            written_by="human:abc",
        ))

        source = IngestionSource(
            tenant_id=tenant_id, engagement_id=engagement.id, kind="free_note",
            raw_text="商談メモ", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        db_session.add(source)
        db_session.flush()

        db_session.add(ExtractionProposal(
            tenant_id=tenant_id, source_id=source.id, engagement_id=engagement.id,
            target_type="qualification_slot", field_path="criterion:budget",
            proposed_value={"amount": 100}, model_score=0.9,
            evidence_quote="予算は100万円です", status=ProposalStatus.ACCEPTED,
            decided_by=uuid.uuid4(),
            decided_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        ))

        policy = GatePolicy(
            tenant_id=tenant_id, code="stage.qualified", version=1,
            kind=GateKind.STAGE, strength=GateStrength.REQUIRE_APPROVAL,
            to_stage=Stage.QUALIFIED, conditions={}, is_active=True,
        )
        db_session.add(policy)
        db_session.flush()

        db_session.add(Waiver(
            tenant_id=tenant_id, engagement_id=engagement.id,
            policy_id=policy.id, approved_by=uuid.uuid4(),
            reason="経営判断", approved_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
            written_by="human:def",
        ))

        db_session.add(QualificationSlot(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.BUDGET, value={"amount": 100},
            confidence=Confidence.VERIFIED,
            verified_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            verified_by=uuid.uuid4(),
            verification_method="customer_document",
        ))
        db_session.commit()

        items = load_activity_log(db_session, tenant_id, engagement)
        kinds_in_order = [i.kind for i in items]
        assert kinds_in_order == [
            "verification", "waiver", "proposal", "source", "stage_transition",
        ]
        assert items[0].occurred_at > items[-1].occurred_at

    def test_pending_proposal_shows_ai_as_actor(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        source = IngestionSource(
            tenant_id=tenant_id, engagement_id=engagement.id, kind="free_note",
            raw_text="メモ",
        )
        db_session.add(source)
        db_session.flush()
        db_session.add(ExtractionProposal(
            tenant_id=tenant_id, source_id=source.id, engagement_id=engagement.id,
            target_type="qualification_slot", field_path="criterion:timing",
            proposed_value={"target_date": "2026-12-01"}, model_score=0.7,
            evidence_quote="年内には導入したい", status=ProposalStatus.PENDING,
        ))
        db_session.commit()

        items = load_activity_log(db_session, tenant_id, engagement)
        proposal_items = [i for i in items if i.kind == "proposal"]
        assert len(proposal_items) == 1
        assert proposal_items[0].actor == "AI"
        assert "年内には導入したい" in proposal_items[0].detail

    def test_empty_engagement_has_no_activity(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()
        assert load_activity_log(db_session, tenant_id, engagement) == []


class TestActivityLogPage:
    def test_renders_timeline(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)
        db_session.add(StageTransition(
            tenant_id=tenant_id, engagement_id=engagement.id,
            from_stage=Stage.LEAD, to_stage=Stage.PROSPECT,
            occurred_at=datetime.now(timezone.utc),
            gate_snapshot={}, written_by="human:abc",
        ))
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}/activity")
        assert resp.status_code == 200
        assert "ステージ変更" in resp.text

    def test_detail_page_links_to_activity_log(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.get(f"/ui/engagements/{engagement.id}")
        assert resp.status_code == 200
        assert f"/ui/engagements/{engagement.id}/activity" in resp.text
