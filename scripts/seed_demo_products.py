"""デモ用 ERP品目マスタ/商品グループ/Product Priceリスト + 見積もり/契約
サンプルの投入コマンド。

  python scripts/seed_demo_products.py --tenant-id <uuid>

指定テナントの erp_material / product_group / product /
engagement_line_item / quote / quote_line_item / contract /
contract_line_item を全削除したうえで:
  - ダミーの ErpMaterial(ERP品目マスタの「箱」、standard_price=原価)を登録
  - 商品グループ(階層構造)を登録
  - ErpMaterial に販売価格・商品グループを付加した Product(CRM価格表)を作成
    (これにより商品一覧で原価・粗利率が表示される)
  - scripts/seed_demo_pipeline.py で作成済みの「東北製作所株式会社」の
    既存契約案件に商品構成を組み、見積もり(SENT→ACCEPTED)と
    契約(SIGNED→ACTIVE)まで実際のサービス関数を通して作成する

先に seed_demo_pipeline.py を実行しておくこと。対象の案件が見つからない
場合は Product 等の登録のみ行う。

§7.4: crm_app ロール(非 superuser)で接続し、RLS のテナント文脈を
自分で SET してから実行する。
"""

from __future__ import annotations

import argparse
import os
import uuid
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from crm_mvp.enums import ContractStatus, QuoteStatus
from crm_mvp.models import Engagement, ErpMaterial, Product, ProductGroup
from crm_mvp.services.erp_materials import upsert_erp_material
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.product_groups import create_product_group
from crm_mvp.services.quoting import (
    create_contract, create_quote_from_engagement, update_contract_status,
    update_quote_status,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
ACTOR_ID = uuid.uuid4()

# ERP品目マスタ(standard_price = 原価)。material_code は erp-system の
# MAT-XXXXXXX 形式を踏襲。
MATERIALS = [
    dict(
        material_code="MAT-0001001", description="検査装置 標準モデル",
        material_type="FERT", base_unit="PC", standard_price=Decimal("21000000"),
        currency="JPY",
    ),
    dict(
        material_code="MAT-0001002", description="検査装置 オプション: 高速搬送ユニット",
        material_type="FERT", base_unit="PC", standard_price=Decimal("5600000"),
        currency="JPY",
    ),
    dict(
        material_code="MAT-0002001", description="導入設置・調整サービス",
        material_type="HAWA", base_unit="SET", standard_price=Decimal("2400000"),
        currency="JPY",
    ),
    dict(
        material_code="MAT-0002002", description="保守サポート(年間)",
        material_type="HAWA", base_unit="SET", standard_price=Decimal("400000"),
        currency="JPY",
    ),
    dict(
        material_code="MAT-0003001", description="予備部品パッケージ",
        material_type="HAWA", base_unit="SET", standard_price=Decimal("350000"),
        currency="JPY",
    ),
]

# 商品グループ(親子階層)。値は (グループ名, 親グループ名 or None)。
PRODUCT_GROUPS = [
    ("検査装置", None),
    ("標準機", "検査装置"),
    ("オプション", "検査装置"),
    ("導入・保守サービス", None),
    ("消耗品・予備品", None),
]

# Product(CRM側の価格表)。material_code で ErpMaterial に、group で
# ProductGroup にそれぞれ紐づける。
PRODUCTS = [
    dict(
        name="検査装置 標準モデル", sku="INSP-STD-100", list_price=Decimal("30000000"),
        description="外観・寸法検査を自動化する標準ライン検査装置。",
        material_code="MAT-0001001", group="標準機",
    ),
    dict(
        name="検査装置 オプション: 高速搬送ユニット", sku="INSP-OPT-CONV",
        list_price=Decimal("8000000"), description="タクトタイム短縮用の高速搬送ユニット。",
        material_code="MAT-0001002", group="オプション",
    ),
    dict(
        name="導入設置・調整サービス", sku="SVC-INSTALL", list_price=Decimal("3000000"),
        description="現地設置・ライン調整・立ち上げ支援(1式)。",
        material_code="MAT-0002001", group="導入・保守サービス",
    ),
    dict(
        name="保守サポート(年間)", sku="SVC-SUPPORT-1Y", list_price=Decimal("1000000"),
        description="年間保守契約(定期点検・障害対応)。",
        material_code="MAT-0002002", group="導入・保守サービス",
    ),
    dict(
        name="予備部品パッケージ", sku="PART-SPARE-KIT", list_price=Decimal("500000"),
        description="消耗部品・予備品一式。",
        material_code="MAT-0003001", group="消耗品・予備品",
    ),
]


def wipe_tenant_pricing_data(session: Session) -> None:
    tables = [
        "contract_line_item", "quote_line_item", "contract", "quote",
        "engagement_line_item", "product", "product_group", "erp_material",
    ]
    for t in tables:
        session.execute(
            text(f"DELETE FROM {t} WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)}
        )


def seed_erp_materials(session: Session) -> dict[str, ErpMaterial]:
    materials = {}
    for spec in MATERIALS:
        material = upsert_erp_material(session, TENANT_ID, **spec)
        materials[material.material_code] = material
    return materials


def seed_product_groups(session: Session) -> dict[str, ProductGroup]:
    groups: dict[str, ProductGroup] = {}
    for name, parent_name in PRODUCT_GROUPS:
        parent = groups[parent_name] if parent_name else None
        groups[name] = create_product_group(
            session, TENANT_ID, name=name,
            parent_group_id=parent.id if parent else None,
        )
    return groups


def seed_products(
    session: Session, materials: dict[str, ErpMaterial], groups: dict[str, ProductGroup],
) -> dict[str, Product]:
    products = {}
    for spec in PRODUCTS:
        material = materials[spec["material_code"]]
        group = groups[spec["group"]]
        product = Product(
            tenant_id=TENANT_ID, name=spec["name"], sku=spec["sku"],
            list_price=spec["list_price"], currency="JPY",
            description=spec["description"], erp_material_id=material.id,
            product_group_id=group.id,
        )
        session.add(product)
        session.flush()
        products[product.sku] = product
    return products


def wire_demo_quote_and_contract(session: Session, products: dict[str, Product]) -> None:
    engagement = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == TENANT_ID, Engagement.name == "検査ライン一式導入契約",
        )
    ).scalar_one_or_none()
    if engagement is None:
        print(
            "対象案件(検査ライン一式導入契約)が見つからないため、"
            "見積もり/契約のサンプル作成はスキップします。"
            "先に scripts/seed_demo_pipeline.py を実行してください。"
        )
        return

    actor = f"human:{ACTOR_ID}"
    for sku, quantity in [
        ("INSP-STD-100", 1), ("INSP-OPT-CONV", 1), ("SVC-INSTALL", 1),
        ("SVC-SUPPORT-1Y", 1),
    ]:
        add_line_item(
            session, TENANT_ID, engagement, product=products[sku],
            quantity=quantity, discount_rate=Decimal("0"),
        )

    quote = create_quote_from_engagement(
        session, TENANT_ID, engagement, valid_until=None, actor=actor,
    )
    update_quote_status(quote, QuoteStatus.SENT)
    update_quote_status(quote, QuoteStatus.ACCEPTED)

    contract = create_contract(
        session, TENANT_ID, engagement, quote=quote,
        start_date=None, end_date=None, actor=actor,
    )
    update_contract_status(contract, ContractStatus.SIGNED)
    update_contract_status(contract, ContractStatus.ACTIVE)

    session.flush()
    print(
        f"{engagement.name}: 商品構成4件、見積もり {quote.quote_number}、"
        f"契約 {contract.contract_number} を作成しました(合計 "
        f"{contract.total_amount:,.0f} {contract.currency})。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存の品目/グループ/商品/明細/見積/契約はこのテナント分のみ全削除される)",
    )
    return parser.parse_args()


def main() -> None:
    global TENANT_ID
    args = parse_args()
    TENANT_ID = args.tenant_id

    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, false)"),
            {"tid": str(TENANT_ID)},
        )
        wipe_tenant_pricing_data(session)
        session.commit()

        materials = seed_erp_materials(session)
        groups = seed_product_groups(session)
        session.commit()

        products = seed_products(session, materials, groups)
        session.commit()

        wire_demo_quote_and_contract(session, products)
        session.commit()

    print(f"Seeded demo ERP materials / product groups / price list for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
