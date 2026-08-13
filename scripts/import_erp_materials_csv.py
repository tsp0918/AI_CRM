"""ERP品目マスタCSVの取り込み(ダミーデータをより現実的なサンプルに置換)。

  python scripts/import_erp_materials_csv.py --tenant-id <uuid>

デフォルトの CSV は scripts/data/erp_product_list.csv — erp-system から
書き出された実際のエクスポート形状(material_code, description,
material_type, base_unit, standard_price, currency, hs_code, eccn,
fefta_judgment, country_of_origin, weight_kg, last_compliance_check_at,
is_active, created_at, updated_at)をそのまま受け取る。

CRM側の ErpMaterial は「CRM参照向けフィールド」(docs/erp_crm_spec.md §6)
だけを保持する設計のため、weight_kg / last_compliance_check_at / ERP側の
created_at・updated_at は意図的に取り込まない(CRM側の created_at/
updated_at・imported_at は取り込み時刻を別途記録する)。

実行前に指定テナントの erp_material / product / product_group を全削除
する(2026-08-13 ユーザー指示: 「これまでの製品でもデータは一度削除して」)。
EngagementLineItem / Quote / Contract は product_name_snapshot 等に
凍結済みのため、参照先の Product が消えても表示は壊れない
(product_id は ondelete=SET NULL)。

§7.4: crm_app ロール(非 superuser)で接続し、RLS のテナント文脈を
自分で SET してから実行する。
"""

from __future__ import annotations

import argparse
import csv
import os
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crm_mvp.services.erp_materials import upsert_erp_material

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DEFAULT_CSV_PATH = Path(__file__).parent / "data" / "erp_product_list.csv"

TENANT_ID = DEFAULT_TENANT_ID


def wipe_product_master_data(session: Session) -> None:
    for table in ["product", "product_group", "erp_material"]:
        session.execute(
            text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)}
        )


def import_csv(session: Session, csv_path: Path) -> int:
    count = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            upsert_erp_material(
                session, TENANT_ID,
                material_code=row["material_code"],
                description=row["description"].strip(),
                material_type=row["material_type"].strip(),
                base_unit=row["base_unit"].strip(),
                standard_price=Decimal(row["standard_price"]),
                currency=row["currency"].strip(),
                hs_code=row["hs_code"].strip() or None,
                eccn=row["eccn"].strip() or None,
                fefta_judgment=row["fefta_judgment"].strip() or "UNKNOWN",
                country_of_origin=row["country_of_origin"].strip() or None,
                is_active=row["is_active"].strip().lower() == "true",
            )
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存の品目/商品グループ/商品はこのテナント分のみ全削除される)",
    )
    parser.add_argument(
        "--csv-path", type=Path, default=DEFAULT_CSV_PATH,
        help=f"取り込むCSVファイルのパス(既定: {DEFAULT_CSV_PATH})",
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
        wipe_product_master_data(session)
        session.commit()

        count = import_csv(session, args.csv_path)
        session.commit()

    print(
        f"Imported {count} ERP materials from {args.csv_path} for tenant {TENANT_ID}. "
        "商品グループ・CRM販売価格(Product)は削除済みです — /ui/products から必要な品目に"
        "価格設定してください。"
    )


if __name__ == "__main__":
    main()
