"""売上レポート集計(2026-08-13 ユーザー要望)。

closed_won の Engagement を対象に、実売上額の基準は:
  - Contract があれば Contract.realized_amount(未設定なら total_amount で代用)
  - Contract が無ければ Engagement.amount(POベースの受注)

realized_amount は今はどの契約でも未設定(常に None)だが、将来
ERPと契約IDで同期して実際の出荷・請求額が積算されるようになったとき、
このレポートはコードの変更なしでそのまま正しい実売上を返すように
設計してある(契約金額 → 実売上額への切替が自然に起きる)。

通貨換算は行わない(為替レートを持たないため)。各行は Engagement.currency
を保持し、totals_by_currency() で通貨ごとに合計する — 複数通貨が混在する
テナントでは合算せず、通貨ごとの内訳を返す(2026-08-14: 単一通貨=JPY
という誤った前提でハードコードしていたのを修正)。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Stage
from ..models import (
    Account, Contract, ContractLineItem, Engagement, EngagementLineItem,
    Product, ProductGroup, SalesGroup, StageTransition, User,
)
from .account_hierarchy import list_accounts


def _closed_at_by_engagement(
    session: Session, tenant_id: uuid.UUID, engagement_ids: list[uuid.UUID],
) -> dict[uuid.UUID, date]:
    """各エンゲージメントについて、closed_wonへの最新の遷移日を返す
    (同じエンゲージメントに複数回to_stage=CLOSED_WONの遷移があっても
    最新のものを採用する)。as_of指定時の「その時点で受注済みだったか」
    判定と、期間軸(period_date)の両方で使う共通ロジック。"""
    if not engagement_ids:
        return {}
    closed_at: dict[uuid.UUID, date] = {}
    transitions = session.execute(
        select(StageTransition)
        .where(
            StageTransition.tenant_id == tenant_id,
            StageTransition.engagement_id.in_(engagement_ids),
            StageTransition.to_stage == Stage.CLOSED_WON,
        )
        .order_by(StageTransition.occurred_at.desc())
    ).scalars()
    for t in transitions:
        closed_at.setdefault(t.engagement_id, t.occurred_at.date())
    return closed_at


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


def closed_won_revenue_rows(
    session: Session, tenant_id: uuid.UUID, as_of: date | None = None,
) -> list[dict]:
    """closed_won の商談ごとに、実売上額・取引先(法人グループの頂点まで
    ロールアップ)・セールスグループ・関係性(新規/更新/Upsell/Cross-sell)
    をまとめた行を返す。画面側はこれを好きな軸で集計する。

    as_of を指定すると、「その時点までに受注していた商談」だけに絞る
    (経営レポートのスナップショットで過去時点を再構成するために使う)。
    現在の画面(as_of=None)の挙動は変えない。"""
    engagements = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == tenant_id, Engagement.stage == Stage.CLOSED_WON,
        )
    ).scalars().all()
    if not engagements:
        return []
    engagement_ids = [e.id for e in engagements]

    if as_of is not None:
        closed_at = _closed_at_by_engagement(session, tenant_id, engagement_ids)
        engagements = [
            e for e in engagements
            if e.id in closed_at and closed_at[e.id] <= as_of
        ]
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
            "currency": e.currency,
            "has_contract": e.id in has_contract,
            "account": account,
            "root_account": root_account,
            "sales_group": sales_group,
            "relationship_type": e.relationship_type or "new_business",
        })
    return rows


def totals_by_currency(
    rows: list[dict], amount_key: str = "amount",
) -> list[tuple[str, Decimal]]:
    """通貨ごとの合計を金額降順で返す。単一通貨のテナントなら要素数1件、
    複数通貨が混在していれば通貨ごとに別々の行になる(為替換算はしない)。"""
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        totals[row.get("currency") or "JPY"] += row[amount_key]
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


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


def product_group_revenue(
    session: Session, tenant_id: uuid.UUID, as_of: date | None = None,
) -> list[dict]:
    """closed_won 商談の明細(Contractがあればその明細、無ければ
    EngagementLineItem)を商品グループ単位で集計する。"""
    facts = line_item_facts(session, tenant_id, as_of=as_of)
    return aggregate_by(
        facts,
        lambda f: f["product_group"].name if f["product_group"] else "未分類",
        lambda f: f["product_group"].id if f["product_group"] else None,
    )


RELATIONSHIP_TYPE_REPORT_LABELS = {
    "new_business": "新規", "renewal": "更新(Renewal)",
    "upsell": "Upsell", "cross_sell": "Cross-sell",
}

STAGE_REPORT_LABELS = {
    "lead": "引き合い", "prospect": "見込み", "qualified": "案件化",
    "proposal": "提案", "negotiation": "最終交渉",
    "closed_won": "受注", "closed_lost": "失注",
}


# --- ドリルダウン基盤(2026-08-14) ------------------------------------------
#
# closed_won 商談の商品明細を1行=1ファクトとして展開する。売上を
# 「取引先」「商品グループ」「セールスグループ」「関係性」のどれで
# 絞り込んでも、同じファクト行から一貫して集計・一覧できるようにする
# ための共通基盤 — 将来の自由な動的レポート機能もこの上に構築する想定。

def _line_item_facts(
    session: Session, tenant_id: uuid.UUID, stages: list[Stage] | None,
    as_of: date | None = None,
) -> list[dict]:
    """stages=None は全ステージ対象(動的レポートビルダー用)。closed_won の
    エンゲージメントは従来どおり Contract 優先(無ければ EngagementLineItem)、
    それ以外のステージは Contract が存在しないため常に EngagementLineItem。
    as_of指定時は、closed_wonエンゲージメントを「その時点までに受注して
    いたもの」だけに絞る(それ以外のステージへのas_ofの意味付けは無い
    ため、closed_won以外はas_ofを無視する)。"""
    query = select(Engagement.id).where(Engagement.tenant_id == tenant_id)
    if stages is not None:
        query = query.where(Engagement.stage.in_(stages))
    engagement_ids = session.execute(query).scalars().all()
    if not engagement_ids:
        return []

    engagements = {
        e.id: e for e in session.execute(
            select(Engagement).where(Engagement.id.in_(engagement_ids))
        ).scalars()
    }
    closed_won_ids = [
        eid for eid, e in engagements.items() if e.stage == Stage.CLOSED_WON
    ]

    if as_of is not None and closed_won_ids:
        closed_at = _closed_at_by_engagement(session, tenant_id, closed_won_ids)
        closed_won_ids = [
            eid for eid in closed_won_ids
            if eid in closed_at and closed_at[eid] <= as_of
        ]
        excluded_ids = {
            eid for eid in engagements
            if engagements[eid].stage == Stage.CLOSED_WON and eid not in closed_won_ids
        }
        if excluded_ids:
            engagement_ids = [eid for eid in engagement_ids if eid not in excluded_ids]
            engagements = {
                eid: e for eid, e in engagements.items() if eid not in excluded_ids
            }

    contracts = session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.engagement_id.in_(closed_won_ids),
        )
    ).scalars().all() if closed_won_ids else []
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
    users = {
        u.id: u for u in session.execute(
            select(User).where(User.tenant_id == tenant_id)
        ).scalars()
    }

    # 実測クローズ日: closed_won への最新の StageTransition.occurred_at。
    # 同じエンゲージメントに複数回 to_stage=CLOSED_WON の遷移があっても
    # 最新のものを採用する(occurred_at 降順で最初に見た行を残す)。
    closed_at_by_engagement: dict[uuid.UUID, date] = {}
    if closed_won_ids:
        transitions = session.execute(
            select(StageTransition)
            .where(
                StageTransition.tenant_id == tenant_id,
                StageTransition.engagement_id.in_(closed_won_ids),
                StageTransition.to_stage == Stage.CLOSED_WON,
            )
            .order_by(StageTransition.occurred_at.desc())
        ).scalars()
        for t in transitions:
            closed_at_by_engagement.setdefault(t.engagement_id, t.occurred_at.date())

    facts = []
    for eng_id, li in line_items_with_engagement:
        engagement = engagements[eng_id]
        product = products.get(li.product_id) if li.product_id else None
        group = groups.get(product.product_group_id) if product and product.product_group_id else None
        account = accounts.get(engagement.account_id)
        root_account = _walk_to_root(account, accounts)
        sales_group = sales_groups.get(engagement.sales_group_id) if engagement.sales_group_id else None
        owner_user = users.get(engagement.owner_user_id) if engagement.owner_user_id else None

        if engagement.stage == Stage.CLOSED_WON:
            period_date = closed_at_by_engagement.get(eng_id) or engagement.updated_at.date()
        else:
            period_date = engagement.expected_close_date

        facts.append({
            "engagement": engagement, "line_item": li, "amount": li.line_total,
            "currency": engagement.currency,
            "product": product, "product_group": group,
            "account": account, "root_account": root_account,
            "sales_group": sales_group,
            "relationship_type": engagement.relationship_type or "new_business",
            "stage": engagement.stage, "owner_user": owner_user,
            "period_date": period_date,
        })
    return facts


def line_item_facts(
    session: Session, tenant_id: uuid.UUID, as_of: date | None = None,
) -> list[dict]:
    return _line_item_facts(session, tenant_id, stages=[Stage.CLOSED_WON], as_of=as_of)


def all_stage_line_item_facts(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    """動的レポートビルダー用: ステージを問わず全商談の明細をファクト化する。
    パイプライン中の商談は EngagementLineItem(現在の構成)、closed_won は
    line_item_facts() と同じロジックで実績額を返す。"""
    return _line_item_facts(session, tenant_id, stages=None)


def filter_facts(
    facts: list[dict], *, product_group_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None, account_id: uuid.UUID | None = None,
    sales_group_id: uuid.UUID | None = None, relationship_type: str | None = None,
    stage: str | None = None, owner_user_id: uuid.UUID | None = None,
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
        if stage and f["stage"] != stage:
            return False
        if owner_user_id and (not f["owner_user"] or f["owner_user"].id != owner_user_id):
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


# --- 動的レポートビルダー(2026-08-14) ---------------------------------------
#
# 「行の軸」「列の軸(任意)」をユーザーが自由に選び、line_item_facts /
# all_stage_line_item_facts の上でクロス集計するための基盤。個々の軸の
# 意味(ラベルの作り方・ドリルダウン先ID)は Dimension が持ち、画面側は
# DIMENSION_CHOICES から key を選ぶだけでよい。

def _period_label(f: dict, granularity: str) -> str:
    d: date | None = f["period_date"]
    if d is None:
        return "未設定"
    if granularity == "quarter":
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"
    return f"{d.year}-{d.month:02d}"


@dataclass(slots=True)
class Dimension:
    key: str
    label: str
    key_fn: Callable[[dict], str]
    id_fn: Callable[[dict], object | None] | None = None


def build_dimensions(period_granularity: str = "month") -> dict[str, Dimension]:
    """period_granularity("month"|"quarter")に応じて period 軸のラベルの
    粒度を切り替える以外は固定のディメンション一覧を返す。"""
    return {
        "product": Dimension(
            "product", "商品",
            lambda f: f["product"].name if f["product"] else "未分類",
            lambda f: f["product"].id if f["product"] else None,
        ),
        "product_group": Dimension(
            "product_group", "商品グループ",
            lambda f: f["product_group"].name if f["product_group"] else "未分類",
            lambda f: f["product_group"].id if f["product_group"] else None,
        ),
        "account": Dimension(
            "account", "取引先(法人グループ)",
            lambda f: f["root_account"].name if f["root_account"] else "—",
            lambda f: f["root_account"].id if f["root_account"] else None,
        ),
        "sales_group": Dimension(
            "sales_group", "セールスグループ",
            lambda f: f["sales_group"].name if f["sales_group"] else "未設定",
            lambda f: f["sales_group"].id if f["sales_group"] else None,
        ),
        "relationship_type": Dimension(
            "relationship_type", "関係性",
            lambda f: RELATIONSHIP_TYPE_REPORT_LABELS.get(
                f["relationship_type"], f["relationship_type"],
            ),
            lambda f: f["relationship_type"],
        ),
        "stage": Dimension(
            "stage", "ステージ",
            lambda f: STAGE_REPORT_LABELS.get(f["stage"], f["stage"]),
            lambda f: f["stage"],
        ),
        "owner": Dimension(
            "owner", "担当者",
            lambda f: f["owner_user"].name if f["owner_user"] else "未割当",
            lambda f: f["owner_user"].id if f["owner_user"] else None,
        ),
        "period": Dimension(
            "period", "期間",
            lambda f: _period_label(f, period_granularity),
        ),
    }


def pivot(
    facts: list[dict], row_dim: Dimension, col_dim: Dimension | None = None,
) -> dict:
    """col_dim が None なら aggregate_by 相当の1軸集計(行ラベル・金額・
    件数・id)を "rows" に入れて返す。col_dim があれば行×列のクロス集計を
    "row_labels"/"col_labels"/"cells"/"row_totals"/"col_totals"/"grand_total"
    に入れて返す(cells は cells[row_label][col_label] でセルを引ける
    ネスト辞書 — Jinja テンプレート側からタプルキーを使わずに引けるように
    するため、あえて (row, col) タプルキーの単一辞書にしていない)。"""
    if col_dim is None:
        return {"rows": aggregate_by(facts, row_dim.key_fn, row_dim.id_fn)}

    cells: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"amount": Decimal("0"), "count": 0})
    )
    row_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    col_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    row_ids: dict[str, object | None] = {}
    grand_total = Decimal("0")

    for f in facts:
        row_label = row_dim.key_fn(f)
        col_label = col_dim.key_fn(f)
        cell = cells[row_label][col_label]
        cell["amount"] += f["amount"]
        cell["count"] += 1
        row_totals[row_label] += f["amount"]
        col_totals[col_label] += f["amount"]
        grand_total += f["amount"]
        if row_dim.id_fn is not None and row_label not in row_ids:
            row_ids[row_label] = row_dim.id_fn(f)

    row_labels = sorted(row_totals, key=lambda k: row_totals[k], reverse=True)
    col_labels = sorted(col_totals)
    cells = {row: dict(col_map) for row, col_map in cells.items()}

    return {
        "row_labels": row_labels, "col_labels": col_labels,
        "row_ids": row_ids, "cells": dict(cells),
        "row_totals": dict(row_totals), "col_totals": dict(col_totals),
        "grand_total": grand_total,
    }
