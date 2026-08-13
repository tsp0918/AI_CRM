"""テナント切り替え(認証基盤が無い MVP における擬似ログイン)。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .session import (
    clear_ui_session_cookies, read_ui_session, set_ui_session_cookies,
)
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/workspace", response_class=HTMLResponse)
def workspace_form(
    request: Request, next: str = "/ui/", error: str = "",
) -> HTMLResponse:
    current = read_ui_session(request)
    return templates.TemplateResponse(request, "workspace.html", {
        "current": current, "next": next, "error": error,
    })


@router.post("/ui/workspace")
def workspace_submit(
    request: Request,
    tenant_id: str = Form(...),
    actor_name: str = Form(""),
    next: str = Form("/ui/"),
) -> RedirectResponse:
    try:
        tenant_uuid = uuid.UUID(tenant_id.strip())
    except ValueError:
        return RedirectResponse(
            url=f"/ui/workspace?next={next}&error=tenant_id は UUID 形式で入力してください",
            status_code=303,
        )

    existing = read_ui_session(request)
    actor_id = existing.actor_id if existing else uuid.uuid4()
    resolved_name = actor_name.strip() or (existing.actor_name if existing else None)

    response = RedirectResponse(url=next or "/ui/", status_code=303)
    set_ui_session_cookies(response, tenant_uuid, actor_id, resolved_name)
    return response


@router.post("/ui/workspace/logout")
def workspace_logout() -> RedirectResponse:
    response = RedirectResponse(url="/ui/workspace", status_code=303)
    clear_ui_session_cookies(response)
    return response
