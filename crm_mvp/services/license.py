"""輸出許可証の残枠照会・仮引当・解放(2026-08-15, CRM_連携引き継ぎ書.md §6.5)。

IF-06(見積作成時の残枠照会)・IF-07(契約発行時の仮引当、キャンセル時の解放)
を実装する。IF-32(商流ゲート)と同じ「同期レスポンス型」を仮定して組む —
Outbox dispatcherがその場で`LicenseAllocation`を更新し、別途受信Webhookは
置かない。ペイロード・パスは2026-08-16のE2E疎通確認(実`export_license`
モジュール)で判明した実際のスキーマに合わせている。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import LicenseAllocationStatus, OutboxResult
from ..models import (
    Account, Contract, ContractLineItem, Engagement, ErpMaterial,
    LicenseAllocation, OutboxMessage, Product, Quote, QuoteLineItem,
)
from .action_items import create_manual_action_item
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher
from .quoting import list_contract_line_items, list_quote_line_items

LICENSE_ACTION_ASSIGNEE = "輸出管理チーム"


def _resolve_quota_items(
    session: Session, tenant_id: uuid.UUID,
    line_items: Sequence[QuoteLineItem | ContractLineItem],
) -> list[dict]:
    """`product_code`(ERP品目コード)を解決できた明細だけを送る。未解決の
    品目は静かにスキップする(残枠照会自体は品目マッピング未整備でも
    できる範囲で実施する、というIF-06の前提に合わせる)。"""
    product_ids = {li.product_id for li in line_items if li.product_id}
    products = {
        p.id: p for p in session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.id.in_(product_ids))
        ).scalars()
    } if product_ids else {}
    material_ids = {p.erp_material_id for p in products.values() if p.erp_material_id}
    materials = {
        m.id: m for m in session.execute(
            select(ErpMaterial).where(ErpMaterial.tenant_id == tenant_id, ErpMaterial.id.in_(material_ids))
        ).scalars()
    } if material_ids else {}

    items = []
    for li in line_items:
        product = products.get(li.product_id) if li.product_id else None
        material = materials.get(product.erp_material_id) if product and product.erp_material_id else None
        if material is None:
            continue
        items.append({"product_code": material.material_code, "quantity": float(li.quantity)})
    return items


def _end_user_party_id(session: Session, end_user_account_id: uuid.UUID | None) -> str | None:
    if end_user_account_id is None:
        return None
    account = session.get(Account, end_user_account_id)
    return account.aitm_party_id if account else None


def submit_quota_check(
    session: Session, tenant_id: uuid.UUID, quote: Quote, engagement: Engagement, *,
    actor: str,
) -> LicenseAllocation:
    """IF-06: 見積作成時に残枠を照会する。品目マッピング未設定でも起票は
    妨げない(残枠照会自体が「品目コードが分かる範囲で」実施される想定)。
    """
    allocation = LicenseAllocation(
        tenant_id=tenant_id, quote_id=quote.id, engagement_id=engagement.id,
        status=LicenseAllocationStatus.PENDING, written_by=actor,
    )
    session.add(allocation)
    session.flush()

    line_items = list_quote_line_items(session, tenant_id, quote.id)
    payload = {
        "items": _resolve_quota_items(session, tenant_id, line_items),
        "destination_country": quote.destination_country,
        "end_user_party_id": _end_user_party_id(session, quote.end_user_account_id),
        "context": {"case_no": f"CRM-{quote.quote_number}", "purpose": "quote"},
    }
    enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.license.quota_check",
        payload=payload, ref_type="license_allocation", ref_id=str(allocation.id), actor=actor,
    )
    return allocation


def submit_license_allocation(
    session: Session, tenant_id: uuid.UUID, contract: Contract, engagement: Engagement, *,
    actor: str,
) -> LicenseAllocation:
    """IF-07: 契約発行(締結)時に枠を仮引当する。"""
    allocation = LicenseAllocation(
        tenant_id=tenant_id, contract_id=contract.id, engagement_id=engagement.id,
        status=LicenseAllocationStatus.PENDING, written_by=actor,
    )
    session.add(allocation)
    session.flush()

    line_items = list_contract_line_items(session, tenant_id, contract.id)
    payload = {
        "case_no": f"CRM-{contract.contract_number}",
        "items": _resolve_quota_items(session, tenant_id, line_items),
        "destination_country": contract.destination_country,
        "end_user_party_id": _end_user_party_id(session, contract.end_user_account_id),
    }
    enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.license.allocate",
        payload=payload, ref_type="license_allocation", ref_id=str(allocation.id), actor=actor,
    )
    return allocation


def submit_license_release(
    session: Session, tenant_id: uuid.UUID, contract: Contract, *, actor: str,
) -> None:
    """IF-08連動: 契約キャンセル時に仮引当を解放する(§6.6)。"""
    allocations = session.execute(
        select(LicenseAllocation).where(
            LicenseAllocation.tenant_id == tenant_id,
            LicenseAllocation.contract_id == contract.id,
            LicenseAllocation.status == LicenseAllocationStatus.ALLOCATED,
        )
    ).scalars().all()
    for allocation in allocations:
        enqueue_outbox(
            session, tenant_id, target_system="aitm", kind="aitm.license.release",
            payload={}, ref_type="license_allocation", ref_id=str(allocation.id), actor=actor,
        )


def _get_allocation(session: Session, tenant_id: uuid.UUID, message: OutboxMessage) -> LicenseAllocation | None:
    if not message.ref_id:
        return None
    return session.execute(
        select(LicenseAllocation).where(
            LicenseAllocation.tenant_id == tenant_id, LicenseAllocation.id == uuid.UUID(message.ref_id),
        )
    ).scalar_one_or_none()


def _post_license_api(tenant_id: uuid.UUID, path: str, payload: dict, request_id: str) -> httpx.Response:
    client = SignedClient(
        os.environ.get("AITM_LICENSE_URL"), tenant_id,
        bearer_env="AITM_LICENSE_BEARER", secret_env="AITM_LICENSE_SECRET",
    )
    try:
        return client.post(path, payload, request_id=request_id)
    finally:
        client.close()


def dispatch_license_quota_check(session: Session, message: OutboxMessage) -> OutboxResult:
    """2026-08-16 E2E疎通確認で判明した実レスポンス形状:
    `{"overall": ..., "items": [{"product_code", "license_required", "sufficient", "warnings"}], "warnings": [...]}`。
    """
    allocation = _get_allocation(session, message.tenant_id, message)
    if allocation is None:
        return OutboxResult.FAILED_NO_RETRY

    try:
        response = _post_license_api(
            message.tenant_id, "/api/licenses/quota-check", message.payload, str(message.id),
        )
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        data = response.json()
        allocation.status = LicenseAllocationStatus.CHECKED
        allocation.warnings = data.get("warnings") or []
        allocation.checked_at = datetime.now(timezone.utc)
        if allocation.warnings or data.get("overall") not in (None, "not_required", "sufficient"):
            create_manual_action_item(
                session, message.tenant_id, allocation.engagement_id,
                assigned_to=LICENSE_ACTION_ASSIGNEE,
                task=(
                    f"許可証の残枠照会結果: {data.get('overall')}。"
                    + ("; ".join(str(w) for w in allocation.warnings) if allocation.warnings else "")
                ),
                assigned_by="system:license-check",
            )
        session.flush()
    return result


def dispatch_license_allocate(session: Session, message: OutboxMessage) -> OutboxResult:
    """実パスは`/api/licenses/allocations`(POST)。レスポンスは
    `{"allocation_id", "status", "allocations": [...], "valid_until"}`。
    """
    allocation = _get_allocation(session, message.tenant_id, message)
    if allocation is None:
        return OutboxResult.FAILED_NO_RETRY

    try:
        response = _post_license_api(
            message.tenant_id, "/api/licenses/allocations", message.payload, str(message.id),
        )
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        data = response.json()
        allocation.status = LicenseAllocationStatus.ALLOCATED
        sub_allocations = data.get("allocations") or []
        allocation.allocation_id = data.get("allocation_id") or (
            sub_allocations[0].get("allocation_no") if sub_allocations else None
        )
        allocation.allocated_at = datetime.now(timezone.utc)
        session.flush()
    return result


def dispatch_license_release(session: Session, message: OutboxMessage) -> OutboxResult:
    """実パスは`DELETE /api/licenses/allocations/{allocation_no}`
    (`SignedClient`はPOST専用のため、ここだけ生の署名付きDELETEを組む)。"""
    allocation = _get_allocation(session, message.tenant_id, message)
    if allocation is None:
        return OutboxResult.FAILED_NO_RETRY
    if not allocation.allocation_id:
        # 引当自体が成立していなかった(allocation_id無し) — 解放するものが無い。
        allocation.status = LicenseAllocationStatus.RELEASED
        allocation.released_at = datetime.now(timezone.utc)
        session.flush()
        return OutboxResult.SENT

    base_url = os.environ.get("AITM_LICENSE_URL")
    if not base_url:
        return OutboxResult.FAILED_NO_RETRY
    try:
        response = httpx.delete(
            f"{base_url.rstrip('/')}/api/licenses/allocations/{allocation.allocation_id}",
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)

    result = classify_http_response(response)
    if result == OutboxResult.SENT:
        allocation.status = LicenseAllocationStatus.RELEASED
        allocation.released_at = datetime.now(timezone.utc)
        session.flush()
    return result


def register_aitm_license_dispatchers() -> None:
    if os.environ.get("AITM_LICENSE_URL"):
        register_dispatcher("aitm.license.quota_check", dispatch_license_quota_check)
        register_dispatcher("aitm.license.allocate", dispatch_license_allocate)
        register_dispatcher("aitm.license.release", dispatch_license_release)
