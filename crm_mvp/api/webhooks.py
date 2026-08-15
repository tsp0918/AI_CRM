"""外部システムからの Webhook 受信(HANDOVER.md §5 Phase5, item 20,21)。

本番では CRM_WEBHOOK_BEARER 相当の署名/トークン検証が必須
(CRM_INTEGRATION_HANDOVER.md §11 参照)。この MVP では未実装 —
X-Tenant-Id ヘッダのみで宛先テナントを決めており、送信元の真正性は
検証していない。本番投入前に必ず認証を追加すること。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..enums import (
    ComplianceCheckType, ComplianceOutcome, ReviewCaseStatus, Stage,
    WebhookEventResult,
)
from ..models import Account, ComplianceStatus, Engagement, ReviewCase
from ..services.action_items import create_manual_action_item
from .deps import get_session, get_tenant_id, get_tenant_scoped_session
from .webhook_security import record_webhook_event, verify_webhook

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
    session: Session = Depends(get_tenant_scoped_session),
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


class WebhookReceiptOut(BaseModel):
    """受理のみを返す。業務データ(対象アカウント・商談等)は含めない —
    2026-08-15: CRM_連携引き継ぎ書.md §7.2「Webhookのレスポンスに業務
    データを載せないこと」に合わせた変更。以前はaffected_engagements
    をレスポンスで返していたが、リトライ時に副作用が読めなくなる
    (呼び出し側がレスポンスを再利用できない)ため、通知はActionItem
    としてCRM側に永続化する形に変更した(下記SANCTIONS_ACTION_ASSIGNEE
    参照)。"""

    status: str = "processed"
    hits_processed: int


NON_TERMINAL_STAGES = [
    Stage.LEAD, Stage.PROSPECT, Stage.QUALIFIED, Stage.PROPOSAL,
    Stage.NEGOTIATION,
]

SANCTIONS_ACTION_ASSIGNEE = "輸出管理チーム"


@router.post("/sanctions-list-updated", response_model=WebhookReceiptOut)
def receive_sanctions_list_update(
    body: SanctionsListUpdatedPayload,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_tenant_scoped_session),
) -> WebhookReceiptOut:
    """制裁リスト更新時の遡及再評価(HANDOVER.md §5 item21)。

    ヒットしたアカウントの ComplianceStatus を HIT に更新し、進行中
    (CLOSED_* でない)の Engagement ごとに ActionItem を起票する。
    レスポンスには業務データを含めない(WebhookReceiptOut参照) — 「誰が
    見ても分かる形」の通知はActionItemとして画面上に残す。
    """
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
        for engagement in engagements:
            create_manual_action_item(
                session, tenant_id, engagement.id,
                assigned_to=SANCTIONS_ACTION_ASSIGNEE,
                task=(
                    f"制裁リスト更新: {hit.matched_entity_name}"
                    f"({hit.matched_list})への該当が検知されました。"
                    "取引先・商談の取り扱いを確認してください。"
                ),
                assigned_by="system:sanctions-webhook",
            )

    session.commit()
    return WebhookReceiptOut(hits_processed=len(body.hits))


REVIEW_ACTION_ASSIGNEE = "輸出管理チーム"
_UNRESOLVED_STATUSES = (
    ReviewCaseStatus.HIT, ReviewCaseStatus.BLOCKED, ReviewCaseStatus.NEEDS_REVIEW,
)


@router.post("/aitm/review-result")
async def receive_aitm_review_result(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """AI_TMからの取引審査ケース判定結果(2026-08-15, IF-10相当・§7.4)。

    `/webhooks/compliance-judgment`はアカウント単位のComplianceStatus更新用、
    こちらはReviewCase(見積・契約という取引の1版)単位の判定結果通知用で
    別エンドポイントにしている(CRM_連携_実装計画.md Phase 1a)。

    Phase 0で単体実装済みの`verify_webhook`/`record_webhook_event`をここで
    初めて実ルートに接続する。テナント文脈は署名検証済みの`ctx.tenant_id`を
    正とする(X-Tenant-Idヘッダの二重パースを避けるため、既存2エンドポイント
    と異なりDepends(get_tenant_id)は使わない)。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_REVIEW_WEBHOOK_SECRET",
        bearer_env="AITM_REVIEW_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    case_no = str(ctx.payload.get("case_no", ""))
    revision = int(ctx.payload.get("revision", 0))
    event_id = str(ctx.payload.get("event_id") or f"{case_no}:{revision}")

    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id, source_system="aitm",
        event_type="review.judged", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    review_case = session.execute(
        select(ReviewCase).where(
            ReviewCase.tenant_id == ctx.tenant_id, ReviewCase.case_no == case_no,
        )
    ).scalar_one_or_none()
    if review_case is None:
        event.result = WebhookEventResult.ERROR
        event.error = f"該当する ReviewCase が見つかりません: {case_no}"
        session.commit()
        return {"status": "processed"}

    if revision <= review_case.revision:
        event.result = WebhookEventResult.STALE
        session.commit()
        return {"status": "processed"}

    try:
        status = ReviewCaseStatus(ctx.payload.get("status", ""))
    except ValueError:
        status = ReviewCaseStatus.NEEDS_REVIEW

    review_case.status = status
    review_case.revision = revision
    review_case.detail = ctx.payload.get("detail") or {}
    review_case.decided_at = datetime.now(timezone.utc)
    valid_until_raw = ctx.payload.get("valid_until")
    if valid_until_raw:
        review_case.valid_until = datetime.fromisoformat(valid_until_raw)

    if status in _UNRESOLVED_STATUSES:
        create_manual_action_item(
            session, ctx.tenant_id, review_case.engagement_id,
            assigned_to=REVIEW_ACTION_ASSIGNEE,
            task=f"取引審査ケース {case_no} の判定結果: {status.value}。内容を確認してください。",
            assigned_by="system:aitm-review-webhook",
        )

    session.commit()
    return {"status": "processed"}


