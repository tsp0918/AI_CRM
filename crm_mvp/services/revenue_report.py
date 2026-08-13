"""売上レポート集計(2026-08-13 ユーザー要望)。

closed_won の Engagement を対象に、実売上額の基準は:
  - Contract があれば Contract.realized_amount(未設定なら total_amount で代用)
  - Contract が無ければ Engagement.amount(POベースの受注)

realized_amount は今はどの契約でも未設定(常に None)だが、将来
ERPと契約IDで同期して実際の出荷・請求額が積算されるようになったとき、
このレポートはコードの変更なしでそのまま正しい実売上を返すように
設計してある(契約金額 → 実売上額への切替が自然に起きる)。

複数通貨が混在するケースの換算は今回のスコープ外 — 現状のデモデータは
全てJPYのため単純合算している。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Stage
from ..models import (
    Account, Contract, ContractLineItem, Engagement, EngagementLineItem,
    Product, ProductGroup, SalesGroup,
)
from .account_hierarchy import list_accounts


def _engagement_revenue_from_contracts(
    session: Session, tenant_id: uuid.UUID, engagement_ids: list[uuid.UUID],
) -> tuple[dict[uuid.UUID, Decimal], set[uuid.UUID]]:
    contracts = session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.engagement_id.in_(engagement_ids),
        )
    ).scalars().all()
    by_engagement: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    has_contract: set[uuid.UUID] = set()
    for c in contracts:
        amount = c.realized_amount if c.realized_amount is not None else c.total_amount
        by_engagement[c.engagement_id] += amount
        has_contract.add(c.engagement_id)
    return by_engagement, has_contract


def closed_won_revenue_rows(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    """closed_won の商談ごとに、実売上額・取引先(法人グループの頂点まで
    ロールアップ)・セールスグループ・関係性(新規/更新/Upsell/Cross-sell)
    をまとめた行を返す。画面側はこれを好きな軸で集計する。"""
    engagements = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == tenant_id, Engagement.stage == Stage.CLOSED_WON,
        )
    ).scalars().all()
    if not engagements:
        return []
    engagement_ids = [e.id for e in engagements]

    revenue_by_engagement, has_contract = _engagement_revenue_from_contracts(
        session, tenant_id, engagement_ids,
    )
    accounts = {a.id: a for a in list_accounts(session, tenant_id)}
    sales_groups = {
        g.id: g for g in session.execute(
            select(SalesGroup).where(SalesGroup.tenant_id == tenant_id)
        ).scalars()
    }

    rows = []
    for e in engagements:
        amount = revenue_by_engagement.get(e.id)
        if amount is None:
            amount = e.amount or Decimal("0")

        account = accounts.get(e.account_id)
        root_account = _walk_to_root(account, accounts)
        sales_group = sales_groups.get(e.sales_group_id) if e.sales_group_id else None

        rows.append({
            "engagement": e,
            "amount": amount,
            "has_contract": e.id in has_contract,
            "account": account,
            "root_account": root_account,
            "sales_group": sales_group,
            "relationship_type": e.relationship_type or "new_business",
        })
    return rows


def _walk_to_root(
    account: Account | None, accounts: dict[uuid.UUID, Account],
) -> Account | None:
    if account is None:
        return None
    current = account
    seen: set[uuid.UUID] = set()
    while current.parent_account_id is not None and current.parent_account_id not in seen:
        seen.add(current.id)
        parent = accounts.get(current.parent_account_id)
        if parent is None:
            break
        current = parent
    return current


def aggregate_by(rows: list[dict], key_fn: Callable[[dict], str]) -> list[dict]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        label = key_fn(row)
        totals[label] += row["amount"]
        counts[label] += 1
    return sorted(
        ({"label": k, "amount": v, "count": counts[k]} for k, v in totals.items()),
        key=lambda r: r["amount"], reverse=True,
    )


def product_group_revenue(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    """closed_won 商談の明細(Contractがあればその明細、無ければ
    EngagementLineItem)を商品グループ単位で集計する。"""
    engagement_ids = session.execute(
        select(Engagement.id).where(
            Engagement.tenant_id == tenant_id, Engagement.stage == Stage.CLOSED_WON,
        )
    ).scalars().all()
    if not engagement_ids:
        return []

    contracts = session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.engagement_id.in_(engagement_ids),
        )
    ).scalars().all()
    engagements_with_contract = {c.engagement_id for c in contracts}
    contract_ids = [c.id for c in contracts]

    line_items: list[ContractLineItem | EngagementLineItem] = []
    if contract_ids:
        line_items += session.execute(
            select(ContractLineItem).where(
                ContractLineItem.tenant_id == tenant_id,
                ContractLineItem.contract_id.in_(contract_ids),
            )
        ).scalars().all()

    remaining = [eid for eid in engagement_ids if eid not in engagements_with_contract]
    if remaining:
        line_items += session.execute(
            select(EngagementLineItem).where(
                EngagementLineItem.tenant_id == tenant_id,
                EngagementLineItem.engagement_id.in_(remaining),
            )
        ).scalars().all()

    if not line_items:
        return []

    product_ids = {li.product_id for li in line_items if li.product_id}
    products = {
        p.id: p for p in session.execute(
            select(Product).where(Product.id.in_(product_ids))
        ).scalars()
    } if product_ids else {}
    group_ids = {p.product_group_id for p in products.values() if p.product_group_id}
    groups = {
        g.id: g for g in session.execute(
            select(ProductGroup).where(ProductGroup.id.in_(group_ids))
        ).scalars()
    } if group_ids else {}

    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: dict[str, int] = defaultdict(int)
    for li in line_items:
        product = products.get(li.product_id) if li.product_id else None
        group = groups.get(product.product_group_id) if product and product.product_group_id else None
        label = group.name if group else "未分類"
        totals[label] += li.line_total
        counts[label] += 1

    return sorted(
        ({"label": k, "amount": v, "count": counts[k]} for k, v in totals.items()),
        key=lambda r: r["amount"], reverse=True,
    )


RELATIONSHIP_TYPE_REPORT_LABELS = {
    "new_business": "新規", "renewal": "更新(Renewal)",
    "upsell": "Upsell", "cross_sell": "Cross-sell",
}
