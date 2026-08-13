"""ERP品目マスタの手動取り込みUI。

erp-system の実API `GET /mdm/materials` のレスポンス形状に合わせた
フィールドをそのまま入力する手動インポート運用(2026-08-13 ユーザー決定)。
取り込んだ品目は /ui/products から「CRM商品として価格設定する」ことで
Product(CRM側の価格表)に変換される。
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...enums import FeftaJudgment, MaterialType
from ...services.erp_materials import list_erp_materials, upsert_erp_material
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/erp-materials", response_class=HTMLResponse)
def erp_materials_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    materials = list_erp_materials(session, ui_session.tenant_id)

    context = base_context(
        session, ui_session, active_nav="erp_materials", flash=flash, flash_type=flash_type,
    )
    context.update({
        "materials": materials,
        "material_type_values": list(MaterialType),
        "fefta_judgment_values": list(FeftaJudgment),
    })
    return templates.TemplateResponse(request, "erp_materials.html", context)


@router.post("/ui/erp-materials/import")
def erp_material_import_submit(
    material_code: str = Form(...),
    description: str = Form(...),
    material_type: str = Form(...),
    base_unit: str = Form("PC"),
    standard_price: str = Form(...),
    currency: str = Form("JPY"),
    hs_code: str = Form(""),
    eccn: str = Form(""),
    fefta_judgment: str = Form(FeftaJudgment.UNKNOWN.value),
    country_of_origin: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    if not material_code.strip():
        return redirect_with_flash("/ui/erp-materials", "品目コードを入力してください", "error")
    if not description.strip():
        return redirect_with_flash("/ui/erp-materials", "品目説明を入力してください", "error")
    try:
        price = Decimal(standard_price)
    except Exception:
        return redirect_with_flash(
            "/ui/erp-materials", "標準価格(原価)は数値で入力してください", "error",
        )
    if price < 0:
        return redirect_with_flash(
            "/ui/erp-materials", "標準価格(原価)は0以上を指定してください", "error",
        )

    material = upsert_erp_material(
        session, ui_session.tenant_id,
        material_code=material_code, description=description.strip(),
        material_type=material_type, base_unit=base_unit.strip() or "PC",
        standard_price=price, currency=currency.strip() or "JPY",
        hs_code=hs_code.strip() or None, eccn=eccn.strip() or None,
        fefta_judgment=fefta_judgment, country_of_origin=country_of_origin.strip() or None,
    )
    session.commit()
    return redirect_with_flash(
        "/ui/erp-materials", f"品目「{material.material_code}」を取り込みました",
    )
