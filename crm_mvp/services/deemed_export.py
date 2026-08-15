"""みなし輸出イベントの連携(2026-08-15, CRM_連携引き継ぎ書.md §6.7 IF-09)。

外為法上、外国籍の人物・外国法人への機密技術の提供・開示は「輸出」と
みなされる。モノが動く前の営業・技術活動そのものが規制対象になりうるため、
`IngestionSource`(活動ログ)に技術情報授受フラグが立った時点でAI_TMへ
イベントを送る。判定結果はIF-13(`crm_mvp/api/webhooks.py`の
`receive_deemed_export_risk`)で受け取る。
"""

from __future__ import annotations

import os
import uuid

import httpx
from sqlalchemy.orm import Session

from ..enums import DeemedExportEventType, OutboxResult
from ..models import Account, Engagement, IngestionSource, OutboxMessage
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher
from .party_compliance import build_party_ref


def submit_deemed_export_event(
    session: Session, tenant_id: uuid.UUID, source: IngestionSource, *, actor: str,
) -> None:
    """`involves_technical_disclosure=True`で保存された活動ログをAI_TMへ送る。
    それ以外(通常の商談メモ等)は何もしない — 呼び出し側は無条件に呼んでよい。
    """
    if not source.involves_technical_disclosure or source.engagement_id is None:
        return

    engagement = session.get(Engagement, source.engagement_id)
    if engagement is None:
        return
    account = session.get(Account, engagement.account_id)

    try:
        event_type = DeemedExportEventType(source.deemed_export_event_type or "")
    except ValueError:
        event_type = None

    payload = {
        "event_type": event_type.value if event_type else source.deemed_export_event_type,
        "crm_engagement_id": str(engagement.id),
        "crm_source_id": str(source.id),
        "counterparty": build_party_ref(account) if account else {},
        # 参加者の国籍は現状 participants(JSONB, 話者→Contact対応) 側の
        # 生データに依存する — Contact.nationality を今回追加したが、
        # participants からの自動突合はスコープ外(呼び出し側で必要なら
        # 別途Contactを引いて補完する)。
        "occurred_at": source.occurred_at.isoformat() if source.occurred_at else None,
    }
    enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.deemed_export.submit",
        payload=payload, ref_type="ingestion_source", ref_id=str(source.id), actor=actor,
    )


def dispatch_deemed_export_submit(session: Session, message: OutboxMessage) -> OutboxResult:
    client = SignedClient(
        os.environ.get("AITM_DEEMED_EXPORT_URL"), message.tenant_id,
        bearer_env="AITM_DEEMED_EXPORT_BEARER", secret_env="AITM_DEEMED_EXPORT_SECRET",
    )
    try:
        response = client.post(
            "/api/deemed-export/events", message.payload, request_id=str(message.id),
        )
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)
    finally:
        client.close()
    return classify_http_response(response)


def register_deemed_export_dispatchers() -> None:
    if os.environ.get("AITM_DEEMED_EXPORT_URL"):
        register_dispatcher("aitm.deemed_export.submit", dispatch_deemed_export_submit)
