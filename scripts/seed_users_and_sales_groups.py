"""社内ユーザー名簿の取り込みと、実組織に対応したセールスグループへの
再編・商談の再割当(user_matrix_CRM.csv、2026-08-13)。

  python scripts/seed_users_and_sales_groups.py --tenant-id <uuid>

これまでの scripts/seed_org_structure.py(地域別)・
scripts/seed_demo_material_deals.py(商品ライン別)で作った仮のセールス
グループは、この実組織データに置き換える。SalesGroupは Function を
頂点に、Sales配下は実際のマネージャー体制(Sales ManagerがBDM/KAM/AM
を統括、Customer Success ManagerがCSチームを統括)に対応させた4チーム
に分ける:

  Board
  Marketing
  Inside Sales
  Sales
    ├─ 新規開拓(BDM)
    ├─ 大手顧客(KAM)
    ├─ 既存顧客(AM)
    └─ カスタマーサクセス(CS)

商談の再割当ルール(2026-08-14 ユーザー訂正 — 役割の実態に合わせた):
  - BDM = 新規ロゴ獲得担当 → relationship_type が無い(新規)商談はすべて
    新規開拓(BDM)。ステージや金額の大小では判定しない。
  - AM = 既存顧客担当の営業 → Upsell/Cross-sell(既存顧客への追加販売)
    はすべて既存顧客(AM)。
  - CS = 既存顧客の契約更新管理 → Renewal(契約更新)はすべて
    カスタマーサクセス(CS)。
  大手顧客(KAM)は組織・ユーザーとしては残すが、この自動割当ルールの
  対象外(今回は判定基準が無いため商談は自動では割り当てない)。
  Authorityは今回は承認ゲートには使わず、データとして保持するのみ。

先に scripts/seed_demo_pipeline.py・scripts/seed_demo_2yr_backfill.py・
scripts/seed_demo_material_deals.py を実行しておくこと(商談データが必要)。

§7.4: crm_app ロール(非 superuser)で接続し、RLS のテナント文脈を
自分で SET してから実行する。
"""

from __future__ import annotations

import argparse
import csv
import itertools
import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from crm_mvp.enums import AuthorityLevel
from crm_mvp.models import Engagement, SalesGroup, User
from crm_mvp.services.sales_groups import create_sales_group
from crm_mvp.services.users import upsert_user

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DEFAULT_CSV_PATH = Path(__file__).parent / "data" / "user_matrix_CRM.csv"

TENANT_ID = DEFAULT_TENANT_ID

# CSVの Function + Role から、どのセールスグループに属するかへのマッピング。
# (Function, Role) -> グループキー。キーは seed_sales_group_hierarchy が
# 返す辞書のキーと対応する。
ROLE_TO_GROUP_KEY = {
    ("Board", "Managing Director"): "board",
    ("Marketing", "Director"): "marketing",
    ("Marketing", "Field Marketing"): "marketing",
    ("Inside Sales", "Director"): "inside_sales",
    ("Inside Sales", "SDR"): "inside_sales",
    ("Inside Sales", "BDR"): "inside_sales",
    ("Sales", "Director"): "sales",
    ("Sales", "Sales Manager"): "sales",
    ("Sales", "BDM"): "new_business",
    ("Sales", "KAM"): "key_accounts",
    ("Sales", "AM"): "account_management",
    ("Sales", "Customer Success Manager"): "customer_success",
    ("Sales", "CS"): "customer_success",
    ("Sales", "RecOps"): "sales",
    ("Sales", "RevOps"): "sales",
}

AUTHORITY_MAP = {
    "": AuthorityLevel.NONE,
    "Approver": AuthorityLevel.APPROVER,
    "Approver-High": AuthorityLevel.APPROVER_HIGH,
    "Approver-Super": AuthorityLevel.APPROVER_SUPER,
}


