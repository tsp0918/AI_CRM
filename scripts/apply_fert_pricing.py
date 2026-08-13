"""FERT(完成品)区分のERP品目に、商品グループと粗利60%の販売価格を設定する。

  python scripts/apply_fert_pricing.py --tenant-id <uuid>

対象は material_type=FERT の ErpMaterial のみ(2026-08-13 ユーザー指示)。
商品名はERP品目の description(製品名)をそのまま踏襲する。商品グループは
scripts/data/erp_product_list.csv の中身(半導体製造材料)を実際に確認して
手作業で分類した固定マッピング(_GROUP_MATERIAL_CODES)を使う — 自動分類
ロジックは組まず、素材の実態を見て決めた分類そのものをコードとして残す。

価格は「粗利率60%になる販売価格」を逆算する:
  list_price = 原価 / (1 - 0.60) = 原価 × 2.5

商品グループ・Productは同名/同ErpMaterialであれば再実行時に上書き更新する
(アップサート)。
"""

from __future__ import annotations

import argparse
import os
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_mvp.models import ErpMaterial, Product, ProductGroup

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
TARGET_MARGIN_RATE = Decimal("60")
TWO_PLACES = Decimal("0.01")

# scripts/data/erp_product_list.csv の内容を実際に確認して分類した
# 半導体製造材料のグルーピング。「ある程度リーズナブルな範囲で」という
# 指示に沿って、工程上の役割で7グループに分けた。
_GROUP_MATERIAL_CODES: dict[str, list[str]] = {
    "フォトレジスト・パターニング材料": [
        "MAT-1000001", "MAT-1000002", "MAT-1000003", "MAT-F0001", "MAT-F0003",
    ],
    "CMPスラリー": [
        "MAT-2000001", "MAT-2000002", "MAT-F0002", "MAT-F0006", "MAT-F0010",
    ],
    "エッチング・洗浄薬液": [
        "MAT-3000001", "MAT-3000002", "MAT-F0005", "MAT-F0008", "MAT-F0009",
    ],
    "プロセスガス・前駆体": [
        "MAT-4000001", "MAT-4000002", "MAT-4000003", "MAT-F0004",
    ],
    "薄膜・誘電体材料": ["MAT-F0007"],
    "電子部品・IC": ["MAT-DMIC-001", "CTRL-HC200"],
    "製造装置": ["MAT-E0001", "MAT-E0002", "MAT-E0003", "MAT-E0004"],
}


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def list_price_for_margin(cost: Decimal, margin_rate: Decimal) -> Decimal:
    """粗利率(%)から逆算した販売価格。margin = (price-cost)/price なので
    price = cost / (1 - margin/100)。"""
    return _round(cost / (Decimal("1") - margin_rate / Decimal("100")))


def get_or_create_group(session: Session, name: str) -> ProductGroup:
    group = session.execute(
        select(ProductGroup).where(ProductGroup.tenant_id == TENANT_ID, ProductGroup.name == name)
    ).scalar_one_or_none()
    if group is None:
        group = ProductGroup(tenant_id=TENANT_ID, name=name)
        session.add(group)
        session.flush()
    return group


def upsert_product_from_material(
    session: Session, material: ErpMaterial, group: ProductGroup,
) -> Product:
    product = session.execute(
        select(Product).where(
            Product.tenant_id == TENANT_ID, Product.erp_material_id == material.id,
        )
    ).scalar_one_or_none()
    list_price = list_price_for_margin(material.standard_price, TARGET_MARGIN_RATE)

    if product is None:
        product = Product(tenant_id=TENANT_ID, erp_material_id=material.id)
        session.add(product)

    product.name = material.description
    product.currency = material.currency
    product.list_price = list_price
    product.product_group_id = group.id
    product.is_active = True
    session.flush()
    return product


def main() -> None:
    global TENANT_ID
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID)
    args = parser.parse_args()
    TENANT_ID = args.tenant_id

    code_to_group_name = {
        code: group_name
        for group_name, codes in _GROUP_MATERIAL_CODES.items()
        for code in codes
    }

    from sqlalchemy import create_engine, text

    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, false)"),
            {"tid": str(TENANT_ID)},
        )

        materials = session.execute(
            select(ErpMaterial).where(
                ErpMaterial.tenant_id == TENANT_ID, ErpMaterial.material_type == "FERT",
            ).order_by(ErpMaterial.material_code)
        ).scalars().all()

        unmapped = [m.material_code for m in materials if m.material_code not in code_to_group_name]
        if unmapped:
            raise SystemExit(
                f"分類マッピングに無いFERT品目があります(_GROUP_MATERIAL_CODESを更新してください): {unmapped}"
            )

        groups = {name: get_or_create_group(session, name) for name in _GROUP_MATERIAL_CODES}
        session.flush()

        created_or_updated = 0
        for material in materials:
            group = groups[code_to_group_name[material.material_code]]
            product = upsert_product_from_material(session, material, group)
            created_or_updated += 1
            print(
                f"  {material.material_code:15} {product.name[:45]:45} "
                f"原価 {material.standard_price:>12,.0f} → 定価 {product.list_price:>12,.0f} "
                f"[{group.name}]"
            )

        session.commit()

    print(
        f"\n{created_or_updated}件のFERT品目に商品グループ・粗利60%の販売価格を設定しました"
        f"(テナント {TENANT_ID})。"
    )


if __name__ == "__main__":
    main()
