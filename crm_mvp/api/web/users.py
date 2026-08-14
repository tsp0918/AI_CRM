"""社内ユーザー名簿の閲覧UI(user_matrix_CRM.csv、2026-08-13)。

ログイン認証は持たない読み取り専用の名簿 — Function/Role/Authority/
所属セールスグループを一覧できる。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import SalesGroup
from ...services.users import list_users
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

AUTHORITY_LABELS = {
    "none": "—", "approver": "Approver", "approver_high": "Approver-High",
    "approver_super": "Approver-Super",
}


@router.get("/ui/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    users = list_users(session, ui_session.tenant_id)
    sales_groups = {
        g.id: g for g in session.execute(
            select(SalesGroup).where(SalesGroup.tenant_id == ui_session.tenant_id)
        ).scalars()
    }

    context = base_context(session, ui_session, active_nav="users", request=request)
    context.update({
        "users": users, "sales_groups": sales_groups, "authority_labels": AUTHORITY_LABELS,
    })
    return templates.TemplateResponse(request, "users.html", context)
