"""アウトバウンド・シーケンス自動化基盤(下書き止まり)(ロードマップ§7)。

実際の送信基盤(SMTP/ESP連携)は未接続。ここではステップの内容を生成して
SequenceDraft として残すところまでを実装する。既存のAutonomyModeと同じ
思想: 実績が蓄積するまでは常に人の確認を経由させる(§3.2の裏返し —
外部への送信は社内データの書き込みよりさらに慎重さが必要な領域)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import (
    SequenceDraftStatus, SequenceEnrollmentStatus, SequenceStepChannel,
)


class Sequence(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    """施策テンプレート。ステップの並びは SequenceStep が持つ。"""

    __tablename__ = "sequence"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SequenceStep(Base, UUIDPk, TenantScoped):
    """1ステップの定義。channel + 遅延日数 + テンプレート文面。"""

    __tablename__ = "sequence_step"

    sequence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequence.id", ondelete="CASCADE"), index=True
    )
    step_order: Mapped[int] = mapped_column(Integer)
    channel: Mapped[SequenceStepChannel] = mapped_column(String(16))
    # 前ステップ(1件目は登録時点)からの遅延日数。
    delay_days: Mapped[int] = mapped_column(Integer, default=0)
    subject_template: Mapped[str | None] = mapped_column(String(255))
    body_template: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("sequence_id", "step_order", name="uq_sequence_step_order"),
    )


class SequenceEnrollment(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    """Leadごとの実行状態。"""

    __tablename__ = "sequence_enrollment"

    sequence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequence.id", ondelete="CASCADE"), index=True
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lead.id", ondelete="CASCADE"), index=True
    )
    # 次に生成すべき SequenceStep.step_order の位置(0-indexed)。
    current_step_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[SequenceEnrollmentStatus] = mapped_column(
        String(16), default=SequenceEnrollmentStatus.ACTIVE, index=True
    )
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SequenceDraft(Base, UUIDPk, TenantScoped):
    """1ステップ分の生成済みドラフト。ステータスが reviewed/dismissed に
    変わっても実際に送信されたことを意味しない — 送信基盤が無いため。
    """

    __tablename__ = "sequence_draft"

    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequence_enrollment.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequence_step.id", ondelete="CASCADE")
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel: Mapped[SequenceStepChannel] = mapped_column(String(16))
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    # 証拠駆動パーソナライズで何を参照したかの説明(透明性のため)。
    personalization_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[SequenceDraftStatus] = mapped_column(
        String(16), default=SequenceDraftStatus.DRAFT, index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
