"""輸出許可証の残枠照会・仮引当・解放(2026-08-15, CRM_連携引き継ぎ書.md §6.5)。

IF-06(見積作成時の残枠照会)・IF-07(契約発行時の仮引当、キャンセル時の解放)
を実装する。AI_TM側の実レスポンス契約は未確定のため、IF-32(商流ゲート)と
同じ「同期レスポンス型」を仮定して組む — Outbox dispatcherがその場で
`LicenseAllocation`を更新し、別途受信Webhookは置かない。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import LicenseAllocationStatus, OutboxResult
from ..models import Contract, Engagement, LicenseAllocation, OutboxMessage, Quote
from .action_items import create_manual_action_item
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher
from .quoting import list_contract_line_items, list_quote_line_items

LICENSE_ACTION_ASSIGNEE = "輸出管理チーム"


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
        "allocation_id_ref": str(allocation.id),
        "items": [
            {"quantity": float(li.quantity)} for li in line_items
        ],
        "destination_country": quote.destination_country,
        "contract_start_date": None, "contract_end_date": None,
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
        "allocation_id_ref": str(allocation.id),
        "items": [{"quantity": float(li.quantity)} for li in line_items],
        "destination_country": contract.destination_country,
        "context": {"case_no": f"CRM-{contract.contract_number}", "purpose": "contract"},
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
            payload={"allocation_id_ref": str(allocation.id), "allocation_id": allocation.allocation_id},
            ref_type="license_allocation", ref_id=str(allocation.id), actor=actor,
        )


def _get_allocation(session: Session, tenant_id: uuid.UUID, message: OutboxMessage) -> LicenseAllocation | None:
    ref_id = message.payload.get("allocation_id_ref") or message.ref_id
    if not ref_id:
        return None
    return session.execute(
        select(LicenseAllocation).where(
            LicenseAllocation.tenant_id == tenant_id, LicenseAllocation.id == uuid.UUID(ref_id),
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
        allocation.license_number = data.get("license_number")
        allocation.warnings = data.get("warnings") or []
        allocation.checked_at = datetime.now(timezone.utc)
        if allocation.warnings:
            create_manual_action_item(
                session, message.tenant_id, allocation.engagement_id,
                assigned_to=LICENSE_ACTION_ASSIGNEE,
                task=(
                    "許可証の残枠照会で警告があります: "
                    + "; ".join(str(w) for w in allocation.warnings)
                ),
                assigned_by="system:license-check",
            )
        session.flush()
    return result


def dispatch_license_allocate(session: Session, message: OutboxMessage) -> OutboxResult:
    allocation = _get_allocation(session, message.tenant_id, message)
    if allocation is None:
        return OutboxResult.FAILED_NO_RETRY

    try:
        response = _post_license_api(
            message.tenant_id, "/api/licenses/allocate", message.payload, str(message.id),
        )
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        data = response.json()
        allocation.status = LicenseAllocationStatus.ALLOCATED
        allocation.allocation_id = data.get("allocation_id")
        allocation.license_number = data.get("license_number") or allocation.license_number
        allocation.allocated_at = datetime.now(timezone.utc)
        session.flush()
    return result


def dispatch_license_release(session: Session, message: OutboxMessage) -> OutboxResult:
    allocation = _get_allocation(session, message.tenant_id, message)
    if allocation is None:
        return OutboxResult.FAILED_NO_RETRY

    try:
        response = _post_license_api(
            message.tenant_id, "/api/licenses/release", message.payload, str(message.id),
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
