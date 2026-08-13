"""商品グループ(CRM独自の分類軸)。

ERP側(erp-system)には商用の分類軸が存在しない(material_type はSAP的な
完成品/半製品/原材料/商品の区分であり、営業側が使う分類感覚とは別物)。
そのためCRM側で独自に持つ。親子関係による階層構造を許すが、循環参照は
サービス層(services/product_groups.py)でガードする。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk


class ProductGroup(Base, UUIDPk, Timestamped, TenantScoped):
    __tablename__ = "product_group"

    name: Mapped[str] = mapped_column(String(120))
    parent_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product_group.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[str | None] = mapped_column(Text)
