"""取引先(Account)を頂点にしたディレクトリ構造の管理・閲覧UI。

2026-08-13 ユーザー要望への対応:
  - 「取引先をヒエラルキートップに置いたディレクトリ構造」— 法人グループ
    (親会社/子会社)のロールアップと、配下の商談・リード一覧
  - 「Leadもアカウントの下の階層に入ってくるとABM戦略が立てやすい」—
    一覧・詳細どちらにも Lead を並べて表示する
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...models import Account
from ...services.account_hierarchy import (
    create_grouping_account, list_accounts_tree_ordered,
    list_engagements_for_account, list_leads_for_account, set_parent_account,
)
from .common import base_context, redirect_with_flash
from .leads import LEAD_STATUS_LABELS
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


def _get_account_or_404(session: Session, ui_session: UiSession, account_id: uuid.UUID) -> Account:
    account = session.get(Account, account_id)
    if account is None or account.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="account not found")
    return account


@router.get("/ui/accounts", response_class=HTMLResponse)
def accounts_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    accounts = list_accounts_tree_ordered(session, ui_session.tenant_id)

    context = base_context(
        session, ui_session, active_nav="accounts", flash=flash, flash_type=flash_type,
    )
    context.update({"accounts": accounts})
    return templates.TemplateResponse(request, "accounts.html", context)


@router.post("/ui/accounts/new")
def account_new_submit(
    name: str = Form(...),
    parent_account_id: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    try:
        account = create_grouping_account(
            session, ui_session.tenant_id, name=name,
            parent_account_id=uuid.UUID(parent_account_id) if parent_account_id.strip() else None,
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash("/ui/accounts", str(exc), "error")

    session.commit()
    return redirect_with_flash("/ui/accounts", f"取引先「{account.name}」を作成しました")


@router.post("/ui/accounts/{account_id}/parent")
def account_set_parent_ui(
    account_id: uuid.UUID,
    parent_account_id: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    account = _get_account_or_404(session, ui_session, account_id)
    try:
        set_parent_account(
            session, ui_session.tenant_id, account,
            uuid.UUID(parent_account_id) if parent_account_id.strip() else None,
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash("/ui/accounts", str(exc), "error")

    session.commit()
    return redirect_with_flash("/ui/accounts", f"「{account.name}」の親取引先を更新しました")


@router.get("/ui/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(
    request: Request,
    account_id: uuid.UUID,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    account = _get_account_or_404(session, ui_session, account_id)
    parent = session.get(Account, account.parent_account_id) if account.parent_account_id else None
    children = [
        a for a in list_accounts_tree_ordered(session, ui_session.tenant_id)
        if a.parent_account_id == account.id
    ]
    engagements = list_engagements_for_account(session, ui_session.tenant_id, account.id)
    leads = list_leads_for_account(session, ui_session.tenant_id, account.id)

    context = base_context(
        session, ui_session, active_nav="accounts", flash=flash, flash_type=flash_type,
    )
    context.update({
        "account": account, "parent": parent, "children": children,
        "engagements": engagements, "leads": leads,
        "lead_status_labels": LEAD_STATUS_LABELS,
    })
    return templates.TemplateResponse(request, "account_detail.html", context)
