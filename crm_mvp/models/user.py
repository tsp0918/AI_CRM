"""社内ユーザーの名簿(user_matrix_CRM.csv、2026-08-13)。

これまでこのCRMには実ユーザーテーブルが無く、Engagement.owner は自由記述
の担当者名のみだった(ログイン認証の作り込みを避けるための意図的な判断)。
このUserは実際の組織図データ(Function/Role/Authority/Name/email)を
保持する名簿であり、認証基盤ではない — ログイン・パスワード等は持たない。
SalesGroup(営業組織)の実体を、この名簿の Function/Role に対応させる。

Authority(承認権限)は今回はデータとして保持するだけで、値引率承認等の
実際のゲート判定にはまだ使わない(将来の拡張として温存)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk
from ..enums import AuthorityLevel


class User(Base, UUIDPk, Timestamped, TenantScoped):
    __tablename__ = "app_user"

    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    function: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(64))
    authority: Mapped[str] = mapped_column(String(20), default=AuthorityLevel.NONE.value)
    sales_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales_group.id", ondelete="SET NULL"), index=True
    )
    is_active: Mapped[bool] = mapped_column(default=True)
