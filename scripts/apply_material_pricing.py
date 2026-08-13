"""ROH/HALB/HAWA(原材料・中間体・消耗品)区分のERP品目に、商品グループと
カテゴリ別粗利率で販売価格を設定する。scripts/apply_fert_pricing.py の
FERT(完成品)版に対する、それ以外の区分向けの版。

  python scripts/apply_material_pricing.py --tenant-id <uuid>

FERTは半導体材料メーカーが完成品(フォトレジスト・CMPスラリー等)を
ファブに売る商流だったのに対し、ここでは:
  - 原材料(ROH)を製造メーカーへ販売する商流(距離感の近い川上取引、
    薄利多売寄りの粗利率)
  - 中間体(HALB)— 一部は化学系の半製品ベース、一部は装置サブアセンブリ
    部材(ポンプ・PCB・配管等)で性質が異なるため2グループに分ける
  - 消耗品・交換部品(HAWA)— O-リング、フィルター、MFC等の補修部材

粗利率はカテゴリごとに変える(2026-08-13 ユーザー要望: 装置販売・原材料
販売など商流を多岐に広げる)。コモディティに近い原材料ほど粗利は薄く、
エンジニアリング価値の高い装置サブアセンブリほど厚くしてある — 実務的な
妥当性を優先した仮定であり、絶対値はあくまでデモ用の目安。
"""

from __future__ import annotations

import argparse
import os
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from crm_mvp.models import ErpMaterial, Product, ProductGroup

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
TWO_PLACES = Decimal("0.01")

# (親グループ名 or None, グループ名, 粗利率(%), 対象material_code)
GROUP_DEFINITIONS: list[tuple[str | None, str, Decimal, list[str]]] = [
    ("原材料", "高純度溶剤", Decimal("20"), [
        "MAT-9000001", "MAT-R0001", "MAT-R0002", "MAT-R0011",
    ]),
    ("原材料", "酸・アルカリ薬液", Decimal("18"), [
        "MAT-9000002", "MAT-9000007", "MAT-R0003", "MAT-R0004",
    ]),
    ("原材料", "前駆体・ドーパントガス", Decimal("30"), [
        "MAT-R0005", "MAT-R0006", "MAT-R0007", "MAT-R0008", "MAT-R0009", "MAT-R0010",
    ]),
    ("原材料", "添加剤・機能性薬液", Decimal("25"), [
        "MAT-9000003", "MAT-9000005", "MAT-9000006", "MAT-9000008",
    ]),
    ("原材料", "半導体部材・基板", Decimal("22"), [
        "MAT-DMIC-C01", "MAT-DMIC-C02", "MAT-DMIC-C03", "MAT-DMIC-C04", "MAT-DMIC-C05",
        "PKG-CERAMIC", "SIL-WAF-JP", "SIL-WAF-US", "ADH-EPOXY-01",
    ]),
    ("中間体", "中間体・ベース材料", Decimal("35"), [
        "MAT-8000001", "MAT-9000004", "MAT-I0001", "MAT-I0002", "MAT-I0003",
        "MAT-I0004", "MAT-I0005",
    ]),
    ("中間体", "装置サブアセンブリ部材", Decimal("38"), [
        "MAT-I0006", "MAT-I0007", "MAT-I0008", "MAT-M0001", "MAT-M0002", "MAT-M0003",
    ]),
    (None, "消耗品・交換部品", Decimal("28"), [
        "MAT-9100001", "MAT-P0001", "MAT-P0002", "MAT-P0003", "MAT-P0004", "MAT-P0005",
    ]),
]


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def list_price_for_margin(cost: Decimal, margin_rate: Decimal) -> Decimal:
    return _round(cost / (Decimal("1") - margin_rate / Decimal("100")))


def get_or_create_group(
    session: Session, name: str, parent_id: uuid.UUID | None = None,
) -> ProductGroup:
    group = session.execute(
        select(ProductGroup).where(ProductGroup.tenant_id == TENANT_ID, ProductGroup.name == name)
    ).scalar_one_or_none()
    if group is None:
        group = ProductGroup(tenant_id=TENANT_ID, name=name, parent_group_id=parent_id)
        session.add(group)
        session.flush()
    elif parent_id is not None and group.parent_group_id != parent_id:
        group.parent_group_id = parent_id
        session.flush()
    return group


def upsert_product_from_material(
    session: Session, material: ErpMaterial, group: ProductGroup, margin_rate: Decimal,
) -> Product:
    product = session.execute(
        select(Product).where(
            Product.tenant_id == TENANT_ID, Product.erp_material_id == material.id,
        )
    ).scalar_one_or_none()
    list_price = list_price_for_margin(material.standard_price, margin_rate)

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

    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, false)"),
            {"tid": str(TENANT_ID)},
        )

        parent_names = {p for p, _, _, _ in GROUP_DEFINITIONS if p is not None}
        parents = {name: get_or_create_group(session, name) for name in parent_names}
        session.flush()

        code_to_group_name = {
            code: name for _, name, _, codes in GROUP_DEFINITIONS for code in codes
        }
        code_to_margin = {
            code: margin for _, _, margin, codes in GROUP_DEFINITIONS for code in codes
        }

        materials = session.execute(
            select(ErpMaterial).where(
                ErpMaterial.tenant_id == TENANT_ID,
                ErpMaterial.material_type.in_(["ROH", "HALB", "HAWA"]),
            ).order_by(ErpMaterial.material_code)
        ).scalars().all()

        unmapped = [m.material_code for m in materials if m.material_code not in code_to_group_name]
        if unmapped:
            raise SystemExit(
                f"分類マッピングに無い品目があります(GROUP_DEFINITIONSを更新してください): {unmapped}"
            )

        groups: dict[str, ProductGroup] = dict(parents)
        for parent_name, group_name, _margin, _codes in GROUP_DEFINITIONS:
            parent_id = parents[parent_name].id if parent_name else None
            groups[group_name] = get_or_create_group(session, group_name, parent_id)
        session.flush()

        count = 0
        for material in materials:
            group = groups[code_to_group_name[material.material_code]]
            margin = code_to_margin[material.material_code]
            product = upsert_product_from_material(session, material, group, margin)
            count += 1
            print(
                f"  {material.material_code:15} {product.name[:45]:45} "
                f"原価 {material.standard_price:>10,.0f} → 定価 {product.list_price:>12,.0f} "
                f"[{group.name} / 粗利{margin}%]"
            )

        session.commit()

    print(f"\n{count}件のROH/HALB/HAWA品目に商品グループ・カテゴリ別粗利で販売価格を設定しました(テナント {TENANT_ID})。")


if __name__ == "__main__":
    main()
