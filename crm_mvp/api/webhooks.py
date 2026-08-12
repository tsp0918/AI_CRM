"""外部システムからの Webhook 受信(HANDOVER.md §5 Phase5, item 20,21)。

本番では CRM_WEBHOOK_BEARER 相当の署名/トークン検証が必須
(CRM_INTEGRATION_HANDOVER.md §11 参照)。この MVP では未実装 —
X-Tenant-Id ヘッダのみで宛先テナントを決めており、送信元の真正性は
検証していない。本番投入前に必ず認証を追加すること。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComplianceCheckType, ComplianceOutcome, Stage
from ..models import Account, ComplianceStatus, Engagement
from .deps import get_session, get_tenant_id

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class ComplianceJudgmentPayload(BaseModel):
    account_id: uuid.UUID
    check_type: ComplianceCheckType
    outcome: ComplianceOutcome
    provider: str
    provider_request_id: str | None = None
    evidence_uri: str | None = None
    evidence_hash: str | None = None
    valid_until: datetime | None = None


@router.post("/compliance-judgment", status_code=204)
def receive_compliance_judgment(
    body: ComplianceJudgmentPayload,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> None:
    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == tenant_id,
            ComplianceStatus.account_id == body.account_id,
            ComplianceStatus.check_type == body.check_type,
        )
    ).scalar_one_or_none()
    if status is None:
        status = ComplianceStatus(
            tenant_id=tenant_id, account_id=body.account_id,
            check_type=body.check_type,
        )
        session.add(status)

    status.outcome = body.outcome
    status.provider = body.provider
    status.provider_request_id = body.provider_request_id
    status.evidence_uri = body.evidence_uri
    status.evidence_hash = body.evidence_hash
    status.checked_at = datetime.now(timezone.utc)
    status.valid_until = body.valid_until
    session.commit()


class SanctionsHit(BaseModel):
    account_id: uuid.UUID
    matched_list: str
    matched_entity_name: str


class SanctionsListUpdatedPayload(BaseModel):
    hits: list[SanctionsHit]


class ReevaluationOut(BaseModel):
    affected_accounts: int
    affected_engagements: list[uuid.UUID]


NON_TERMINAL_STAGES = [
    Stage.LEAD, Stage.PROSPECT, Stage.QUALIFIED, Stage.PROPOSAL,
    Stage.NEGOTIATION,
]


@router.post("/sanctions-list-updated", response_model=ReevaluationOut)
def receive_sanctions_list_update(
    body: SanctionsListUpdatedPayload,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> ReevaluationOut:
    """制裁リスト更新時の遡及再評価(HANDOVER.md §5 item21)。

    ヒットしたアカウントの ComplianceStatus を HIT に更新し、進行中
    (CLOSED_* でない)の Engagement を洗い出す。実際の通知チャネル
    (メール/Slack 等)への連携は本 MVP のスコープ外 — 呼び出し側が
    affected_engagements を使って任意の通知手段に繋ぐ想定。
    """
    affected_engagement_ids: list[uuid.UUID] = []
    now = datetime.now(timezone.utc)

    for hit in body.hits:
        account = session.get(Account, hit.account_id)
        if account is None or account.tenant_id != tenant_id:
            continue

        status = session.execute(
            select(ComplianceStatus).where(
                ComplianceStatus.tenant_id == tenant_id,
                ComplianceStatus.account_id == account.id,
                ComplianceStatus.check_type == ComplianceCheckType.SANCTIONS,
            )
        ).scalar_one_or_none()
        if status is None:
            status = ComplianceStatus(
                tenant_id=tenant_id, account_id=account.id,
                check_type=ComplianceCheckType.SANCTIONS,
            )
            session.add(status)
        status.outcome = ComplianceOutcome.HIT
        status.provider = "sanctions-list-monitor"
        status.detail = {
            "matched_list": hit.matched_list,
            "matched_entity_name": hit.matched_entity_name,
        }
        status.checked_at = now
        status.valid_until = now

        engagements = session.execute(
            select(Engagement).where(
                Engagement.tenant_id == tenant_id,
                Engagement.account_id == account.id,
                Engagement.stage.in_(NON_TERMINAL_STAGES),
            )
        ).scalars().all()
        affected_engagement_ids.extend(e.id for e in engagements)

    session.commit()
    return ReevaluationOut(
        affected_accounts=len(body.hits),
        affected_engagements=affected_engagement_ids,
    )
