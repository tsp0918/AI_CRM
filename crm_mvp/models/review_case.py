"""AI_TMへの取引審査ケース(2026-08-15, CRM_連携引き継ぎ書.md §2.3・§5)。

見積作成時の仮審査(provisional)・契約発行時の正式審査(formal)を
1レコードずつ記録する。`ComplianceStatus`(Account単位・チェック種別単位で
1件のみ)とは別テーブル — こちらはQuote/Contractという「取引の1版」単位で
複数件が積み上がる点が本質的に異なる。

送信自体はOutbox(crm_mvp/services/outbox.py)経由で非同期に行われるため、
このモデル自体は送信状態を持たない(Outbox側のOutboxMessageが持つ)。
ここが持つのはAI_TM側の審査ケースの状態(PENDING→judged)のみ。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import ReviewCaseStatus


class ReviewCase(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "review_case"

    # 仮審査 CRM-Q{quote_id} / 正式審査 CRM-C{contract_id}(§5.3)。
    case_no: Mapped[str] = mapped_column(String(32), index=True)
    parent_case_no: Mapped[str | None] = mapped_column(String(32))
    review_type: Mapped[str] = mapped_column(String(16))
    artifact_type: Mapped[str] = mapped_column(String(16))

    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), index=True
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )

    review_key_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=ReviewCaseStatus.PENDING, index=True
    )
    # IF-10ペイロードの revision。これより新しい通知のみ適用する(古い通知は破棄)。
    revision: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "case_no", name="uq_review_case_tenant_case_no"),
    )
