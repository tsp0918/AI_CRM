"""デモ用 Product Priceリスト + 見積もり/契約サンプルの投入コマンド。

  python scripts/seed_demo_products.py --tenant-id <uuid>

指定テナントの product / engagement_line_item / quote / quote_line_item /
contract / contract_line_item を全削除したうえで:
  - ダミーの Product(価格表)を数件登録する(ERP連携までのCRM内ダミー)
  - scripts/seed_demo_pipeline.py で作成済みの「東北製作所株式会社」の
    既存契約案件に商品構成を組み、見積もり(SENT→ACCEPTED)と
    契約(SIGNED→ACTIVE)まで実際のサービス関数を通して作成する

先に seed_demo_pipeline.py を実行しておくこと。対象の案件が見つからない
場合は Product の登録のみ行う。

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
from crm_mvp.models import Engagement, Product
from crm_mvp.services.pricing import add_line_item
from crm_mvp.services.quoting import (
    create_contract, create_quote_from_engagement, update_contract_status,
    update_quote_status,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID
ACTOR_ID = uuid.uuid4()

PRODUCTS = [
    dict(
        name="検査装置 標準モデル", sku="INSP-STD-100", list_price=Decimal("30000000"),
        description="外観・寸法検査を自動化する標準ライン検査装置。",
    ),
    dict(
        name="検査装置 オプション: 高速搬送ユニット", sku="INSP-OPT-CONV",
        list_price=Decimal("8000000"), description="タクトタイム短縮用の高速搬送ユニット。",
    ),
    dict(
        name="導入設置・調整サービス", sku="SVC-INSTALL", list_price=Decimal("3000000"),
        description="現地設置・ライン調整・立ち上げ支援(1式)。",
    ),
    dict(
        name="保守サポート(年間)", sku="SVC-SUPPORT-1Y", list_price=Decimal("1000000"),
        description="年間保守契約(定期点検・障害対応)。",
    ),
    dict(
        name="予備部品パッケージ", sku="PART-SPARE-KIT", list_price=Decimal("500000"),
        description="消耗部品・予備品一式。",
    ),
]


def wipe_tenant_pricing_data(session: Session) -> None:
    tables = [
        "contract_line_item", "quote_line_item", "contract", "quote",
        "engagement_line_item", "product",
    ]
    for t in tables:
        session.execute(
            text(f"DELETE FROM {t} WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)}
        )


def seed_products(session: Session) -> dict[str, Product]:
    products = {}
    for spec in PRODUCTS:
        product = Product(tenant_id=TENANT_ID, **spec)
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
        help="投入先テナントID(既存の商品/明細/見積/契約はこのテナント分のみ全削除される)",
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

        products = seed_products(session)
        session.commit()

        wire_demo_quote_and_contract(session, products)
        session.commit()

    print(f"Seeded demo product price list for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
