"""契約更新(Renewal)管理画面。

契約終了日が近い ACTIVE な契約のうち、まだ更新商談が作られていないものを
一覧する — 「契約中商談のrenewal商談もわかるようにしたい」という要望への
対応(2026-08-13)。ここから直接、更新商談を1クリックで起こせる。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...enums import EngagementRelationshipType
from ...models import Contract, Engagement
from ...services.engagement_relationships import (
    create_child_engagement, list_renewal_candidates, resolve_renewal_context,
)
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/renewals", response_class=HTMLResponse)
def renewals_list(
    request: Request,
    within_days: int = 90,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    candidates = list_renewal_candidates(
        session, ui_session.tenant_id, within_days=within_days,
    )
    renewal_context = resolve_renewal_context(session, ui_session.tenant_id, candidates)

    context = base_context(
        session, ui_session, active_nav="renewals", flash=flash, flash_type=flash_type,
    )
    context.update({
        "candidates": candidates, "renewal_context": renewal_context,
        "within_days": within_days,
    })
    return templates.TemplateResponse(request, "renewals.html", context)


@router.post("/ui/renewals/{contract_id}/start")
def start_renewal_ui(
    contract_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    contract = session.get(Contract, contract_id)
    if contract is None or contract.tenant_id != ui_session.tenant_id:
        return redirect_with_flash("/ui/renewals", "契約が見つかりません", "error")

    parent = session.get(Engagement, contract.engagement_id)
    if parent is None:
        return redirect_with_flash("/ui/renewals", "元の商談が見つかりません", "error")

    child = create_child_engagement(
        session, ui_session.tenant_id, parent,
        relationship_type=EngagementRelationshipType.RENEWAL,
        name=f"{parent.name}(更新)",
    )
    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{child.id}", f"更新商談「{child.name}」を作成しました",
    )
