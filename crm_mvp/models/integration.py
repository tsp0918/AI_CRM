"""外部システム(ERP/AI_TM)連携の共通基盤(2026-08-15, CRM_連携引き継ぎ書.md §9.3)。

OutboxMessage: 送信保証キュー。見積・契約作成等のトランザクションを
外部システムの可用性に依存させないため、業務処理自体はOutboxへの
書き込みだけで完結させ、実際のHTTP送信は別プロセス(process_outbox)
が非同期に行う。

WebhookEvent: 受信Webhookの冪等性記録。at-least-once配信を前提に、
同一event_idの再受信を「正常系の重複」として扱うための記録簿。

いずれも実際の送信先(AI_TM/ERP)固有のロジックは持たない — kind文字列
と汎用JSONBペイロードだけの薄い箱にして、Phase 1以降で実際の送受信
ハンドラを積み上げていく前提の土台。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import OutboxStatus, WebhookEventResult


class OutboxMessage(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "outbox_message"

    target_system: Mapped[str] = mapped_column(String(16))  # "aitm" | "erp"
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default=OutboxStatus.PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base, UUIDPk, Provenance, TenantScoped):
    __tablename__ = "webhook_event"

    # 送信元(AI_TM/ERP)が発行する冪等性キー。テナント内で一意。
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    source_system: Mapped[str] = mapped_column(String(16))  # "aitm" | "erp"
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(
        String(16), default=WebhookEventResult.PROCESSED
    )
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "event_id", name="uq_webhook_event_tenant_event_id"
        ),
    )
