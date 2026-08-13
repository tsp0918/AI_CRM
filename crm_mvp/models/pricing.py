"""Product Priceリスト(価格表)と案件の商品構成。

将来ERPと連携する前提で、CRM側は「今その案件でどの商品をいくらで
売ろうとしているか」の作業台として持つ。ERP側のマスタと同期する
までは、Product はこのテーブルにダミーで持たせて良い。

EngagementLineItem は list_price_snapshot(選択時点の定価)を必ず
保持する — Product.list_price が後から変わっても、既存の商談明細は
当時の定価を基準にした値引率のまま残る(evidence保存の思想と同じ)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk


class Product(Base, UUIDPk, Timestamped, TenantScoped):
    """価格表の1行。"""

    __tablename__ = "product"

    name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str | None] = mapped_column(String(64))
    list_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class EngagementLineItem(Base, UUIDPk, Timestamped, TenantScoped):
    """案件が今組んでいる商品構成の1行。合計が Engagement.amount を駆動する。"""

    __tablename__ = "engagement_line_item"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL"), index=True
    )
    # 商品が後で改名・削除されても明細上の表示が壊れないようにする。
    product_name_snapshot: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    list_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
