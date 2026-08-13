"""提案承認インボックス。「AIが提案→人が1タップ承認」という
この製品の中核ワークフローの UI(HANDOVER.md §3.1/§4 参照)。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...enums import ProposalStatus, SourceKind
from ...models import Engagement, ExtractionProposal, IngestionSource
from ...services.apply_proposal import apply_proposal
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/proposals", response_class=HTMLResponse)
def proposals_inbox(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    proposals = session.execute(
        select(ExtractionProposal).where(
            ExtractionProposal.tenant_id == ui_session.tenant_id,
            ExtractionProposal.status == ProposalStatus.PENDING,
        ).order_by(ExtractionProposal.created_at.desc())
    ).scalars().all()

    engagement_ids = {p.engagement_id for p in proposals if p.engagement_id}
    engagements = {}
    if engagement_ids:
        engagements = {
            e.id: e for e in session.execute(
                select(Engagement).where(Engagement.id.in_(engagement_ids))
            ).scalars()
        }

    source_ids = {p.source_id for p in proposals if p.source_id}
    source_kinds = {}
    if source_ids:
        source_kinds = {
            s.id: s.kind for s in session.execute(
                select(IngestionSource).where(IngestionSource.id.in_(source_ids))
            ).scalars()
        }

    context = base_context(
        session, ui_session, active_nav="proposals", flash=flash, flash_type=flash_type,
    )
    context.update({
        "proposals": proposals, "engagements": engagements,
        "source_kinds": source_kinds, "calendar_sync": SourceKind.CALENDAR_SYNC,
    })
    return templates.TemplateResponse(request, "proposals.html", context)


def _get_pending_or_404(
    session: Session, ui_session: UiSession, proposal_id: uuid.UUID,
) -> ExtractionProposal:
    proposal = session.execute(
        select(ExtractionProposal).where(
            ExtractionProposal.tenant_id == ui_session.tenant_id,
            ExtractionProposal.id == proposal_id,
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return proposal


@router.post("/ui/proposals/{proposal_id}/accept")
def accept_proposal_ui(
    proposal_id: uuid.UUID,
    redirect_to: str = Form("/ui/proposals"),
    rep_comment: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    proposal = _get_pending_or_404(session, ui_session, proposal_id)
    if proposal.status != ProposalStatus.PENDING:
        return redirect_with_flash(redirect_to, "既に処理済みの提案です", "error")

    try:
        apply_proposal(session, proposal)
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(redirect_to, f"適用に失敗しました: {exc}", "error")

    proposal.status = ProposalStatus.ACCEPTED
    proposal.decided_by = ui_session.actor_id
    proposal.decided_at = datetime.now(timezone.utc)
    proposal.rep_comment = rep_comment.strip() or None
    session.commit()
    return redirect_with_flash(redirect_to, "提案を承認しました")


@router.post("/ui/proposals/{proposal_id}/reject")
def reject_proposal_ui(
    proposal_id: uuid.UUID,
    corrected_value: str = Form(""),
    rep_comment: str = Form(""),
    redirect_to: str = Form("/ui/proposals"),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    proposal = _get_pending_or_404(session, ui_session, proposal_id)
    if proposal.status != ProposalStatus.PENDING:
        return redirect_with_flash(redirect_to, "既に処理済みの提案です", "error")

    corrected: dict | None = None
    if corrected_value.strip():
        try:
            corrected = json.loads(corrected_value)
        except json.JSONDecodeError:
            return redirect_with_flash(
                redirect_to, "訂正値が正しいJSON形式ではありません", "error",
            )

    proposal.status = ProposalStatus.REJECTED
    proposal.decided_by = ui_session.actor_id
    proposal.decided_at = datetime.now(timezone.utc)
    proposal.corrected_value = corrected
    proposal.rep_comment = rep_comment.strip() or None
    session.commit()
    return redirect_with_flash(redirect_to, "提案を却下しました")
