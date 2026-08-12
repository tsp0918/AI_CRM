"""取引先コンプライアンスチェック起票 API(HANDOVER.md §5 Phase5, item 19,20)。

現時点では ScreeningPort を同期呼び出しする(outbox/非同期キューは未導入)。
実運用でモジュールを AITMScreeningAdapter に差し替えれば、そのまま
非同期化(submit → webhook callback)の骨格として使える形にしてある。

Account 自体の CRUD は §3.10(既存 CRM が System of Record)によりこの
MVP のスコープ外 — Account は他経路(ミラー同期等)で存在する前提。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComplianceCheckType, ComplianceOutcome
from ..models import Account, ComplianceStatus
from ..ports.screening import ScreeningPort
from ..services.idempotency import compute_idempotency_key
from .deps import get_screening_port, get_session, get_tenant_id

router = APIRouter(prefix="/accounts", tags=["accounts"])

FRESHNESS_WINDOW_DAYS = 180


class ComplianceCheckRequest(BaseModel):
    check_type: ComplianceCheckType


class ComplianceCheckOut(BaseModel):
    id: uuid.UUID
    check_type: ComplianceCheckType
    outcome: ComplianceOutcome
    provider: str | None
    provider_request_id: str | None
    idempotency_key: str
    checked_at: datetime | None
    valid_until: datetime | None


@router.post(
    "/{account_id}/compliance-checks", response_model=ComplianceCheckOut,
    status_code=201,
)
def submit_compliance_check(
    account_id: uuid.UUID, body: ComplianceCheckRequest,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
    screening: ScreeningPort = Depends(get_screening_port),
) -> dict:
    account = session.get(Account, account_id)
    if account is None or account.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="account not found")

    idempotency_key = compute_idempotency_key(
        account.name, body.check_type.value, policy_version="v1",
    )
    result = screening.screen(account.name, body.check_type)

    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == tenant_id,
            ComplianceStatus.account_id == account.id,
            ComplianceStatus.check_type == body.check_type,
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if status is None:
        status = ComplianceStatus(
            tenant_id=tenant_id, account_id=account.id, check_type=body.check_type,
        )
        session.add(status)

    status.outcome = result.outcome
    status.provider = result.provider
    status.provider_request_id = result.provider_request_id
    status.checked_at = now
    status.valid_until = now + timedelta(days=FRESHNESS_WINDOW_DAYS)
    # §3.7: 証跡は参照のみ。result.detail(外部APIの生レスポンス)は
    # 一時的な受け渡し用であり、そのまま永続化はしない。
    session.commit()
    session.refresh(status)

    return {
        "id": status.id, "check_type": status.check_type, "outcome": status.outcome,
        "provider": status.provider, "provider_request_id": status.provider_request_id,
        "idempotency_key": idempotency_key,
        "checked_at": status.checked_at, "valid_until": status.valid_until,
    }
