"""クオリフィケーション・スロット。

BANT / MEDDIC をテキスト欄で持たない。各評価軸を
「構造化された主張 + 根拠 + 証拠強度」として保持する。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import CONFIDENCE_ORDER, Confidence, Criterion, VerificationMethod


class QualificationSlot(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "qualification_slot"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    criterion: Mapped[Criterion] = mapped_column(String(32))

    # 構造化値。criterion ごとにスキーマが異なる。
    #   economic_buyer -> {"node_id": "<uuid>"}
    #   metrics        -> {"kpi": "歩留まり", "delta": 3.2, "unit": "pt",
    #                      "annual_value": 42000000}
    #   paper_process  -> {"approval_layers": 3, "legal_review": true}
    value: Mapped[dict] = mapped_column(JSONB, default=dict)

    confidence: Mapped[Confidence] = mapped_column(
        String(16), default=Confidence.ASSERTED
    )
    # 根拠となった IngestionSource / 発言箇所
    evidence_source_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True)
    )
    evidence_quote: Mapped[str | None] = mapped_column(Text)

    asserted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 情報の鮮度。人事異動・予算期で腐るため、ゲート評価時に失効判定する。
    decays_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    # §7.2: VERIFIED への昇格記録。/verify エンドポイント経由でのみ人が書く。
    # evidence_uri は §3.7 に従い参照のみ（顧客文書の本文はコピーしない）。
    evidence_uri: Mapped[str | None] = mapped_column(String(512))
    verification_method: Mapped[VerificationMethod | None] = mapped_column(
        String(32)
    )
    verification_note: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    engagement: Mapped["Engagement"] = relationship(back_populates="slots")

    __table_args__ = (
        UniqueConstraint("engagement_id", "criterion", name="uq_slot_per_deal"),
    )

    def meets(self, required: Confidence, now: datetime | None = None) -> bool:
        """要求された証拠強度を満たしているか（失効を含めて判定）。"""
        now = now or datetime.now(tz=self.decays_at.tzinfo if self.decays_at else None)
        if self.decays_at is not None and self.decays_at <= now:
            return False
        if not self.value:
            return False
        return CONFIDENCE_ORDER[self.confidence] >= CONFIDENCE_ORDER[required]


from .engagement import Engagement  # noqa: E402
