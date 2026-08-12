"""PipelineSnapshot 日次バッチの投入コマンド(HANDOVER.md §5 item18)。

  python scripts/daily_snapshot.py --tenant-id <uuid> [--date YYYY-MM-DD]

cron 等の外部スケジューラから日次で実行する想定(本コマンド自体はスケジューラを
含まない)。同日分は冪等にスキップされるため、リトライしても重複しない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from crm_mvp.services.snapshot import create_daily_snapshots

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost:5432/crm_mvp"


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
        created = create_daily_snapshots(session, args.tenant_id, args.date)
        session.commit()

    print(f"pipeline_snapshot: {created} 件作成 (tenant_id={args.tenant_id})")


if __name__ == "__main__":
    main()
