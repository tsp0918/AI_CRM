"""提案の承認・却下 API(HANDOVER.md §5 Phase2, item 10)。

PENDING の ExtractionProposal のみを対象とする。承認は apply_proposal を
唯一の経路として業務テーブルへ反映する(§3.1)。却下時の corrected_value は
次の精度改善の教師データになる(ingestion.py のコメント参照)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ProposalStatus
from ..models import ExtractionProposal
from ..services.apply_proposal import apply_proposal
from .deps import get_session, get_tenant_id

router = APIRouter(prefix="/proposals", tags=["proposals"])


class ProposalOut(BaseModel):
    id: uuid.UUID
    target_type: str
    field_path: str
    proposed_value: dict
    status: ProposalStatus
    model_score: float
    evidence_quote: str | None


class AcceptRequest(BaseModel):
    decided_by: uuid.UUID


class RejectRequest(BaseModel):
    decided_by: uuid.UUID
    corrected_value: dict | None = None


def _get_pending_proposal(
    session: Session, tenant_id: uuid.UUID, proposal_id: uuid.UUID,
) -> ExtractionProposal:
    proposal = session.execute(
        select(ExtractionProposal).where(
            ExtractionProposal.tenant_id == tenant_id,
            ExtractionProposal.id == proposal_id,
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal.status != ProposalStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"proposal is not pending (status={proposal.status})",
        )
    return proposal


@router.post("/{proposal_id}/accept", response_model=ProposalOut)
def accept_proposal(
    proposal_id: uuid.UUID, body: AcceptRequest,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> ExtractionProposal:
    proposal = _get_pending_proposal(session, tenant_id, proposal_id)
    try:
        apply_proposal(session, proposal)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    proposal.status = ProposalStatus.ACCEPTED
    proposal.decided_by = body.decided_by
    proposal.decided_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(
    proposal_id: uuid.UUID, body: RejectRequest,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> ExtractionProposal:
    proposal = _get_pending_proposal(session, tenant_id, proposal_id)
    proposal.status = ProposalStatus.REJECTED
    proposal.decided_by = body.decided_by
    proposal.decided_at = datetime.now(timezone.utc)
    proposal.corrected_value = body.corrected_value
    session.commit()
    session.refresh(proposal)
    return proposal
