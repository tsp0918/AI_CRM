"""経営レポートのスナップショット(2026-08-14)。

売上・キャンペーン効果・シーケンス効果・パイプラインの状態を、任意の
時点で1行に丸めて保存する。テナント×日付につき1行(同じ日に再保存
すると上書き)で、時系列に並べて振り返れるようにするための機能。

集計結果そのもの(labelとamount/countの組)をJSONBに固定化して持つ —
後から集計ロジックが変わっても、過去のスナップショットの見え方は
保存時点のまま変わらない(監査性を優先し、都度再計算はしない)。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk


class ReportSnapshot(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "report_snapshot"

    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    label: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("tenant_id", "snapshot_date", name="uq_report_snapshot_date"),
    )
