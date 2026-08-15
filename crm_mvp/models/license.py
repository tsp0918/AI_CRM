"""輸出許可証の残枠照会・仮引当(2026-08-15, CRM_連携引き継ぎ書.md §6.5)。

見積段階での残枠照会(IF-06)と、契約発行時の仮引当(IF-07)を同じ
テーブルで管理する。1件の`Quote`につき照会は複数回(見積が改訂されるたび)
起こりうるため、`quote_id`単位で複数行を許容する(ReviewCaseと同じ設計)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import LicenseAllocationStatus


class LicenseAllocation(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "license_allocation"

    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), index=True
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=LicenseAllocationStatus.PENDING, index=True
    )
    license_number: Mapped[str | None] = mapped_column(String(64))
    allocation_id: Mapped[str | None] = mapped_column(String(64))
    # IF-06レスポンスの警告(残枠不足・有効期限切れ間近等)をそのまま保持する。
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
