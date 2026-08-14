"""ダッシュボード(案件一覧)。骨格 CRM 機能の入口。

2026-08-14 UI/UX見直し: 全テナントの全商談をフラットに出すだけだと、
Userとowner_user_idの構造が入った今でも「自分の担当」を見られなかった。
擬似セッション(Cookie)にUserを紐付けず、クエリパラメータでの絞り込み
(?owner_user_id=...)で対応する(レポートビルダー等と同じSSRパターン)。
既定でクローズ済み(受注/失注)は非表示にし、日次で触る「進行中の案件」
だけが見える状態を既定にする。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...enums import Stage
from ...models import Account, Engagement
from ...services.confidence_score import compute_confidence_score
from ...services.stage_transitions import load_gate_context
from ...services.users import list_users
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    owner_user_id: str = "",
    show_closed: bool = False,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    query = select(Engagement).where(Engagement.tenant_id == ui_session.tenant_id)
    if owner_user_id:
        query = query.where(Engagement.owner_user_id == uuid.UUID(owner_user_id))
    if not show_closed:
        query = query.where(
            Engagement.stage.notin_([Stage.CLOSED_WON, Stage.CLOSED_LOST])
        )
    engagements = session.execute(
        query.order_by(Engagement.updated_at.desc())
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
        session, ui_session, active_nav="dashboard", request=request, flash=flash, flash_type=flash_type,
    )
    context.update({
        "rows": rows,
        "users": list_users(session, ui_session.tenant_id),
        "owner_user_id": owner_user_id,
        "show_closed": show_closed,
    })
    return templates.TemplateResponse(request, "dashboard.html", context)