def wipe_previous_org_assignment(session: Session) -> None:
    """以前の(地域別・商品ライン別)セールスグループとユーザー名簿を
    削除し、商談側の紐付けもリセットする。取引先(NSC Groupロールアップ等)
    は削除しない。"""
    session.execute(
        text("UPDATE engagement SET sales_group_id = NULL, owner_user_id = NULL WHERE tenant_id = :tid"),
        {"tid": str(TENANT_ID)},
    )
    session.execute(text("DELETE FROM app_user WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)})
    session.execute(text("DELETE FROM sales_group WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)})


def seed_sales_group_hierarchy(session: Session) -> dict[str, uuid.UUID]:
    board = create_sales_group(session, TENANT_ID, name="Board")
    marketing = create_sales_group(session, TENANT_ID, name="Marketing")
    inside_sales = create_sales_group(session, TENANT_ID, name="Inside Sales")
    sales = create_sales_group(session, TENANT_ID, name="Sales")
    new_business = create_sales_group(
        session, TENANT_ID, name="新規開拓(BDM)", parent_group_id=sales.id,
    )
    key_accounts = create_sales_group(
        session, TENANT_ID, name="大手顧客(KAM)", parent_group_id=sales.id,
    )
    account_management = create_sales_group(
        session, TENANT_ID, name="既存顧客(AM)", parent_group_id=sales.id,
    )
    customer_success = create_sales_group(
        session, TENANT_ID, name="カスタマーサクセス(CS)", parent_group_id=sales.id,
    )
    session.flush()
    return {
        "board": board.id, "marketing": marketing.id, "inside_sales": inside_sales.id,
        "sales": sales.id, "new_business": new_business.id, "key_accounts": key_accounts.id,
        "account_management": account_management.id, "customer_success": customer_success.id,
    }


def import_users(
    session: Session, csv_path: Path, group_ids: dict[str, uuid.UUID],
) -> dict[str, list[User]]:
    """CSVを取り込み、Function/Roleごとにグループ分けしたユーザーの
    辞書を返す(再割当のラウンドロビンに使う)。"""
    by_group_key: dict[str, list[User]] = {}
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            function = row["Function"].strip()
            role = row["Role"].strip()
            group_key = ROLE_TO_GROUP_KEY.get((function, role))
            if group_key is None:
                raise SystemExit(
                    f"ROLE_TO_GROUP_KEY に無い (Function, Role) があります: ({function}, {role})"
                )
            user = upsert_user(
                session, TENANT_ID,
                name=row["Name"].strip(), email=row["email"].strip(),
                function=function, role=role,
                authority=AUTHORITY_MAP[row["Authority"].strip()],
                sales_group_id=group_ids[group_key],
            )
            by_group_key.setdefault(group_key, []).append(user)
    return by_group_key


def reassign_engagements(
    session: Session, group_ids: dict[str, uuid.UUID], users_by_group: dict[str, list[User]],
) -> int:
    bdm_cycle = itertools.cycle(users_by_group.get("new_business", [None]))
    am_cycle = itertools.cycle(users_by_group.get("account_management", [None]))
    cs_cycle = itertools.cycle(users_by_group.get("customer_success", [None]))

    engagements = session.execute(
        select(Engagement).where(Engagement.tenant_id == TENANT_ID)
        .order_by(Engagement.created_at)
    ).scalars().all()

    count = 0
    for e in engagements:
        # 2026-08-14 ユーザー訂正: BDMは新規ロゴ獲得、AMは既存顧客の
        # Upsell/Cross-sell、CSは既存顧客の契約更新管理を担当する。
        # 案件のステージや金額の大小ではなく、relationship_typeという
        # 「その商談の性質」だけで一意に決まる — KAM(大手顧客)は今回の
        # 自動割当ルールの対象外(既存ユーザー・グループとしては残すが、
        # 自動では商談が割り当たらない)。
        if e.relationship_type == "renewal":
            group_key, user = "customer_success", next(cs_cycle)
        elif e.relationship_type in ("upsell", "cross_sell"):
            group_key, user = "account_management", next(am_cycle)
        else:
            group_key, user = "new_business", next(bdm_cycle)

        e.sales_group_id = group_ids[group_key]
        e.owner_user_id = user.id if user else None
        e.owner = user.name if user else None
        count += 1
    session.flush()
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID)
    parser.add_argument("--csv-path", type=Path, default=DEFAULT_CSV_PATH)
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
        wipe_previous_org_assignment(session)
        session.commit()

        group_ids = seed_sales_group_hierarchy(session)
        session.commit()

        users_by_group = import_users(session, args.csv_path, group_ids)
        session.commit()

        n = reassign_engagements(session, group_ids, users_by_group)
        session.commit()

    total_users = sum(len(v) for v in users_by_group.values())
    print(
        f"Imported {total_users} users and reassigned {n} engagements to the "
        f"real org structure for tenant {TENANT_ID}."
    )


if __name__ == "__main__":
    main()
