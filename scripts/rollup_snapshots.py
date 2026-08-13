"""PipelineSnapshot 保持期間ロールアップの投入コマンド(HANDOVER.md §7.6)。

  python scripts/rollup_snapshots.py --tenant-id <uuid> [--date YYYY-MM-DD]

決定(2026-08-13): 日次90日 → 週次1年 → 月次永年。cron 等で週次実行する想定
(日次バッチほど頻繁に回す必要はない)。何度実行しても安全(冪等)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crm_mvp.services.snapshot_rollup import rollup_snapshots

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--date", type=dt.date.fromisoformat, default=None,
        help="省略時は実行日",
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
        outcome = rollup_snapshots(session, args.tenant_id, args.date)
        session.commit()

    print(
        f"pipeline_snapshot: 日次→週次 {outcome.daily_rows_collapsed} 件削除, "
        f"週次→月次 {outcome.weekly_rows_collapsed} 件削除 "
        f"(tenant_id={args.tenant_id})"
    )


if __name__ == "__main__":
    main()
