"""Product Priceリスト(価格表)管理のUI。

将来ERPと同期する前提で、CRM側は作成・一覧のみのシンプルな管理画面。
ここに投入したダミーデータがそのまま案件の商品構成の選択肢になる。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Product
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

    context = base_context(
        session, ui_session, active_nav="products", flash=flash, flash_type=flash_type,
    )
    context.update({"products": products})
    return templates.TemplateResponse(request, "products.html", context)


@router.post("/ui/products/new")
def product_new_submit(
    name: str = Form(...),
    sku: str = Form(""),
    list_price: str = Form(...),
    currency: str = Form("JPY"),
    description: str = Form(""),
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

    product = Product(
        tenant_id=ui_session.tenant_id, name=name.strip(), sku=sku.strip() or None,
        list_price=price, currency=currency.strip() or "JPY",
        description=description.strip() or None,
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
