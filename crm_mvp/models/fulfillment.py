"""契約の実績3層管理(2026-08-15, CRM_連携引き継ぎ書.md §7.6)。

契約額(CRMが保持)・出荷実績(ERPのDelivery, IF-29)・請求実績(ERPの
BillingDocument, IF-30)を別々に積み上げる。「invoice発行だけを見る」と
残り(受注残なのか失注なのか)が分からなくなるため、出荷と請求を別イベント
として記録する。返品(IF-31)は`kind="return"`でマイナス計上する。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk


class ContractFulfillment(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "contract_fulfillment"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), index=True)  # shipment | billing | return
    erp_document_number: Mapped[str] = mapped_column(String(64))
    product_code: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # 同一ERP文書番号の重複計上を防ぐ(IF-29/30/31の冪等性)。
        UniqueConstraint(
            "tenant_id", "kind", "erp_document_number",
            name="uq_contract_fulfillment_tenant_kind_doc",
        ),
    )
