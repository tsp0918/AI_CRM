"""売上レポート・ABM機能のデモ用組織構造投入。

  python scripts/seed_org_structure.py --tenant-id <uuid>

先に scripts/seed_demo_pipeline.py と scripts/seed_demo_2yr_backfill.py を
実行しておくこと(取引先・商談データが必要)。

投入内容:
  - セールスグループ(階層): 海外営業本部 > 東アジア営業課 / 北米・欧州営業課
    既存の全商談に、取引先の国コードに応じて割り当てる
  - "NSC Group" という親取引先を新設し、既存の NSC Taiwan/Korea/Singapore/
    USA の4アカウントをその下にロールアップする(法人グループ単位の
    売上ロールアップ・ディレクトリ構造のデモ)
  - ABMデモ用に、まだAccountを持たない新規リードを1件、NSC Groupに
    手動で紐付ける(「既知の戦略ファミリーからの新規引き合い」を再現)

このスクリプトは既存データを削除せず、組織構造の割り当てのみを行う。
"""

from __future__ import annotations

import argparse
import os
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from crm_mvp.models import Account, Engagement, Lead
from crm_mvp.services.account_hierarchy import (
    create_grouping_account, set_parent_account,
)
from crm_mvp.services.sales_groups import create_sales_group

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

TENANT_ID = DEFAULT_TENANT_ID

# 取引先名 -> 国コード(erp_business_partner.country と同じ値をここでも
# 明示しておく。Accountに country が入っていない場合のフォールバック)。
EAST_ASIA_COUNTRIES = {"TW", "KR", "SG", "CN", "JP"}

NSC_ACCOUNT_NAMES = [
    "NSC Taiwan Materials Co., Ltd.",
    "NSC Korea Advanced Materials Ltd.",
    "NSC Singapore Pte. Ltd.",
    "NSC Electronic Materials USA, Inc.",
]


def wipe_org_structure(session: Session) -> None:
    """組織構造の投入だけをやり直せるように、セールスグループと
    NSC Group 親アカウントのみ削除する(商談・取引先本体は消さない)。"""
    session.execute(
        text(
            "UPDATE engagement SET sales_group_id = NULL "
            "WHERE tenant_id = :tid"
        ),
        {"tid": str(TENANT_ID)},
    )
    session.execute(
        text(
            "UPDATE account SET parent_account_id = NULL "
            "WHERE tenant_id = :tid AND parent_account_id IS NOT NULL"
        ),
        {"tid": str(TENANT_ID)},
    )
    session.execute(
        text("DELETE FROM lead WHERE tenant_id = :tid AND company_name = :name"),
        {"tid": str(TENANT_ID), "name": "NSC Vietnam Materials Co., Ltd."},
    )
    session.execute(
        text("DELETE FROM account WHERE tenant_id = :tid AND name = 'NSC Group'"),
        {"tid": str(TENANT_ID)},
    )
    session.execute(text("DELETE FROM sales_group WHERE tenant_id = :tid"), {"tid": str(TENANT_ID)})


def seed_sales_groups(session: Session) -> dict[str, uuid.UUID]:
    hq = create_sales_group(session, TENANT_ID, name="海外営業本部")
    east_asia = create_sales_group(
        session, TENANT_ID, name="東アジア営業課", parent_group_id=hq.id,
    )
    americas_europe = create_sales_group(
        session, TENANT_ID, name="北米・欧州営業課", parent_group_id=hq.id,
    )
    session.flush()
    return {"east_asia": east_asia.id, "americas_europe": americas_europe.id}


def assign_sales_groups(session: Session, group_ids: dict[str, uuid.UUID]) -> int:
    accounts = session.execute(
        select(Account).where(Account.tenant_id == TENANT_ID)
    ).scalars().all()
    account_country = {a.id: a.country for a in accounts}

    engagements = session.execute(
        select(Engagement).where(Engagement.tenant_id == TENANT_ID)
    ).scalars().all()

    count = 0
    for e in engagements:
        country = account_country.get(e.account_id)
        if country in EAST_ASIA_COUNTRIES:
            e.sales_group_id = group_ids["east_asia"]
        else:
            e.sales_group_id = group_ids["americas_europe"]
        count += 1
    session.flush()
    return count


def rollup_nsc_group(session: Session) -> None:
    nsc_group = create_grouping_account(session, TENANT_ID, name="NSC Group")
    session.flush()

    for name in NSC_ACCOUNT_NAMES:
        account = session.execute(
            select(Account).where(Account.tenant_id == TENANT_ID, Account.name == name)
        ).scalar_one_or_none()
        if account is None:
            print(f"  (スキップ: 取引先「{name}」が見つかりません — 先にERP取込/商談投入を実行済みか確認してください)")
            continue
        set_parent_account(session, TENANT_ID, account, nsc_group.id)
        print(f"  {name} を NSC Group の傘下に設定しました。")


def seed_abm_lead(session: Session) -> None:
    nsc_group = session.execute(
        select(Account).where(Account.tenant_id == TENANT_ID, Account.name == "NSC Group")
    ).scalar_one_or_none()
    if nsc_group is None:
        return

    lead = Lead(
        tenant_id=TENANT_ID, company_name="NSC Vietnam Materials Co., Ltd.",
        full_name="Nguyen Van Minh", title="Procurement Manager",
        matched_account_id=nsc_group.id, written_by="human:demo",
    )
    session.add(lead)
    session.flush()
    print(
        "  ABMデモ用リード「NSC Vietnam Materials Co., Ltd.」を NSC Group に"
        "紐付けて作成しました(既存ファミリーからの新規引き合いの想定)。"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=DEFAULT_TENANT_ID)
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
        wipe_org_structure(session)
        session.commit()

        group_ids = seed_sales_groups(session)
        session.commit()

        n = assign_sales_groups(session, group_ids)
        session.commit()

        rollup_nsc_group(session)
        session.commit()

        seed_abm_lead(session)
        session.commit()

    print(f"Assigned sales groups to {n} engagements and rolled up NSC accounts for tenant {TENANT_ID}.")


if __name__ == "__main__":
    main()
