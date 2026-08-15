"""契約の実績3層(契約額・出荷実績・請求実績)の記録・再集計(2026-08-15)。

CRM_連携引き継ぎ書.md §7.6(IF-29/30/31)を実装する。`Contract.realized_amount`
(Phase 1a以前から存在, `revenue_report.py`が`total_amount`より優先して使う
既存フィールド)を「請求実績累計 − 返品累計」として更新することで、
実績反映の第一段はレポート側の変更なしにそのまま機能する。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Contract, ContractFulfillment


def record_fulfillment_event(
    session: Session, tenant_id: uuid.UUID, contract: Contract, *,
    kind: str, erp_document_number: str, product_code: str | None,
    quantity: Decimal | None, amount: Decimal, currency: str,
    posted_at: datetime, actor: str = "system:erp-fulfillment",
) -> ContractFulfillment | None:
    """`(tenant_id, kind, erp_document_number)`が既存なら None を返す(冪等性、
    IF-29/30/31はat-least-once配信が前提)。新規なら記録して3層を再集計する。
    """
    existing = session.execute(
        select(ContractFulfillment).where(
            ContractFulfillment.tenant_id == tenant_id,
            ContractFulfillment.kind == kind,
            ContractFulfillment.erp_document_number == erp_document_number,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    event = ContractFulfillment(
        tenant_id=tenant_id, contract_id=contract.id, kind=kind,
        erp_document_number=erp_document_number, product_code=product_code,
        quantity=quantity, amount=amount, currency=currency, posted_at=posted_at,
        written_by=actor,
    )
    session.add(event)
    session.flush()
    recalculate_actuals(session, tenant_id, contract)
    return event


def recalculate_actuals(session: Session, tenant_id: uuid.UUID, contract: Contract) -> None:
    events = session.execute(
        select(ContractFulfillment).where(
            ContractFulfillment.tenant_id == tenant_id,
            ContractFulfillment.contract_id == contract.id,
        )
    ).scalars().all()

    billed = sum((e.amount for e in events if e.kind == "billing"), Decimal("0"))
    returned = sum((e.amount for e in events if e.kind == "return"), Decimal("0"))
    # §7.6: 請求実績の累計から返品(マイナス計上)を差し引いたものを
    # 実現収益とする。revenue_report.py はこれを total_amount より優先する。
    contract.realized_amount = billed - returned
    session.flush()


def compute_fulfillment_summary(
    session: Session, tenant_id: uuid.UUID, contract: Contract,
) -> dict:
    """契約詳細UI向けの3層サマリー(§7.6の受注残・未請求残・消化率)。"""
    events = session.execute(
        select(ContractFulfillment).where(
            ContractFulfillment.tenant_id == tenant_id,
            ContractFulfillment.contract_id == contract.id,
        )
    ).scalars().all()

    shipped = sum((e.amount for e in events if e.kind == "shipment"), Decimal("0"))
    billed = sum((e.amount for e in events if e.kind == "billing"), Decimal("0"))
    returned = sum((e.amount for e in events if e.kind == "return"), Decimal("0"))
    shipped_net = shipped - returned
    billed_net = billed - returned

    backlog = contract.total_amount - shipped_net
    unbilled = shipped_net - billed_net
    fulfillment_rate = (
        float(shipped_net / contract.total_amount) if contract.total_amount else None
    )
    return {
        "shipped": shipped_net, "billed": billed_net, "returned": returned,
        "backlog": backlog, "unbilled": unbilled, "fulfillment_rate": fulfillment_rate,
        "events": sorted(events, key=lambda e: e.posted_at, reverse=True),
    }
