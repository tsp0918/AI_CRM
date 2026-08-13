"""ERP品目マスタの手動インポート(アップサート)。

material_code をキーにテナント内でアップサートする — 同じ品目コードを
再インポートしたときに重複行が増えないようにするため。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import FeftaJudgment, MaterialType
from ..models import ErpMaterial


def upsert_erp_material(
    session: Session, tenant_id: uuid.UUID, *,
    material_code: str, description: str, material_type: MaterialType | str,
    base_unit: str, standard_price: Decimal, currency: str,
    hs_code: str | None = None, eccn: str | None = None,
    fefta_judgment: FeftaJudgment | str = FeftaJudgment.UNKNOWN,
    country_of_origin: str | None = None, is_active: bool = True,
) -> ErpMaterial:
    material_code = material_code.strip()
    material = session.execute(
        select(ErpMaterial).where(
            ErpMaterial.tenant_id == tenant_id, ErpMaterial.material_code == material_code,
        )
    ).scalar_one_or_none()
    if material is None:
        material = ErpMaterial(tenant_id=tenant_id, material_code=material_code)
        session.add(material)

    material.description = description
    material.material_type = MaterialType(material_type).value
    material.base_unit = base_unit
    material.standard_price = standard_price
    material.currency = currency
    material.hs_code = hs_code
    material.eccn = eccn
    material.fefta_judgment = FeftaJudgment(fefta_judgment).value
    material.country_of_origin = country_of_origin
    material.is_active = is_active
    material.imported_at = datetime.now(timezone.utc)
    session.flush()
    return material


def list_erp_materials(session: Session, tenant_id: uuid.UUID) -> list[ErpMaterial]:
    return session.execute(
        select(ErpMaterial).where(ErpMaterial.tenant_id == tenant_id)
        .order_by(ErpMaterial.material_code)
    ).scalars().all()
