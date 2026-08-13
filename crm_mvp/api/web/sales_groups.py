"""セールスグループの作成・一覧UI(crm_mvp/api/web/products.py の商品
グループ管理パネルと同じ考え方の単独ページ版)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...services.sales_groups import create_sales_group, list_sales_groups_tree_ordered
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/sales-groups", response_class=HTMLResponse)
def sales_groups_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    groups = list_sales_groups_tree_ordered(session, ui_session.tenant_id)

    context = base_context(
        session, ui_session, active_nav="sales_groups", flash=flash, flash_type=flash_type,
    )
    context.update({"groups": groups})
    return templates.TemplateResponse(request, "sales_groups.html", context)


@router.post("/ui/sales-groups/new")
def sales_group_new_submit(
    name: str = Form(...),
    parent_group_id: str = Form(""),
    description: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    try:
        group = create_sales_group(
            session, ui_session.tenant_id, name=name,
            parent_group_id=uuid.UUID(parent_group_id) if parent_group_id.strip() else None,
            description=description,
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash("/ui/sales-groups", str(exc), "error")

    session.commit()
    return redirect_with_flash("/ui/sales-groups", f"セールスグループ「{group.name}」を作成しました")
