"""ERP品目マスタ(Material)を受け止める「箱」。

フィールド名・型は erp-system の実API `GET /mdm/materials` のレスポンス
形状(docs/erp_crm_spec.md §6 品目マスタ)にそのまま合わせてある。今回は
手動インポートのみで運用するが、将来API連携に切り替える際にこの
テーブル構造の変更が不要になるようにするための選択。

standard_price は ERP内部でもBOM原価ロールアップに使われている実績が
あり、CRM側では「原価」として扱う。Product.list_price(CRM側の販売価格)
との差分が Gross Margin(粗利)になる。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk
from ..enums import FeftaJudgment, MaterialType


class ErpMaterial(Base, UUIDPk, Timestamped, TenantScoped):
    """ERP品目マスタの1行(テナント内で material_code は一意)。"""

    __tablename__ = "erp_material"
    __table_args__ = (
        UniqueConstraint("tenant_id", "material_code", name="uq_erp_material_tenant_code"),
    )

    material_code: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str] = mapped_column(String(255))
    material_type: Mapped[str] = mapped_column(String(10), default=MaterialType.FERT.value)
    base_unit: Mapped[str] = mapped_column(String(5), default="PC")
    standard_price: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    hs_code: Mapped[str | None] = mapped_column(String(20))
    eccn: Mapped[str | None] = mapped_column(String(20))
    fefta_judgment: Mapped[str] = mapped_column(String(20), default=FeftaJudgment.UNKNOWN.value)
    country_of_origin: Mapped[str | None] = mapped_column(String(2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
