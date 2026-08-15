"""取引先(Account)の一括再スクリーニングコマンド(CRM_連携引き継ぎ書.md IF-02, C2-10)。

  python scripts/batch_screening.py --tenant-id <uuid>

`scripts/daily_snapshot.py`/`scripts/process_outbox.py`と同じ運用パターン
(1実行=1テナント、`--tenant-id`必須)。テナント内の全Accountについて、
既定チェック種別(SANCTIONS/ANTI_SOCIAL)で期限切れ・未実施のものだけを
再スクリーニングする(`ensure_account_screened`はfreshならスキップする)。

AI_TM側の実スクリーニングAPIが未接続の間は`MockScreeningAdapter`
(常にCLEAR)が使われる — 本番投入時は`AITMScreeningAdapter`に差し替える。
"""

from __future__ import annotations

import argparse
import os
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from crm_mvp.models import Account
from crm_mvp.services.compliance_screening import ensure_account_screened

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
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
        accounts = session.execute(
            select(Account).where(Account.tenant_id == args.tenant_id)
        ).scalars().all()

        for account in accounts:
            ensure_account_screened(session, args.tenant_id, account)
        session.commit()

    print(f"batch_screening: {len(accounts)}件のAccountを確認 (tenant_id={args.tenant_id})")


if __name__ == "__main__":
    main()