def _resolve_party_account(
    session: Session, tenant_id: uuid.UUID, payload: dict,
) -> Account | None:
    """`crm_account_id`(優先)または`aitm_party_id`からAccountを解決する。"""
    crm_account_id = payload.get("crm_account_id")
    if crm_account_id:
        try:
            account = session.get(Account, uuid.UUID(crm_account_id))
        except ValueError:
            account = None
        if account is not None and account.tenant_id == tenant_id:
            return account

    aitm_party_id = payload.get("aitm_party_id")
    if aitm_party_id:
        return session.execute(
            select(Account).where(
                Account.tenant_id == tenant_id, Account.aitm_party_id == aitm_party_id,
            )
        ).scalar_one_or_none()
    return None


@router.post("/aitm/party-event")
async def receive_aitm_party_event(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """AI_TMからの取引先イベント受信(2026-08-15, IF-11相当・§7.4)。

    `event_type`で2種類を多重化する(CRM_連携_実装計画.md Phase 2追補):
    - `party.linked`: CRM発生の取引先がERPへ後日登録され、AI_TM側の名寄せで
      同一partyとマージされた通知(§5.1)。`external_system`/`external_id`と
      `aitm_party_id`をCRM側に反映する。
    - `screening.alert`: 継続監視中の取引先で新たな懸念が検出された通知。
      該当Accountの`ComplianceStatus`を更新し、進行中の商談にActionItemを
      起票する(`/webhooks/sanctions-list-updated`と同じ設計原則4)。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_PARTY_WEBHOOK_SECRET",
        bearer_env="AITM_PARTY_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    event_id = str(ctx.payload.get("event_id") or "")
    event_type = str(ctx.payload.get("event_type") or "")
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id or f"{event_type}:{datetime.now(timezone.utc).isoformat()}",
        source_system="aitm", event_type=event_type or "party_event", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    account = _resolve_party_account(session, ctx.tenant_id, ctx.payload)
    if account is None:
        event.result = WebhookEventResult.ERROR
        event.error = "該当するAccountが見つかりません"
        session.commit()
        return {"status": "processed"}

    if event_type == "party.linked":
        erp_bp_code = ctx.payload.get("erp_bp_code")
        if erp_bp_code:
            account.external_system = "erp"
            account.external_id = erp_bp_code
        aitm_party_id = ctx.payload.get("aitm_party_id")
        if aitm_party_id:
            account.aitm_party_id = aitm_party_id

    elif event_type == "screening.alert":
        try:
            check_type = ComplianceCheckType(ctx.payload.get("check_type", ""))
        except ValueError:
            check_type = ComplianceCheckType.SANCTIONS
        try:
            outcome = ComplianceOutcome(ctx.payload.get("outcome", ""))
        except ValueError:
            outcome = ComplianceOutcome.NEEDS_REVIEW

        status = session.execute(
            select(ComplianceStatus).where(
                ComplianceStatus.tenant_id == ctx.tenant_id,
                ComplianceStatus.account_id == account.id,
                ComplianceStatus.check_type == check_type,
            )
        ).scalar_one_or_none()
        if status is None:
            status = ComplianceStatus(
                tenant_id=ctx.tenant_id, account_id=account.id, check_type=check_type,
            )
            session.add(status)
        now = datetime.now(timezone.utc)
        status.outcome = outcome
        status.provider = "aitm"
        status.detail = ctx.payload.get("detail") or {}
        status.checked_at = now
        status.valid_until = now

        if outcome in (ComplianceOutcome.HIT, ComplianceOutcome.NEEDS_REVIEW):
            engagements = session.execute(
                select(Engagement).where(
                    Engagement.tenant_id == ctx.tenant_id,
                    Engagement.account_id == account.id,
                    Engagement.stage.in_(NON_TERMINAL_STAGES),
                )
            ).scalars().all()
            for engagement in engagements:
                create_manual_action_item(
                    session, ctx.tenant_id, engagement.id,
                    assigned_to=SANCTIONS_ACTION_ASSIGNEE,
                    task=(
                        f"継続監視アラート: 取引先「{account.name}」で"
                        f"{check_type.value}の懸念が検出されました({outcome.value})。"
                        "取引先・商談の取り扱いを確認してください。"
                    ),
                    assigned_by="system:aitm-party-webhook",
                )

    session.commit()
    return {"status": "processed"}
