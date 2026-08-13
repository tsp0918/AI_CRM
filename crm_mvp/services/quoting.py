"""見積もり作成・契約発行(商談管理オブジェクト内のサブオブジェクト)。

Quote/Contract は作成時点の EngagementLineItem(または元にした Quote)を
スナップショットとして凍結する。あとから商品構成を変えても、発行済みの
見積もり・契約の内容は変わらない — 「なぜその金額で合意したか」を
後から辿れることを優先する(このアプリ全体の evidence 保存の思想と同じ)。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import ContractStatus, QuoteStatus
from ..models import Contract, ContractLineItem, Engagement, Quote, QuoteLineItem
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
