"""Lead獲得・スコアリング基盤(ロードマップ§7)。

Leadは Engagement とは独立したエンティティにする。案件化しなかった
Leadも含めた Marketing/Inside Sales 活動の全体 ROI を追跡するためで、
Engagement.Stage.LEAD を拡張するだけでは案件化しなかった見込み客の
活動が可視化できない。

Touch は IngestionSource と同じ設計思想(種別 + 生データ + 発生時刻 +
取り込み元)を踏襲する。将来どの MA/アウトバウンドツールを選定しても、
新しい source_system の値を1つ増やすだけで取り込める。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import CampaignChannelType, LeadSourceChannel, LeadStatus, TouchChannel


class Campaign(Base, UUIDPk, Timestamped, TenantScoped):
    """ROI集計の単位。ツール非依存の軽量な入れ物。"""

    __tablename__ = "campaign"

    name: Mapped[str] = mapped_column(String(200))
    channel_type: Mapped[CampaignChannelType] = mapped_column(String(24))
    owner_team: Mapped[str | None] = mapped_column(String(32))  # marketing / inside_sales
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)


class Lead(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    """案件化前の見込み客。案件化(コンバージョン)で Account/Contact/
    Engagement に引き継ぐ。案件化しなかった Lead もここに残り続ける。
    """

    __tablename__ = "lead"

    company_name: Mapped[str] = mapped_column(String(200))
    matched_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL"), index=True
    )

    full_name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))

    source_channel: Mapped[LeadSourceChannel | None] = mapped_column(String(16))
    source_campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaign.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[LeadStatus] = mapped_column(
        String(16), default=LeadStatus.NEW, index=True
    )
    owner: Mapped[str | None] = mapped_column(String(120))  # IS/SDR担当者名
    disqualify_reason: Mapped[str | None] = mapped_column(Text)

    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    converted_engagement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("engagement.id", ondelete="SET NULL"), index=True
    )
    # 案件化した瞬間のスコア・接点サマリーのスナップショット。案件化後は
    # Leadの状態が動かなくなる一方、Engagement側では「なぜこのタイミングで
    # 案件化したか」を知りたい需要がある。StageTransition.gate_snapshot と
    # 同じ考え方で、その場限りの状態を JSONB にそのまま残す。
    conversion_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class Touch(Base, UUIDPk, TenantScoped):
    """Marketing/Inside Sales 活動の1接点の記録。IngestionSource と対になる
    「マーケ・IS側の唯一の入力口」。
    """

    __tablename__ = "touch"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lead.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaign.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[TouchChannel] = mapped_column(String(24))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # "manual" / "csv_import" / 将来のMAツール名。同じ列にそのまま追加できる。
    source_system: Mapped[str] = mapped_column(String(32), default="manual")
    actor: Mapped[str | None] = mapped_column(String(120))
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
