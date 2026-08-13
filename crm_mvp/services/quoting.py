"""見積もり作成・契約発行(商談管理オブジェクト内のサブオブジェクト)。

Quote/Contract は作成時点の EngagementLineItem(または元にした Quote)を
スナップショットとして凍結する。あとから商品構成を変えても、発行済みの
見積もり・契約の内容は変わらない — 「なぜその金額で合意したか」を
後から辿れることを優先する(このアプリ全体の evidence 保存の思想と同じ)。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import ContractStatus, QuoteStatus
from ..models import (
    Account, Contract, ContractLineItem, Engagement, Product, Quote,
    QuoteLineItem, SalesGroup, User,
)
from .pricing import list_line_items


def _generate_number(session: Session, tenant_id: uuid.UUID, model, prefix: str) -> str:
    year = datetime.now(timezone.utc).year
    count = session.execute(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    ).scalar_one()
    return f"{prefix}-{year}-{count + 1:04d}"


def create_quote_from_engagement(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement, *,
    valid_until: date | None, actor: str,
) -> Quote:
    items = list_line_items(session, tenant_id, engagement.id)
    if not items:
        raise ValueError("商品構成が無い案件から見積もりは作成できません")

    quote = Quote(
        tenant_id=tenant_id, engagement_id=engagement.id,
        quote_number=_generate_number(session, tenant_id, Quote, "Q"),
        status=QuoteStatus.DRAFT, valid_until=valid_until,
        total_amount=sum((i.line_total for i in items), Decimal("0")),
        currency=engagement.currency, written_by=actor,
    )
    session.add(quote)
    session.flush()

    for item in items:
        session.add(QuoteLineItem(
            tenant_id=tenant_id, quote_id=quote.id, product_id=item.product_id,
            product_name_snapshot=item.product_name_snapshot, quantity=item.quantity,
            list_price_snapshot=item.list_price_snapshot, discount_rate=item.discount_rate,
            unit_price=item.unit_price, line_total=item.line_total,
        ))
    session.flush()
    return quote


def list_quote_line_items(
    session: Session, tenant_id: uuid.UUID, quote_id: uuid.UUID,
) -> list[QuoteLineItem]:
    return session.execute(
        select(QuoteLineItem).where(
            QuoteLineItem.tenant_id == tenant_id, QuoteLineItem.quote_id == quote_id,
        )
    ).scalars().all()


def update_quote_status(quote: Quote, status: QuoteStatus) -> Quote:
    quote.status = status
    if status == QuoteStatus.SENT and quote.issued_at is None:
        quote.issued_at = datetime.now(timezone.utc)
    return quote


def list_quotes(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID | None = None,
) -> list[Quote]:
    stmt = select(Quote).where(Quote.tenant_id == tenant_id)
    if engagement_id is not None:
        stmt = stmt.where(Quote.engagement_id == engagement_id)
    return session.execute(stmt.order_by(Quote.created_at.desc())).scalars().all()


def create_contract(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement, *,
    quote: Quote | None = None, start_date: date | None = None,
    end_date: date | None = None, actor: str,
) -> Contract:
    """quote が指定されればその明細をコピーする。無指定なら現在の
    Engagement の商品構成を直接コピーする(見積もりを介さない直接発行)。"""
    if quote is not None:
        source_items = list_quote_line_items(session, tenant_id, quote.id)
    else:
        source_items = list_line_items(session, tenant_id, engagement.id)
    if not source_items:
        raise ValueError("商品構成(または見積もり)が無いため契約を発行できません")

    contract = Contract(
        tenant_id=tenant_id, engagement_id=engagement.id,
        quote_id=quote.id if quote else None,
        contract_number=_generate_number(session, tenant_id, Contract, "C"),
        status=ContractStatus.DRAFT, start_date=start_date, end_date=end_date,
        total_amount=sum((i.line_total for i in source_items), Decimal("0")),
        currency=engagement.currency, written_by=actor,
    )
    session.add(contract)
    session.flush()

    for item in source_items:
        session.add(ContractLineItem(
            tenant_id=tenant_id, contract_id=contract.id, product_id=item.product_id,
            product_name_snapshot=item.product_name_snapshot, quantity=item.quantity,
            list_price_snapshot=item.list_price_snapshot, discount_rate=item.discount_rate,
            unit_price=item.unit_price, line_total=item.line_total,
        ))
    session.flush()
    return contract


def list_contract_line_items(
    session: Session, tenant_id: uuid.UUID, contract_id: uuid.UUID,
) -> list[ContractLineItem]:
    return session.execute(
        select(ContractLineItem).where(
            ContractLineItem.tenant_id == tenant_id,
            ContractLineItem.contract_id == contract_id,
        )
    ).scalars().all()


def update_contract_status(contract: Contract, status: ContractStatus) -> Contract:
    contract.status = status
    if status == ContractStatus.SIGNED and contract.signed_at is None:
        contract.signed_at = datetime.now(timezone.utc)
    return contract


def list_contracts(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID | None = None,
) -> list[Contract]:
    stmt = select(Contract).where(Contract.tenant_id == tenant_id)
    if engagement_id is not None:
        stmt = stmt.where(Contract.engagement_id == engagement_id)
    return session.execute(stmt.order_by(Contract.created_at.desc())).scalars().all()


# --- 見積・契約一覧の階層フィルタ基盤(2026-08-14) --------------------------
#
# revenue_report.py の line_item_facts と同じ設計思想。ただし1見積/1契約は
# 複数商品を持ちうるため、fact行の粒度は「明細」ではなく「文書1件=1行」とし、
# product_ids/product_group_ids を集合として持たせる(商品絞り込みは
# 部分一致=メンバーシップ判定になる)。

def _enrich_documents(
    session: Session, tenant_id: uuid.UUID, documents: list, line_items: list, doc_id_attr: str,
) -> list[dict]:
    engagement_ids = {d.engagement_id for d in documents}
    engagements = {
        e.id: e for e in session.execute(
            select(Engagement).where(Engagement.id.in_(engagement_ids))
        ).scalars()
    } if engagement_ids else {}

    account_ids = {e.account_id for e in engagements.values()}
    accounts = {
        a.id: a for a in session.execute(
            select(Account).where(Account.id.in_(account_ids))
        ).scalars()
    } if account_ids else {}

    def walk_to_root(account: Account | None) -> Account | None:
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

    sales_group_ids = {e.sales_group_id for e in engagements.values() if e.sales_group_id}
    sales_groups = {
        g.id: g for g in session.execute(
            select(SalesGroup).where(SalesGroup.id.in_(sales_group_ids))
        ).scalars()
    } if sales_group_ids else {}

    owner_ids = {e.owner_user_id for e in engagements.values() if e.owner_user_id}
    users = {
        u.id: u for u in session.execute(
            select(User).where(User.id.in_(owner_ids))
        ).scalars()
    } if owner_ids else {}

    product_ids_by_doc: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    all_product_ids: set[uuid.UUID] = set()
    for li in line_items:
        if li.product_id:
            product_ids_by_doc[getattr(li, doc_id_attr)].add(li.product_id)
            all_product_ids.add(li.product_id)

    products = {
        p.id: p for p in session.execute(
            select(Product).where(Product.id.in_(all_product_ids))
        ).scalars()
    } if all_product_ids else {}

    facts = []
    for doc in documents:
        engagement = engagements.get(doc.engagement_id)
        account = accounts.get(engagement.account_id) if engagement else None
        product_ids = product_ids_by_doc.get(doc.id, set())
        product_group_ids = {
            products[pid].product_group_id for pid in product_ids
            if products.get(pid) and products[pid].product_group_id
        }
        facts.append({
            "document": doc, "engagement": engagement, "account": account,
            "root_account": walk_to_root(account),
            "sales_group": sales_groups.get(engagement.sales_group_id)
            if engagement and engagement.sales_group_id else None,
            "owner_user": users.get(engagement.owner_user_id)
            if engagement and engagement.owner_user_id else None,
            "product_ids": product_ids, "product_group_ids": product_group_ids,
        })
    return facts


def quote_document_facts(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    quotes = list_quotes(session, tenant_id)
    if not quotes:
        return []
    line_items = session.execute(
        select(QuoteLineItem).where(
            QuoteLineItem.tenant_id == tenant_id,
            QuoteLineItem.quote_id.in_([q.id for q in quotes]),
        )
    ).scalars().all()
    return _enrich_documents(session, tenant_id, quotes, line_items, "quote_id")


def contract_document_facts(
    session: Session, tenant_id: uuid.UUID, contracts: list[Contract] | None = None,
) -> list[dict]:
    """contracts を渡すとその集合だけをfact化する(契約更新画面がACTIVE+
    期限内に絞り込んだ後で同じ基盤を再利用するため)。省略時はテナント全件。"""
    if contracts is None:
        contracts = list_contracts(session, tenant_id)
    if not contracts:
        return []
    line_items = session.execute(
        select(ContractLineItem).where(
            ContractLineItem.tenant_id == tenant_id,
            ContractLineItem.contract_id.in_([c.id for c in contracts]),
        )
    ).scalars().all()
    return _enrich_documents(session, tenant_id, contracts, line_items, "contract_id")


def filter_documents(
    facts: list[dict], *, account_ids: set[uuid.UUID] | None = None,
    product_group_ids: set[uuid.UUID] | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> list[dict]:
    """account_ids/product_group_ids は呼び出し側でロールアップ済みの集合
    (get_family_account_ids/get_family_product_group_ids の結果)を渡す。"""
    def matches(f: dict) -> bool:
        if account_ids is not None and (not f["account"] or f["account"].id not in account_ids):
            return False
        if product_group_ids is not None and not (f["product_group_ids"] & product_group_ids):
            return False
        if owner_user_id is not None and (not f["owner_user"] or f["owner_user"].id != owner_user_id):
            return False
        return True

    return [f for f in facts if matches(f)]
