"""ERPからのWebhook受信(2026-08-15, CRM_連携引き継ぎ書.md §7.3・§7.6)。

AI_TM向け(`crm_mvp/api/webhooks.py`)とは送信元(署名鍵・Bearer)が異なる
ため別ファイルに分ける。Phase 0で単体実装済みの`verify_webhook`/
`record_webhook_event`をここでも再利用する。

パスは引き継ぎ書§7.3の一覧に忠実に合わせている(AI_TM向けエンドポイントは
実API契約確定前に独自の命名で先行実装した箇所があるが、ERP側は今回が
初実装のため最初から仕様通りのパスにする)。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .deps import get_session
from .webhook_security import record_webhook_event, verify_webhook

router = APIRouter(prefix="/webhooks/erp", tags=["webhooks"])

SOURCE = "erp"
SECRET_ENV = "ERP_WEBHOOK_SECRET"
BEARER_ENV = "ERP_WEBHOOK_BEARER"


async def _authenticate(request: Request):
    return await verify_webhook(
        request, source=SOURCE, secret_env=SECRET_ENV, bearer_env=BEARER_ENV,
    )


def _set_tenant(session: Session, tenant_id: uuid.UUID) -> None:
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


@router.post("/material-updated")
async def receive_material_updated(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-26: ERP品目マスタの同期。既存の`upsert_erp_material`(CSV取込
    スクリプトが使っているものと同じ関数)を1件ずつ呼ぶだけの薄いラッパー。
    """
    from ..services.erp_materials import upsert_erp_material

    ctx = await _authenticate(request)
    _set_tenant(session, ctx.tenant_id)
    event, is_new = record_webhook_event(
        session, ctx.tenant_id,
        event_id=str(ctx.payload.get("event_id") or ctx.payload.get("material_code", "")),
        source_system=SOURCE, event_type="material.updated", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    upsert_erp_material(
        session, ctx.tenant_id,
        material_code=ctx.payload["material_code"], description=ctx.payload.get("description", ""),
        material_type=ctx.payload.get("material_type", "FERT"),
        base_unit=ctx.payload.get("base_unit", "PC"),
        standard_price=ctx.payload.get("standard_price", 0),
        currency=ctx.payload.get("currency", "JPY"),
        hs_code=ctx.payload.get("hs_code"), eccn=ctx.payload.get("eccn"),
        fefta_judgment=ctx.payload.get("fefta_judgment", "UNKNOWN"),
        country_of_origin=ctx.payload.get("country_of_origin"),
        is_active=ctx.payload.get("is_active", True),
    )
    session.commit()
    return {"status": "processed"}


@router.post("/business-partner-updated")
async def receive_business_partner_updated(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-27: ERP取引先マスタの同期。既存の`upsert_erp_business_partner`を
    1件ずつ呼ぶだけの薄いラッパー。"""
    from ..services.erp_business_partners import upsert_erp_business_partner

    ctx = await _authenticate(request)
    _set_tenant(session, ctx.tenant_id)
    event, is_new = record_webhook_event(
        session, ctx.tenant_id,
        event_id=str(ctx.payload.get("event_id") or ctx.payload.get("bp_code", "")),
        source_system=SOURCE, event_type="business_partner.updated", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    upsert_erp_business_partner(
        session, ctx.tenant_id,
        bp_code=ctx.payload["bp_code"], name=ctx.payload.get("name", ""),
        bp_type=ctx.payload.get("bp_type", "ORG"), country=ctx.payload.get("country"),
        roles=ctx.payload.get("roles", "CUSTOMER"), email=ctx.payload.get("email"),
        phone=ctx.payload.get("phone"), address_line1=ctx.payload.get("address_line1"),
        address_line2=ctx.payload.get("address_line2"), city=ctx.payload.get("city"),
        postal_code=ctx.payload.get("postal_code"),
        credit_limit=ctx.payload.get("credit_limit"), payment_terms=ctx.payload.get("payment_terms"),
        currency=ctx.payload.get("currency"), is_denied_party=ctx.payload.get("is_denied_party", False),
        is_active=ctx.payload.get("is_active", True),
    )
    session.commit()
    return {"status": "processed"}


def _find_contract_by_erp_so(session: Session, tenant_id: uuid.UUID, erp_sales_order_number: str):
    from ..models import Contract

    return session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.external_system == "erp",
            Contract.external_id == erp_sales_order_number,
        )
    ).scalar_one_or_none()


async def _receive_fulfillment_event(request: Request, session: Session, *, kind: str, event_type: str) -> dict:
    from ..services.fulfillment import record_fulfillment_event

    ctx = await _authenticate(request)
    _set_tenant(session, ctx.tenant_id)

    doc_number = str(ctx.payload.get(f"erp_{kind}_number") or ctx.payload.get("erp_document_number", ""))
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=doc_number or str(ctx.payload.get("event_id", "")),
        source_system=SOURCE, event_type=event_type, payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    contract = _find_contract_by_erp_so(
        session, ctx.tenant_id, str(ctx.payload.get("erp_sales_order_number", "")),
    )
    if contract is None:
        event.error = "該当するContract(erp_sales_order_number)が見つかりません"
        session.commit()
        return {"status": "processed"}

    for item in ctx.payload.get("items", []):
        record_fulfillment_event(
            session, ctx.tenant_id, contract, kind=kind,
            erp_document_number=f"{doc_number}:{item.get('material_code', '')}",
            product_code=item.get("material_code"),
            # JSONペイロードはfloat/intで届く。ORM属性にDecimal型以外の
            # 数値が混ざると、後続のsum(..., Decimal("0"))がflush直後
            # (未refresh)のオブジェクトを拾ってTypeErrorになる
            # (2026-08-16 P3疎通確認で判明、billing 2件目以降で発現)。
            quantity=Decimal(str(item["quantity"])) if item.get("quantity") is not None else None,
            amount=Decimal(str(item.get("amount", 0))),
            currency=ctx.payload.get("currency", contract.currency),
            posted_at=datetime.fromisoformat(ctx.payload["posted_at"]),
        )

    session.commit()
    return {"status": "processed"}


@router.post("/delivery-posted")
async def receive_delivery_posted(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-29: 出荷実績の計上。"""
    return await _receive_fulfillment_event(
        request, session, kind="shipment", event_type="delivery.posted",
    )


@router.post("/billing-posted")
async def receive_billing_posted(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-30: 請求実績の計上。"""
    return await _receive_fulfillment_event(
        request, session, kind="billing", event_type="billing.posted",
    )


@router.post("/return-posted")
async def receive_return_posted(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-31: 返品実績(マイナス計上)。"""
    return await _receive_fulfillment_event(
        request, session, kind="return", event_type="return.posted",
    )
