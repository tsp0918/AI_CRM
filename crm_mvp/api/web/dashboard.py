"""ダッシュボード(案件一覧)。骨格 CRM 機能の入口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Account, Engagement
from ...services.confidence_score import compute_confidence_score
from ...services.stage_transitions import load_gate_context
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    engagements = session.execute(
        select(Engagement)
        .where(Engagement.tenant_id == ui_session.tenant_id)
        .order_by(Engagement.updated_at.desc())
    ).scalars().all()

    account_ids = {e.account_id for e in engagements}
    accounts = {}
    if account_ids:
        accounts = {
            a.id: a for a in session.execute(
                select(Account).where(Account.id.in_(account_ids))
            ).scalars()
        }

    rows = []
    for e in engagements:
        ctx = load_gate_context(session, ui_session.tenant_id, e)
        score = compute_confidence_score(
            ctx["slots"], ctx["nodes"], ctx["edges"], ctx["roles"],
        )
        rows.append({
            "engagement": e,
            "account_name": accounts[e.account_id].name
            if e.account_id in accounts else "—",
            "score": score,
        })

    context = base_context(
        session, ui_session, active_nav="dashboard", flash=flash, flash_type=flash_type,
    )
    context.update({"rows": rows})
    return templates.TemplateResponse(request, "dashboard.html", context)
