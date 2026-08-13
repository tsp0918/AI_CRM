"""商談（Engagement）。

Lead / Prospect / 商談 / 取引 / 契約 を別テーブルに分けない。
単一エンティティが stage を遷移することで、獲得〜契約のスループットが
そのまま計測可能になる。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import Stage


class Engagement(Base, UUIDPk, Timestamped, TenantScoped):
    __tablename__ = "engagement"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    stage: Mapped[Stage] = mapped_column(String(24), default=Stage.LEAD, index=True)
    # 自由記述名。ユーザーテーブルが存在しないため、Lead.owner と同じ表現に
    # 揃える(旧 owner_id: UUID は参照先が無く、どこからも書き込まれない
    # 死んだ列だったため置き換えた)。
    owner: Mapped[str | None] = mapped_column(String(120))

    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="JPY")

    # 担当者が申告するクローズ日と、稟議ルートから逆算した推定日を必ず分けて持つ。
    expected_close_date: Mapped[date | None] = mapped_column(Date)
    derived_close_date: Mapped[date | None] = mapped_column(Date)

    # closed_lost 時の理由。失注の背景を後から追跡できるようにする
    # (stage_transition.gate_snapshot はゲート判定の記録であり、
    # 「競合に負けた/予算凍結」等のビジネス上の理由は別に残す必要がある)。
    lost_reason: Mapped[str | None] = mapped_column(Text)

    external_system: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(128))

    # Lead→案件化の出自を永続化する(Leadがアーカイブされても ROI 帰属を追える)。
    originating_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lead.id", ondelete="SET NULL"), index=True
    )

    slots: Mapped[list["QualificationSlot"]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    roles: Mapped[list["EngagementRole"]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "external_system", "external_id",
                         name="uq_engagement_external"),
    )


class StageTransition(Base, UUIDPk, Provenance, TenantScoped):
    """遷移履歴。ゲート評価の結果を必ず添えて残す（監査・コーチング用途）。"""

    __tablename__ = "stage_transition"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    from_stage: Mapped[Stage | None] = mapped_column(String(24))
    to_stage: Mapped[Stage] = mapped_column(String(24))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gate_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    waiver_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))


class PipelineSnapshot(Base, UUIDPk, TenantScoped):
    """日次スナップショット。現在値しか持たない CRM では
    「なぜ落ちたか」を後から説明できないため、初日から積む。"""

    __tablename__ = "pipeline_snapshot"

    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[Stage] = mapped_column(String(24))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    evidence_score: Mapped[float] = mapped_column(default=0.0)
    expected_close_date: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("snapshot_date", "engagement_id",
                         name="uq_snapshot_daily"),
    )


from .qualification import QualificationSlot  # noqa: E402
from .buying_center import EngagementRole  # noqa: E402
