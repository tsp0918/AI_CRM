"""ERP取引先マスタ(BusinessPartner)を受け止める「箱」。

crm_mvp/models/erp_material.py と同じ考え方 — フィールド名・型は
erp-system の実エクスポート(scripts/data/erp_business_partners.csv)の
形状にそのまま合わせる。CRM側の Account はこの箱を直接参照しない
(Account には元々 external_system/external_id という汎用の外部連携キーが
あるため、そちらを "erp" / bp_code で使う — 新しいFK列は増やさない)。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk


class ErpBusinessPartner(Base, UUIDPk, Timestamped, TenantScoped):
    """ERP取引先マスタの1行(テナント内で bp_code は一意)。"""

    __tablename__ = "erp_business_partner"
    __table_args__ = (
        UniqueConstraint("tenant_id", "bp_code", name="uq_erp_business_partner_tenant_code"),
    )

    bp_code: Mapped[str] = mapped_column(String(20), index=True)
    bp_type: Mapped[str] = mapped_column(String(10), default="ORG")
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2))
    # カンマ区切り複数可: "CUSTOMER" / "VENDOR" / "CUSTOMER,VENDOR" 等
    roles: Mapped[str] = mapped_column(String(100), default="CUSTOMER")
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    payment_terms: Mapped[str | None] = mapped_column(String(20))
    currency: Mapped[str | None] = mapped_column(String(3))
    is_denied_party: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
