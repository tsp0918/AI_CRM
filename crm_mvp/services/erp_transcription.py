"""契約closeのERPへの受注転記(2026-08-15, CRM_連携引き継ぎ書.md §6.9 IF-25)。

契約が締結(SIGNED)されたタイミングでERPへ受注情報を送る。
`aitm_transaction_id`(AI_TM側の正式審査ケースの`provider_request_id`)を
必ず引き渡すことで、ERP側が独自に新規審査を起票して案件が二重生成される
事態を防ぐ(§6.9の最重要事項)。レスポンスの`document_number`(受注番号)は
`Contract.external_system="erp"`/`external_id`に保存する — これが
`crm_mvp/api/erp_webhooks.py`のIF-29/30/31が契約を逆引きする際のキーになる。
"""

from __future__ import annotations

import os
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import OutboxResult, ReviewType
from ..models import Account, Contract, Engagement, OutboxMessage, Product, ReviewCase
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher
from .party_compliance import build_party_ref
from .quoting import list_contract_line_items


def _resolve_erp_material_code(session: Session, tenant_id: uuid.UUID, product_id: uuid.UUID | None) -> str | None:
    if product_id is None:
        return None
    product = session.get(Product, product_id)
    if product is None or product.erp_material_id is None:
        return None
    return product.erp_material.material_code if product.erp_material else None


def submit_contract_transcription(
    session: Session, tenant_id: uuid.UUID, contract: Contract, engagement: Engagement, *,
    actor: str,
) -> None:
    review_case = session.execute(
        select(ReviewCase).where(
            ReviewCase.tenant_id == tenant_id, ReviewCase.contract_id == contract.id,
            ReviewCase.review_type == ReviewType.FORMAL,
        ).order_by(ReviewCase.created_at.desc())
    ).scalars().first()

    counterparty = session.get(Account, engagement.account_id)
    end_user = (
        session.get(Account, contract.end_user_account_id)
        if contract.end_user_account_id else None
    )
    line_items = list_contract_line_items(session, tenant_id, contract.id)

    payload = {
        "crm_contract_id": str(contract.id),
        "crm_engagement_id": str(engagement.id),
        "aitm_transaction_id": review_case.provider_request_id if review_case else None,
        "skip_export_check": True,
        "customer_code": (
            counterparty.external_id
            if counterparty and counterparty.external_system == "erp" else None
        ),
        "counterparty": build_party_ref(counterparty) if counterparty else {},
        "end_user": build_party_ref(end_user) if end_user else None,
        "customer_po_number": contract.contract_number,
        "contract_start_date": contract.start_date.isoformat() if contract.start_date else None,
        "contract_end_date": contract.end_date.isoformat() if contract.end_date else None,
        "currency": contract.currency,
        "items": [
            {
                "material_code": _resolve_erp_material_code(session, tenant_id, li.product_id),
                "quantity": li.quantity, "unit_price": str(li.unit_price),
            }
            for li in line_items
        ],
    }
    enqueue_outbox(
        session, tenant_id, target_system="erp", kind="erp.sales_order.submit",
        payload=payload, ref_type="contract", ref_id=str(contract.id), actor=actor,
    )


def dispatch_erp_sales_order_submit(session: Session, message: OutboxMessage) -> OutboxResult:
    client = SignedClient(
        os.environ.get("ERP_BASE_URL"), message.tenant_id,
        bearer_env="ERP_SALES_ORDER_BEARER", secret_env="ERP_SALES_ORDER_SECRET",
    )
    try:
        response = client.post("/sd/sales-orders", message.payload, request_id=str(message.id))
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)
    finally:
        client.close()

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        data = response.json()
        contract = session.get(Contract, uuid.UUID(message.ref_id))
        if contract is not None and data.get("document_number"):
            contract.external_system = "erp"
            contract.external_id = data["document_number"]
            session.flush()
    return result


def register_erp_transcription_dispatchers() -> None:
    if os.environ.get("ERP_BASE_URL"):
        register_dispatcher("erp.sales_order.submit", dispatch_erp_sales_order_submit)
