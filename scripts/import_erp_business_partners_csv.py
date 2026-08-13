"""ERP取引先マスタCSVの取り込み。

  python scripts/import_erp_business_partners_csv.py --tenant-id <uuid>

デフォルトの CSV は scripts/data/erp_business_partners.csv — erp-system
から書き出された実際のエクスポート形状(bp_code, bp_type, name, country,
roles, email, phone, address_line1, address_line2, city, postal_code,
credit_limit, payment_terms, currency, is_denied_party, is_active 他)を
そのまま受け取る。ERP側の id/client_id/created_at/created_by/updated_at/
updated_by は、CRM側では取り込み時刻(imported_at)を別途持つため取り込まない
(scripts/import_erp_materials_csv.py と同じ方針)。

実行前に指定テナントの erp_business_partner を全削除する。

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

from crm_mvp.services.erp_business_partners import upsert_erp_business_partner

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DEFAULT_CSV_PATH = Path(__file__).parent / "data" / "erp_business_partners.csv"

TENANT_ID = DEFAULT_TENANT_ID


def wipe_erp_business_partners(session: Session) -> None:
    session.execute(
        text("DELETE FROM erp_business_partner WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)}
    )


def import_csv(session: Session, csv_path: Path) -> int:
    count = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            upsert_erp_business_partner(
                session, TENANT_ID,
                bp_code=row["bp_code"], name=row["name"].strip(),
                bp_type=row["bp_type"].strip() or "ORG",
                country=row["country"].strip() or None,
                roles=row["roles"].strip() or "CUSTOMER",
                email=row["email"].strip() or None,
                phone=row["phone"].strip() or None,
                address_line1=row["address_line1"].strip() or None,
                address_line2=row["address_line2"].strip() or None,
                city=row["city"].strip() or None,
                postal_code=row["postal_code"].strip() or None,
                credit_limit=Decimal(row["credit_limit"]) if row["credit_limit"].strip() else None,
                payment_terms=row["payment_terms"].strip() or None,
                currency=row["currency"].strip() or None,
                is_denied_party=row["is_denied_party"].strip() in ("1", "true", "True"),
                is_active=row["is_active"].strip() in ("1", "true", "True"),
            )
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID,
        help="投入先テナントID(既存のERP取引先はこのテナント分のみ全削除される)",
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
        wipe_erp_business_partners(session)
        session.commit()

        count = import_csv(session, args.csv_path)
        session.commit()

    print(f"Imported {count} ERP business partners from {args.csv_path} for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
