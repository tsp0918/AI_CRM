"""R&D起点商談の一覧・商談化承認UI(2026-08-15, CRM_連携引き継ぎ書.md §7.5 C4-4)。

`Stage.RND_INCUBATION`はパイプライン集計から除外される隔離ステージ
(`crm_mvp/services/rnd_opportunity.py`)。この画面はその隔離された商談を
一覧し、正式に商談化(通常のLEADステージへ昇格)する操作だけを提供する —
それ以外の編集はできない(通常の商談編集画面と同じ導線に合流させるため)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...enums import Stage
from ...models import Account, Engagement
from ...services.rnd_opportunity import promote_rnd_engagement
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/rnd-opportunities", response_class=HTMLResponse)
def rnd_opportunities_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    engagements = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == ui_session.tenant_id,
            Engagement.stage == Stage.RND_INCUBATION,
        ).order_by(Engagement.created_at.desc())
    ).scalars().all()
    accounts_by_id = {
        a.id: a for a in session.execute(
            select(Account).where(
                Account.tenant_id == ui_session.tenant_id,
                Account.id.in_([e.account_id for e in engagements]),
            )
        ).scalars()
    } if engagements else {}

    context = base_context(
        session, ui_session, active_nav="rnd_opportunities", request=request,
        flash=flash, flash_type=flash_type,
    )
    context.update({"engagements": engagements, "accounts_by_id": accounts_by_id})
    return templates.TemplateResponse(request, "rnd_opportunities.html", context)


@router.post("/ui/rnd-opportunities/{engagement_id}/promote")
def rnd_opportunity_promote(
    engagement_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = session.get(Engagement, engagement_id)
    if engagement is None or engagement.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")

    try:
        promote_rnd_engagement(session, ui_session.tenant_id, engagement)
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash("/ui/rnd-opportunities", str(exc), "error")

    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{engagement.id}", f"「{engagement.name}」を商談化しました",
    )
