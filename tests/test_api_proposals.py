"""POST /proposals/{id}/accept|reject のテスト(HANDOVER.md §5 item10)。"""

from __future__ import annotations

import uuid

from crm_mvp.enums import Confidence, Criterion, ProposalStatus
from crm_mvp.models import ExtractionProposal, IngestionSource, QualificationSlot

from .conftest import create_account_and_engagement


def _make_pending_proposal(
    db_session, tenant_id, engagement,
    target_type="qualification_slot", field_path="criterion:budget",
    proposed_value=None,
) -> ExtractionProposal:
    source = IngestionSource(
        tenant_id=tenant_id, engagement_id=engagement.id, kind="free_note",
        raw_text="メモ",
    )
    db_session.add(source)
    db_session.flush()

    proposal = ExtractionProposal(
        tenant_id=tenant_id, source_id=source.id, engagement_id=engagement.id,
        target_type=target_type, field_path=field_path,
        proposed_value=proposed_value or {"amount": 1000000, "secured": True},
        model_score=0.8, rationale="test", evidence_quote="発言の引用",
        status=ProposalStatus.PENDING,
    )
    db_session.add(proposal)
    db_session.flush()
    return proposal


def test_accept_applies_to_qualification_slot(api_client, db_session, tenant_id):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    proposal = _make_pending_proposal(db_session, tenant_id, engagement)
    db_session.commit()

    resp = api_client.post(
        f"/proposals/{proposal.id}/accept",
        json={"decided_by": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    slot = db_session.query(QualificationSlot).filter_by(
        tenant_id=tenant_id, engagement_id=engagement.id,
        criterion=Criterion.BUDGET,
    ).one()
    assert slot.value == {"amount": 1000000, "secured": True}
    assert slot.confidence == Confidence.CORROBORATED


def test_accept_twice_returns_409(api_client, db_session, tenant_id):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    proposal = _make_pending_proposal(db_session, tenant_id, engagement)
    db_session.commit()

    first = api_client.post(
        f"/proposals/{proposal.id}/accept", json={"decided_by": str(uuid.uuid4())},
    )
    assert first.status_code == 200
    second = api_client.post(
        f"/proposals/{proposal.id}/accept", json={"decided_by": str(uuid.uuid4())},
    )
    assert second.status_code == 409


def test_reject_records_corrected_value(api_client, db_session, tenant_id):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    proposal = _make_pending_proposal(db_session, tenant_id, engagement)
    db_session.commit()

    resp = api_client.post(
        f"/proposals/{proposal.id}/reject",
        json={
            "decided_by": str(uuid.uuid4()),
            "corrected_value": {"amount": 2000000, "secured": False},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    db_session.refresh(proposal)
    assert proposal.corrected_value == {"amount": 2000000, "secured": False}
    # 却下されたので業務テーブルには反映されない
    assert db_session.query(QualificationSlot).filter_by(
        tenant_id=tenant_id, engagement_id=engagement.id,
    ).count() == 0


def test_accept_preserves_verified_confidence(api_client, db_session, tenant_id):
    """§3.2 回帰: 既に VERIFIED な slot に AI 提案を承認しても降格しない。"""
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    existing = QualificationSlot(
        tenant_id=tenant_id, engagement_id=engagement.id,
        criterion=Criterion.BUDGET, value={"amount": 500000},
        confidence=Confidence.VERIFIED,
    )
    db_session.add(existing)
    db_session.flush()

    proposal = _make_pending_proposal(
        db_session, tenant_id, engagement,
        proposed_value={"amount": 999999, "secured": True},
    )
    db_session.commit()

    resp = api_client.post(
        f"/proposals/{proposal.id}/accept", json={"decided_by": str(uuid.uuid4())},
    )
    assert resp.status_code == 200

    db_session.refresh(existing)
    assert existing.confidence == Confidence.VERIFIED
