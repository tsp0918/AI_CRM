"""連携ステータス画面(2026-08-15, CRM_連携引き継ぎ書.md §6.2)。

Outbox(送信保証キュー)とWebhookEvent(受信履歴)の状態を目視確認する
運用画面。Phase 0時点ではdispatcherが未登録のため、実際の外部送信は
まだ行われない — 「テストメッセージを登録する」フォームから投入した
メッセージがpending→(未登録kindのため)failedになる一生を確認できる
ようにし、Phase 1以降で実dispatcherが載った後の運用動線を先取りする。
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...enums import OutboxStatus
from ...models import OutboxMessage, WebhookEvent
from ...services.outbox import enqueue_outbox, requeue_outbox_message
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/integration-status", response_class=HTMLResponse)
def integration_status_view(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    tenant_id = ui_session.tenant_id

    outbox_messages = session.execute(
        select(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id)
        .order_by(OutboxMessage.created_at.desc()).limit(50)
    ).scalars().all()

    webhook_events = session.execute(
        select(WebhookEvent).where(WebhookEvent.tenant_id == tenant_id)
        .order_by(WebhookEvent.received_at.desc()).limit(50)
    ).scalars().all()

    counts = {status: 0 for status in OutboxStatus}
    for message in outbox_messages:
        if message.status in counts:
            counts[message.status] += 1

    context = base_context(
        session, ui_session, active_nav="integration_status", request=request,
        flash=flash, flash_type=flash_type,
    )
    context.update({
        "outbox_messages": outbox_messages,
        "webhook_events": webhook_events,
        "outbox_counts": counts,
    })
    return templates.TemplateResponse(request, "integration_status.html", context)


@router.post("/ui/integration-status/test-message")
def integration_status_enqueue_test(
    target_system: str = Form(...),
    kind: str = Form(...),
    payload_json: str = Form("{}"),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    try:
        payload = json.loads(payload_json) if payload_json.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("JSONオブジェクトを入力してください")
    except (json.JSONDecodeError, ValueError) as exc:
        return redirect_with_flash(
            "/ui/integration-status", f"payloadの解析に失敗しました: {exc}", "error",
        )

    enqueue_outbox(
        session, ui_session.tenant_id, target_system=target_system.strip(),
        kind=kind.strip(), payload=payload,
        actor=f"human:{ui_session.actor_id}",
    )
    session.commit()
    return redirect_with_flash("/ui/integration-status", "テストメッセージをOutboxに登録しました")


@router.post("/ui/integration-status/{message_id}/requeue")
def integration_status_requeue(
    message_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    message = session.execute(
        select(OutboxMessage).where(
            OutboxMessage.tenant_id == ui_session.tenant_id,
            OutboxMessage.id == message_id,
        )
    ).scalar_one_or_none()
    if message is None:
        return redirect_with_flash("/ui/integration-status", "メッセージが見つかりません", "error")

    requeue_outbox_message(message)
    session.commit()
    return redirect_with_flash("/ui/integration-status", "再送待ちに戻しました")
