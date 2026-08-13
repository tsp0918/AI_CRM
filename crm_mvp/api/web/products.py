"""Product Priceリスト(価格表)管理のUI。

ERP品目マスタ(erp_material)に「販売価格・商品グループ」を付加して
CRM商品(Product)を作る、というのが基本フロー。erp_material_id は
nullable なので、ERPに存在しない完全なCRM独自ダミー商品も引き続き
作成できる(2026-08-13 ユーザー決定)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import ErpMaterial, Product, ProductGroup
from ...services.pricing import compute_gross_margin_rate
from ...services.product_groups import create_product_group, list_product_groups_tree_ordered
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    products = session.execute(
        select(Product).where(Product.tenant_id == ui_session.tenant_id)
        .order_by(Product.name)
    ).scalars().all()
    materials = session.execute(
        select(ErpMaterial).where(
            ErpMaterial.tenant_id == ui_session.tenant_id, ErpMaterial.is_active.is_(True),
        ).order_by(ErpMaterial.material_code)
    ).scalars().all()
    product_groups = list_product_groups_tree_ordered(session, ui_session.tenant_id)
    margins = {p.id: compute_gross_margin_rate(p) for p in products}

    context = base_context(
        session, ui_session, active_nav="products", flash=flash, flash_type=flash_type,
    )
    context.update({
        "products": products, "materials": materials, "product_groups": product_groups,
        "margins": margins,
    })
    return templates.TemplateResponse(request, "products.html", context)


@router.post("/ui/products/new")
def product_new_submit(
    name: str = Form(...),
    sku: str = Form(""),
    list_price: str = Form(...),
    currency: str = Form("JPY"),
    description: str = Form(""),
    erp_material_id: str = Form(""),
    product_group_id: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    if not name.strip():
        return redirect_with_flash("/ui/products", "商品名を入力してください", "error")
    try:
        price = Decimal(list_price)
    except Exception:
        return redirect_with_flash("/ui/products", "定価は数値で入力してください", "error")
    if price < 0:
        return redirect_with_flash("/ui/products", "定価は0以上を指定してください", "error")

    material = None
    if erp_material_id.strip():
        material = session.get(ErpMaterial, uuid.UUID(erp_material_id))
        if material is None or material.tenant_id != ui_session.tenant_id:
            return redirect_with_flash("/ui/products", "ERP品目が見つかりません", "error")

    group = None
    if product_group_id.strip():
        group = session.get(ProductGroup, uuid.UUID(product_group_id))
        if group is None or group.tenant_id != ui_session.tenant_id:
            return redirect_with_flash("/ui/products", "商品グループが見つかりません", "error")

    product = Product(
        tenant_id=ui_session.tenant_id, name=name.strip(), sku=sku.strip() or None,
        list_price=price, currency=currency.strip() or "JPY",
        description=description.strip() or None,
        erp_material_id=material.id if material else None,
        product_group_id=group.id if group else None,
    )
    session.add(product)
    session.commit()

    return redirect_with_flash("/ui/products", f"商品「{product.name}」を登録しました")


@router.post("/ui/products/{product_id}/deactivate")
def product_deactivate(
    product_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    product = session.get(Product, product_id)
    if product is not None and product.tenant_id == ui_session.tenant_id:
        product.is_active = False
        session.commit()
    return redirect_with_flash("/ui/products", "商品を非公開にしました")


@router.post("/ui/product-groups/new")
def product_group_new_submit(
    name: str = Form(...),
    parent_group_id: str = Form(""),
    description: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    try:
        group = create_product_group(
            session, ui_session.tenant_id, name=name,
            parent_group_id=uuid.UUID(parent_group_id) if parent_group_id.strip() else None,
            description=description,
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash("/ui/products", str(exc), "error")

    session.commit()
    return redirect_with_flash("/ui/products", f"商品グループ「{group.name}」を作成しました")
