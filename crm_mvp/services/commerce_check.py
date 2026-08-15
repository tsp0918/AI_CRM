"""ERPへの与信・反社チェック依頼(2026-08-15, CRM_連携引き継ぎ書.md §6.8)。

見積をDRAFTで作成したタイミングでERPの商流ゲート(IF-32)へ送信する。
ERP側は当面ダミー実装(常に`ok`を返すスタブ)だが、CRM側は本番同等の
インターフェース(Outbox経由の非同期送信+署名付きHTTP)で呼び出しておき、
ERP側の中身が入れ替わってもCRMの改修が不要な状態にする(§6.8の指示通り)。

IF-32は同期レスポンス型のAPI(送信→Webhookコールバックではなく、その場で
結果が返る)であるため、Outbox dispatcherがレスポンスをその場で
ComplianceStatus/Accountに反映する — 別途受信Webhookは不要。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComplianceCheckType, ComplianceOutcome, OutboxResult
from ..models import Account, ComplianceStatus, Engagement, OutboxMessage, Quote
from .action_items import create_manual_action_item
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher
from .party_compliance import build_party_ref

COMMERCE_FRESHNESS_WINDOW_DAYS = 180
COMMERCE_ACTION_ASSIGNEE = "与信管理チーム"

_OVERALL_RESULT_MAP = {
    "ok": ComplianceOutcome.CLEAR,
    "warning": ComplianceOutcome.NEEDS_REVIEW,
    "ng": ComplianceOutcome.BLOCKED,
    "pending": ComplianceOutcome.UNKNOWN,
}
_CHECK_KIND_MAP = {
    "credit": ComplianceCheckType.CREDIT,
    "antisocial": ComplianceCheckType.ANTI_SOCIAL,
}


def submit_commerce_check(
    session: Session, tenant_id: uuid.UUID, quote: Quote, engagement: Engagement, *,
    actor: str,
) -> None:
    counterparty = session.get(Account, engagement.account_id)
    end_user = (
        session.get(Account, quote.end_user_account_id)
        if quote.end_user_account_id else None
    )
    payload = {
        "request_type": "quote_draft",
        "crm_quote_id": str(quote.id),
        "crm_engagement_id": str(engagement.id),
        "counterparty": build_party_ref(counterparty) if counterparty else {},
        "end_user": build_party_ref(end_user) if end_user else None,
        "check_types": ["credit", "antisocial"],
        "amount": {"currency": quote.currency, "total_amount": str(quote.total_amount)},
    }
    enqueue_outbox(
        session, tenant_id, target_system="erp", kind="erp.commerce_check.submit",
        payload=payload, ref_type="quote", ref_id=str(quote.id), actor=actor,
    )


def _apply_commerce_result(session: Session, tenant_id: uuid.UUID, payload: dict, data: dict) -> None:
    engagement_id = payload.get("crm_engagement_id")
    engagement = session.get(Engagement, uuid.UUID(engagement_id)) if engagement_id else None
    account = session.get(Account, engagement.account_id) if engagement else None
    if account is None:
        return

    now = datetime.now(timezone.utc)
    results = data.get("results") or {}
    overall = _OVERALL_RESULT_MAP.get(data.get("overall_result", ""), ComplianceOutcome.UNKNOWN)

    for check_key, check_type in _CHECK_KIND_MAP.items():
        detail = results.get(check_key) or {}
        outcome = _OVERALL_RESULT_MAP.get(detail.get("result", ""), overall)
        status = session.execute(
            select(ComplianceStatus).where(
                ComplianceStatus.tenant_id == tenant_id,
                ComplianceStatus.account_id == account.id,
                ComplianceStatus.check_type == check_type,
            )
        ).scalar_one_or_none()
        if status is None:
            status = ComplianceStatus(
                tenant_id=tenant_id, account_id=account.id, check_type=check_type,
            )
            session.add(status)
        status.outcome = outcome
        status.provider = "erp"
        status.provider_request_id = data.get("check_id")
        status.detail = detail
        status.checked_at = now
        status.valid_until = now + timedelta(days=COMMERCE_FRESHNESS_WINDOW_DAYS)

    # §6.8: ERPが正とする取引先属性を反映する(参照専用)。
    attrs = data.get("counterparty_attributes") or {}
    if attrs.get("erp_bp_code"):
        account.external_system = "erp"
        account.external_id = attrs["erp_bp_code"]
    credit = results.get("credit") or {}
    if "credit_limit" in credit:
        account.credit_limit = _to_decimal(credit.get("credit_limit"))
        account.credit_available = _to_decimal(credit.get("credit_available"))
    if attrs.get("payment_terms_master"):
        account.payment_terms_master = attrs["payment_terms_master"]
    if attrs.get("customer_group"):
        account.customer_group = attrs["customer_group"]
    if attrs.get("sales_district"):
        account.sales_district = attrs["sales_district"]

    if overall in (ComplianceOutcome.NEEDS_REVIEW, ComplianceOutcome.BLOCKED) and engagement is not None:
        create_manual_action_item(
            session, tenant_id, engagement.id, assigned_to=COMMERCE_ACTION_ASSIGNEE,
            task=(
                f"商流ゲート(与信・反社)の結果: {data.get('overall_result')}。"
                f"取引先「{account.name}」の見積を確認してください。"
            ),
            assigned_by="system:erp-commerce-check",
        )
    session.flush()


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def dispatch_erp_commerce_check(session: Session, message: OutboxMessage) -> OutboxResult:
    client = SignedClient(
        os.environ.get("ERP_BASE_URL"), message.tenant_id,
        bearer_env="ERP_COMMERCE_BEARER", secret_env="ERP_COMMERCE_SECRET",
    )
    try:
        response = client.post(
            "/gts/screening/commerce-check", message.payload, request_id=str(message.id),
        )
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)
    finally:
        client.close()

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        _apply_commerce_result(session, message.tenant_id, message.payload, response.json())
    return result


def register_erp_dispatchers() -> None:
    """`ERP_BASE_URL`が設定されている場合のみdispatcherを登録する
    (Phase 0のAI_TM側と同じ「未設定なら黙って偽OKにしない」原則)。"""
    if os.environ.get("ERP_BASE_URL"):
        register_dispatcher("erp.commerce_check.submit", dispatch_erp_commerce_check)
