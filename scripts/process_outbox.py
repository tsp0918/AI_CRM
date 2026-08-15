"""Outbox送信保証キューの処理コマンド(CRM_連携引き継ぎ書.md §6.2)。

  python scripts/process_outbox.py --tenant-id <uuid> [--interval 30]

`scripts/daily_snapshot.py` と同じ運用パターン(1実行=1テナント、
`--tenant-id` 必須)。テナントレジストリが無いため、複数テナント運用時は
外部cron/schedulerがテナントごとに1行ずつ本コマンドを実行する想定。

§6.2: 実運用では30秒間隔のポーリングを推奨(`--interval 30`)。`--interval`を
省略すると1回処理して終了する(cron起動向け)。

Phase 0時点では `register_dispatcher()` が何も登録されていないため、
投入済みメッセージは全て `failed`(未登録kind)になる — Phase 1以降で
実送信ロジックを登録する。
"""

from __future__ import annotations

import argparse
import os
import time
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crm_mvp.services.outbox import process_outbox

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"


def _run_once(session: Session, tenant_id: uuid.UUID) -> dict:
    summary = process_outbox(session, tenant_id)
    session.commit()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument(
        "--interval", type=int, default=None,
        help="指定秒ごとにループ実行する(省略時は--onceと同じく1回で終了)",
    )
    args = parser.parse_args()

    engine = create_engine(args.database_url)

    while True:
        with Session(engine) as session:
            session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, false)"),
                {"tid": str(args.tenant_id)},
            )
            summary = _run_once(session, args.tenant_id)

        print(
            f"outbox: sent={summary['sent']} retried={summary['retried']} "
            f"dlq={summary['dlq']} failed_no_retry={summary['failed_no_retry']} "
            f"(tenant_id={args.tenant_id})"
        )

        if args.interval is None:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
