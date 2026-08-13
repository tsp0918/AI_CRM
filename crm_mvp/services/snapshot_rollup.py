"""§7.6: PipelineSnapshot の保持期間・ロールアップ。

決定(2026-08-13、ユーザー判断): 日次90日 → 週次1年 → 月次永年。

「ロールアップ」は集計ではなく間引きとして実装する。stage や
evidence_score は時点の状態を表す値であり合算できないため、各期間の
最後の1件(その週・その月で最も新しい日付のスナップショット)を代表値
として残し、それ以外を削除する。繰り返し実行しても安全(各グループが
既に1件なら何もしない)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PipelineSnapshot

DAILY_RETENTION_DAYS = 90
WEEKLY_RETENTION_DAYS = 365


@dataclass(slots=True)
class RollupOutcome:
    daily_rows_collapsed: int    # 週次への間引きで削除された日次行数
    weekly_rows_collapsed: int   # 月次への間引きで削除された週次(相当)行数


def _week_key(d: date) -> tuple[int, int]:
    iso_year, iso_week, _ = d.isocalendar()
    return (iso_year, iso_week)


def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def rollup_snapshots(
    session: Session, tenant_id: uuid.UUID, today: date | None = None,
) -> RollupOutcome:
    today = today or date.today()
    weekly_cutoff = today - timedelta(days=DAILY_RETENTION_DAYS)
    monthly_cutoff = today - timedelta(days=WEEKLY_RETENTION_DAYS)

    daily_collapsed = _collapse(session, tenant_id, weekly_cutoff, _week_key)
    weekly_collapsed = _collapse(session, tenant_id, monthly_cutoff, _month_key)

    return RollupOutcome(
        daily_rows_collapsed=daily_collapsed,
        weekly_rows_collapsed=weekly_collapsed,
    )


def _collapse(
    session: Session, tenant_id: uuid.UUID, upper_bound: date, key_fn,
) -> int:
    rows = session.execute(
        select(PipelineSnapshot).where(
            PipelineSnapshot.tenant_id == tenant_id,
            PipelineSnapshot.snapshot_date < upper_bound,
        ).order_by(PipelineSnapshot.engagement_id, PipelineSnapshot.snapshot_date)
    ).scalars().all()

    groups: dict[tuple, list[PipelineSnapshot]] = {}
    for row in rows:
        key = (row.engagement_id, key_fn(row.snapshot_date))
        groups.setdefault(key, []).append(row)

    deleted = 0
    for group_rows in groups.values():
        if len(group_rows) <= 1:
            continue
        # 最後(最新)の1件だけを残す。
        for row in group_rows[:-1]:
            session.delete(row)
            deleted += 1

    session.flush()
    return deleted
