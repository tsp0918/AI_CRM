"""見積もり・契約(商談管理オブジェクト内のサブオブジェクト)。

Engagement に対する「今の商品構成のスナップショット」を Quote/Contract
として発行する。作成後の明細は凍結し(EngagementLineItem のように
編集可能な作業台ではない)、後から「なぜその金額だったか」を辿れる
証跡として残す。QuoteLineItem / ContractLineItem を独立テーブルに
することで、見積もり一覧・契約一覧を後からそのままレポート化できる。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import ContractStatus, QuoteStatus


class Quote(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "quote"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    quote_number: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[QuoteStatus] = mapped_column(
        String(16), default=QuoteStatus.DRAFT, index=True
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="JPY")


class QuoteLineItem(Base, UUIDPk, TenantScoped):
    __tablename__ = "quote_line_item"

    quote_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quote.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    list_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class Contract(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "contract"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    # どの見積もりから発行されたか(直接発行の場合は NULL)。
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quote.id", ondelete="SET NULL"), index=True
    )
    contract_number: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[ContractStatus] = mapped_column(
        String(16), default=ContractStatus.DRAFT, index=True
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="JPY")


class ContractLineItem(Base, UUIDPk, TenantScoped):
    __tablename__ = "contract_line_item"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    list_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
