"""ERP取引先マスタの手動取り込みUI(crm_mvp/api/web/erp_materials.py と同じ形)。

取り込んだ取引先は、案件作成時に Account.external_system="erp" /
external_id=bp_code として紐付けることを想定する(Account 自体への
新しいFK列は増やさない — 元々ある external_system/external_id を使う)。
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...services.erp_business_partners import (
    list_erp_business_partners, upsert_erp_business_partner,
)
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/erp-business-partners", response_class=HTMLResponse)
def erp_business_partners_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    partners = list_erp_business_partners(session, ui_session.tenant_id)

    context = base_context(
        session, ui_session, active_nav="erp_business_partners", request=request, flash=flash, flash_type=flash_type,
    )
    context.update({"partners": partners})
    return templates.TemplateResponse(request, "erp_business_partners.html", context)


@router.post("/ui/erp-business-partners/import")
def erp_business_partner_import_submit(
    bp_code: str = Form(...),
    name: str = Form(...),
    bp_type: str = Form("ORG"),
    country: str = Form(""),
    roles: str = Form("CUSTOMER"),
    email: str = Form(""),
    phone: str = Form(""),
    city: str = Form(""),
    credit_limit: str = Form(""),
    payment_terms: str = Form(""),
    currency: str = Form(""),
    is_denied_party: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    if not bp_code.strip():
        return redirect_with_flash("/ui/erp-business-partners", "取引先コードを入力してください", "error")
    if not name.strip():
        return redirect_with_flash("/ui/erp-business-partners", "取引先名称を入力してください", "error")

    limit = None
    if credit_limit.strip():
        try:
            limit = Decimal(credit_limit)
        except Exception:
            return redirect_with_flash(
                "/ui/erp-business-partners", "与信限度額は数値で入力してください", "error",
            )

    partner = upsert_erp_business_partner(
        session, ui_session.tenant_id,
        bp_code=bp_code, name=name.strip(), bp_type=bp_type.strip() or "ORG",
        country=country.strip() or None, roles=roles.strip() or "CUSTOMER",
        email=email.strip() or None, phone=phone.strip() or None,
        city=city.strip() or None, credit_limit=limit,
        payment_terms=payment_terms.strip() or None, currency=currency.strip() or None,
        is_denied_party=is_denied_party.strip().lower() in ("true", "1", "on"),
    )
    session.commit()
    return redirect_with_flash(
        "/ui/erp-business-partners", f"取引先「{partner.bp_code}」を取り込みました",
    )
