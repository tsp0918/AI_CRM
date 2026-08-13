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
from sqlalchemy.dialects.postgresql import JSONB
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
    # 前ステップ(1件目は登録時点)からの遅延日数。分岐先として選ばれた
    # 場合も、その先の待機時間としてこの値をそのまま使う。
    delay_days: Mapped[int] = mapped_column(Integer, default=0)
    subject_template: Mapped[str | None] = mapped_column(String(255))
    body_template: Mapped[str] = mapped_column(Text)

    # 前ステップの反応に応じた分岐(ロードマップ§7の「適応的な次の一手」を
    # 簡易な形で実現)。reaction_channels に該当する Touch がこのステップの
    # ドラフト生成後に観測されたら on_reaction_next_order へ、
    # 観測されなければ on_no_reaction_next_order へ進む。両方 NULL なら
    # 常定どおり step_order+1 に進む。値は次のステップの step_order、
    # -1 は「ここでシーケンスを終了する」を意味する。
    reaction_channels: Mapped[list] = mapped_column(JSONB, default=list)
    on_reaction_next_order: Mapped[int | None] = mapped_column(Integer)
    on_no_reaction_next_order: Mapped[int | None] = mapped_column(Integer)

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
    # 直近に生成したステップの step_order。まだ1件も生成していなければ NULL。
    current_step_order: Mapped[int | None] = mapped_column(Integer)
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
