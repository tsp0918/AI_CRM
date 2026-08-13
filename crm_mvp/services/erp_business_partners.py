"""ERP取引先マスタの手動インポート(アップサート)。

bp_code をキーにテナント内でアップサートする(services/erp_materials.py と
同じ考え方)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ErpBusinessPartner


def upsert_erp_business_partner(
    session: Session, tenant_id: uuid.UUID, *,
    bp_code: str, name: str, bp_type: str = "ORG", country: str | None = None,
    roles: str = "CUSTOMER", email: str | None = None, phone: str | None = None,
    address_line1: str | None = None, address_line2: str | None = None,
    city: str | None = None, postal_code: str | None = None,
    credit_limit: Decimal | None = None, payment_terms: str | None = None,
    currency: str | None = None, is_denied_party: bool = False, is_active: bool = True,
) -> ErpBusinessPartner:
    bp_code = bp_code.strip()
    partner = session.execute(
        select(ErpBusinessPartner).where(
            ErpBusinessPartner.tenant_id == tenant_id, ErpBusinessPartner.bp_code == bp_code,
        )
    ).scalar_one_or_none()
    if partner is None:
        partner = ErpBusinessPartner(tenant_id=tenant_id, bp_code=bp_code)
        session.add(partner)

    partner.name = name
    partner.bp_type = bp_type
    partner.country = country
    partner.roles = roles
    partner.email = email
    partner.phone = phone
    partner.address_line1 = address_line1
    partner.address_line2 = address_line2
    partner.city = city
    partner.postal_code = postal_code
    partner.credit_limit = credit_limit
    partner.payment_terms = payment_terms
    partner.currency = currency
    partner.is_denied_party = is_denied_party
    partner.is_active = is_active
    partner.imported_at = datetime.now(timezone.utc)
    session.flush()
    return partner


def list_erp_business_partners(
    session: Session, tenant_id: uuid.UUID, *, role: str | None = None,
) -> list[ErpBusinessPartner]:
    stmt = select(ErpBusinessPartner).where(ErpBusinessPartner.tenant_id == tenant_id)
    if role is not None:
        stmt = stmt.where(ErpBusinessPartner.roles.contains(role))
    return session.execute(stmt.order_by(ErpBusinessPartner.bp_code)).scalars().all()
