"""1on1で使う週次レビュー(2026-08-14)。

商談ごとに「週の月曜日」をキーに1行だけ持つ(追記型ではなく、その週の
内容をその場で編集する)。担当者コメント・マネージャーコメントを別カラム
で持ち、マネージャーコメントには簡易ステータス(順調/要注意/エスカレー
ション)を添えられる。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import ReviewStatus


class WeeklyReview(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    __tablename__ = "weekly_review"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    week_start_date: Mapped[date] = mapped_column(Date, index=True)

    rep_comment: Mapped[str | None] = mapped_column(Text)
    manager_comment: Mapped[str | None] = mapped_column(Text)
    manager_status: Mapped[ReviewStatus | None] = mapped_column(String(16))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "engagement_id", "week_start_date",
            name="uq_weekly_review_week",
        ),
    )
