"""標準ゲートポリシー・初期自動化設定の投入コマンド。

  python scripts/seed_policies.py --tenant-id <uuid>

GatePolicy（製造業テンプレート）と FieldAutonomyPolicy（初期自動化設定）を
投入する。再実行しても同一レコードは重複投入しない（冪等）。

--tenant-id が必須なのは全テーブルが tenant_id 列を持つため。
テナント分離は Row-Level Security に確定済み（§7.4）。このスクリプトは
crm_app ロール（非 superuser）で接続し、対象テナントの session 変数を
自分で SET してから書き込む — RLS は superuser を素通りするため、
admin ロールで実行すると分離が検証されないまま通ってしまう。
"""

from __future__ import annotations

import argparse
import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crm_mvp.services.seed_policies import (
    upsert_default_autonomy, upsert_gate_policies,
)

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id", required=True, type=uuid.UUID,
        help="投入先テナント ID(GatePolicy / FieldAutonomyPolicy 共通)",
    )
    parser.add_argument(
        "--industry-template", default="manufacturing",
        help="GatePolicy に付与する業種テンプレート名（既定: manufacturing）",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    args = parser.parse_args()

    engine = create_engine(args.database_url)
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, false)"),
            {"tid": str(args.tenant_id)},
        )
        n_gates = upsert_gate_policies(
            session, tenant_id=args.tenant_id,
            industry_template=args.industry_template,
        )
        n_autonomy = upsert_default_autonomy(session, tenant_id=args.tenant_id)
        session.commit()

    print(
        f"gate_policy: {n_gates} 件投入 "
        f"(industry_template={args.industry_template})"
    )
    print(f"field_autonomy_policy: {n_autonomy} 件投入 (tenant_id={args.tenant_id})")


if __name__ == "__main__":
    main()
