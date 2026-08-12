"""SQLAlchemy 2.0 の宣言的ベースと共通ミックスイン。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Provenance:
    """由来。全ての「判断に影響する」レコードはこれを持つ。

    written_by は 'human:<user_id>' / 'ai:<model>' / 'sync:<system>' の形式。
    AI が書いたレコードを後から機械的に取り消せることが要件。
    """

    written_by: Mapped[str] = mapped_column(default="human:unknown")
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )


class TenantScoped:
    """全テーブル共通のテナント識別子。

    分離方式（Row-Level Security / スキーマ分離 / DB分離）は
    HANDOVER.md §7.4 で未決。現時点ではカラムを持たせてアプリケーション層の
    フィルタリングを可能にするだけの最小実装であり、DB レベルの隔離は
    保証しない。方式が決まり次第、この上に RLS ポリシー等を追加する。
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
