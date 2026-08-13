"""Product Priceリストと案件の商品構成(ロードマップ: 見積・契約基盤)。

Engagement.amount は、商品構成(EngagementLineItem)が1件でもあれば
その合計から自動計算する — 手入力とproduct由来の金額が食い違ったまま
放置される事故を防ぐため。商品構成が無い案件では amount は手動編集の
ままにしておける(初期の粗い金額感の入力を妨げない)。

discount_rate(%)による承認フロー(値引き率のしきい値に応じた承認要求)
は将来の拡張として設計上の余地だけ残す — ここでは実装しない
(2026-08-13 ユーザー決定: 「先の開発で良い」)。
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Engagement, EngagementLineItem, Product

TWO_PLACES = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_unit_price(list_price: Decimal, discount_rate: Decimal) -> Decimal:
    return _round(list_price * (Decimal("100") - discount_rate) / Decimal("100"))


def list_line_items(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID,
) -> list[EngagementLineItem]:
    return session.execute(
        select(EngagementLineItem).where(
            EngagementLineItem.tenant_id == tenant_id,
            EngagementLineItem.engagement_id == engagement_id,
        ).order_by(EngagementLineItem.created_at)
    ).scalars().all()


def recalculate_engagement_amount(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
) -> None:
    """商品構成があればその合計を金額とする。全て削除されたら手動入力に
    戻せるよう金額をクリアする(前の自動計算値を残留させない)。"""
    items = list_line_items(session, tenant_id, engagement.id)
    engagement.amount = (
        sum((item.line_total for item in items), Decimal("0")) if items else None
    )
    session.flush()


def add_line_item(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement, *,
    product: Product, quantity: int, discount_rate: Decimal,
) -> EngagementLineItem:
    if quantity < 1:
        raise ValueError("数量は1以上を指定してください")
    if not (Decimal("0") <= discount_rate <= Decimal("100")):
        raise ValueError("値引率は0〜100の範囲で指定してください")

    unit_price = compute_unit_price(product.list_price, discount_rate)
    item = EngagementLineItem(
        tenant_id=tenant_id, engagement_id=engagement.id, product_id=product.id,
        product_name_snapshot=product.name, quantity=quantity,
        list_price_snapshot=product.list_price, discount_rate=discount_rate,
        unit_price=unit_price, line_total=_round(unit_price * quantity),
    )
    session.add(item)
    session.flush()

    if engagement.currency != product.currency:
        engagement.currency = product.currency
    recalculate_engagement_amount(session, tenant_id, engagement)
    return item


def remove_line_item(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
    line_item: EngagementLineItem,
) -> None:
    session.delete(line_item)
    session.flush()
    recalculate_engagement_amount(session, tenant_id, engagement)
