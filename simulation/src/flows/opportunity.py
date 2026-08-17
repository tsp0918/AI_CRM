"""商談〜見積(docs/BULK_SIMULATION_SPEC.md §5.2)。

2026-08-16のP3疎通確認で実証済みの経路をそのまま関数化したもの:
engagement作成 → 明細追加 → 見積DRAFT作成(provisional review/commerce-check/
quota-checkが自動enqueueされる)→ Outbox送信 → AI_TM側の判定を実行させて
webhookで橋渡し → 見積SENTへの遷移を試みる。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from crm_mvp.models import Engagement, Quote, ReviewCase
from sqlalchemy import select

from ..clients.aitm import AitmClient
from ..clients.crm import CrmWebhookClient
from ..clients.crm_ui import CrmUiClient, UiFlowError
from ..db import tenant_session
from ..outbox_runner import drain_outbox
from .review import drive_and_push_review_result


@dataclass
class OpportunityResult:
    engagement_id: uuid.UUID
    quote_id: uuid.UUID | None = None
    quote_number: str | None = None
    review_case_no: str | None = None
    aitm_case_no: str | None = None
    outcome: str = "unknown"  # issuable | blocked_party | blocked_review | error
    reason: str | None = None
    outbox_stats: dict = field(default_factory=dict)


def create_engagement_existing_account(
    tenant_id: uuid.UUID, *, account_id: uuid.UUID, name: str, stage: str = "qualified",
) -> uuid.UUID:
    """Web UIの`engagement_new_submit`は常に新規Accountを作るため使えない
    (既存顧客への商談を表現できない) — 既存Accountに紐付けるにはDB直接作成
    が必要(P2のマスタ投入と同じ、書き込みが公開APIのスコープ外という
    既存の設計上の制約による)。"""
    with tenant_session(tenant_id) as session:
        engagement = Engagement(tenant_id=tenant_id, account_id=account_id, name=name, stage=stage)
        session.add(engagement)
        session.commit()
        return engagement.id


def run_opportunity_to_quote(
    tenant_id: uuid.UUID, ui: CrmUiClient, aitm: AitmClient, crm_wh: CrmWebhookClient, *,
    engagement_id: uuid.UUID, line_items: list[tuple[uuid.UUID, float]],
    destination_country: str = "", end_use: str = "", end_user_account_id: str = "",
) -> OpportunityResult:
    result = OpportunityResult(engagement_id=engagement_id)

    for product_id, qty in line_items:
        ui.add_line_item(engagement_id, product_id=product_id, quantity=qty)

    try:
        ui.create_quote(
            engagement_id, destination_country=destination_country, end_use=end_use,
            end_user_account_id=end_user_account_id,
        )
    except UiFlowError as e:
        result.outcome, result.reason = "blocked_party", str(e)
        return result

    with tenant_session(tenant_id) as session:
        quote = session.execute(
            select(Quote).where(Quote.tenant_id == tenant_id, Quote.engagement_id == engagement_id)
        ).scalars().first()
        result.quote_id, result.quote_number = quote.id, quote.quote_number

    with tenant_session(tenant_id) as session:
        result.outbox_stats = drain_outbox(session, tenant_id)

    with tenant_session(tenant_id) as session:
        review_case = session.execute(
            select(ReviewCase).where(
                ReviewCase.tenant_id == tenant_id, ReviewCase.engagement_id == engagement_id,
                ReviewCase.review_type == "provisional",
            )
        ).scalars().first()
        if review_case is not None:
            result.review_case_no, result.aitm_case_no = review_case.case_no, review_case.provider_request_id

    if result.aitm_case_no:
        drive_and_push_review_result(aitm, crm_wh, result.review_case_no, result.aitm_case_no)

    try:
        ui.update_quote_status(engagement_id, result.quote_id, status="sent")
        result.outcome = "issuable"
    except UiFlowError as e:
        result.outcome, result.reason = "blocked_review", str(e)

    return result
