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


def aggregate_by(
    rows: list[dict], key_fn: Callable[[dict], str],
    id_fn: Callable[[dict], object | None] | None = None,
) -> list[dict]:
    """id_fn を渡すと各行に id を付ける(ドリルダウンのリンク先を組み立てる用)。"""
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: dict[str, int] = defaultdict(int)
    ids: dict[str, object | None] = {}
    for row in rows:
        label = key_fn(row)
        totals[label] += row["amount"]
        counts[label] += 1
        if id_fn is not None and label not in ids:
            ids[label] = id_fn(row)
    return sorted(
        ({"label": k, "amount": v, "count": counts[k], "id": ids.get(k)} for k, v in totals.items()),
        key=lambda r: r["amount"], reverse=True,
    )


def product_group_revenue(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    """closed_won 商談の明細(Contractがあればその明細、無ければ
    EngagementLineItem)を商品グループ単位で集計する。"""
    facts = line_item_facts(session, tenant_id)
    return aggregate_by(
        facts,
        lambda f: f["product_group"].name if f["product_group"] else "未分類",
        lambda f: f["product_group"].id if f["product_group"] else None,
    )


RELATIONSHIP_TYPE_REPORT_LABELS = {
    "new_business": "新規", "renewal": "更新(Renewal)",
    "upsell": "Upsell", "cross_sell": "Cross-sell",
}


# --- ドリルダウン基盤(2026-08-14) ------------------------------------------
#
# closed_won 商談の商品明細を1行=1ファクトとして展開する。売上を
# 「取引先」「商品グループ」「セールスグループ」「関係性」のどれで
# 絞り込んでも、同じファクト行から一貫して集計・一覧できるようにする
# ための共通基盤 — 将来の自由な動的レポート機能もこの上に構築する想定。

def line_item_facts(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    engagement_ids = session.execute(
        select(Engagement.id).where(
            Engagement.tenant_id == tenant_id, Engagement.stage == Stage.CLOSED_WON,
        )
    ).scalars().all()
    if not engagement_ids:
        return []

    engagements = {
        e.id: e for e in session.execute(
            select(Engagement).where(Engagement.id.in_(engagement_ids))
        ).scalars()
    }

    contracts = session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.engagement_id.in_(engagement_ids),
        )
    ).scalars().all()
    contract_engagement_by_contract_id = {c.id: c.engagement_id for c in contracts}
    engagements_with_contract = {c.engagement_id for c in contracts}
    contract_ids = list(contract_engagement_by_contract_id)

    line_items_with_engagement: list[tuple[uuid.UUID, ContractLineItem | EngagementLineItem]] = []
    if contract_ids:
        for li in session.execute(
            select(ContractLineItem).where(
                ContractLineItem.tenant_id == tenant_id,
                ContractLineItem.contract_id.in_(contract_ids),
            )
        ).scalars():
            line_items_with_engagement.append(
                (contract_engagement_by_contract_id[li.contract_id], li)
            )

    remaining = [eid for eid in engagement_ids if eid not in engagements_with_contract]
    if remaining:
        for li in session.execute(
            select(EngagementLineItem).where(
                EngagementLineItem.tenant_id == tenant_id,
                EngagementLineItem.engagement_id.in_(remaining),
            )
        ).scalars():
            line_items_with_engagement.append((li.engagement_id, li))

    if not line_items_with_engagement:
        return []

    accounts = {a.id: a for a in list_accounts(session, tenant_id)}
    product_ids = {li.product_id for _, li in line_items_with_engagement if li.product_id}
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
    sales_groups = {
        g.id: g for g in session.execute(
            select(SalesGroup).where(SalesGroup.tenant_id == tenant_id)
        ).scalars()
    }

    facts = []
    for eng_id, li in line_items_with_engagement:
        engagement = engagements[eng_id]
        product = products.get(li.product_id) if li.product_id else None
        group = groups.get(product.product_group_id) if product and product.product_group_id else None
        account = accounts.get(engagement.account_id)
        root_account = _walk_to_root(account, accounts)
        sales_group = sales_groups.get(engagement.sales_group_id) if engagement.sales_group_id else None

        facts.append({
            "engagement": engagement, "line_item": li, "amount": li.line_total,
            "product": product, "product_group": group,
            "account": account, "root_account": root_account,
            "sales_group": sales_group,
            "relationship_type": engagement.relationship_type or "new_business",
        })
    return facts


def filter_facts(
    facts: list[dict], *, product_group_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None, account_id: uuid.UUID | None = None,
    sales_group_id: uuid.UUID | None = None, relationship_type: str | None = None,
) -> list[dict]:
    """account_id は「そのAccount自身」だけでなく法人グループの子孫すべてを
    含めたい場面が多いため、呼び出し側で get_family_account_ids を使って
    複数IDを渡したい場合はこの関数を複数回呼ぶのではなく、呼び出し側で
    account_id 判定を account.id in {ファミリーのID集合} に置き換えること。
    ここではシンプルに「そのAccount行に完全一致」で絞り込む。"""
    def matches(f: dict) -> bool:
        if product_group_id and (not f["product_group"] or f["product_group"].id != product_group_id):
            return False
        if product_id and (not f["product"] or f["product"].id != product_id):
            return False
        if account_id and (not f["account"] or f["account"].id != account_id):
            return False
        if sales_group_id and (not f["sales_group"] or f["sales_group"].id != sales_group_id):
            return False
        if relationship_type and f["relationship_type"] != relationship_type:
            return False
        return True

    return [f for f in facts if matches(f)]


def facts_by_engagement(facts: list[dict]) -> list[dict]:
    """ファクト行(明細単位)を商談単位に集約する(ドリルダウンで商談
    一覧を出すため)。"""
    by_engagement: dict[uuid.UUID, dict] = {}
    for f in facts:
        engagement = f["engagement"]
        row = by_engagement.setdefault(
            engagement.id,
            {"engagement": engagement, "account": f["account"], "amount": Decimal("0")},
        )
        row["amount"] += f["amount"]
    return sorted(by_engagement.values(), key=lambda r: r["amount"], reverse=True)
