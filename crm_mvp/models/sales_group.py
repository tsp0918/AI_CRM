"""セールスグループ(CRM独自の営業組織軸、ProductGroupと同じ設計)。

このCRMには実ユーザーテーブルが無く、Engagement.owner は自由記述の
担当者名のみ(2026-08-13 ユーザー決定: 本格的なUser/Role基盤は作らない)。
SalesGroup はそれとは独立した、レポート集計用の軽量な組織タグ —
「誰が」ではなく「どの営業組織の実績か」を商談に紐付けるためだけに使う。
親子階層はProductGroupと同じ自己参照パターン、循環参照はサービス層
(services/sales_groups.py)でガードする。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk


class SalesGroup(Base, UUIDPk, Timestamped, TenantScoped):
    __tablename__ = "sales_group"

    name: Mapped[str] = mapped_column(String(120))
    parent_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales_group.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[str | None] = mapped_column(Text)
