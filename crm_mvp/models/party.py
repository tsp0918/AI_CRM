"""取引先・担当者・コンプライアンス状態。

コンプライアンスは「判定」を持たない。外部（AI_TM / 反社チェック / 与信）が返した
結論と有効期限、証跡の参照だけを保持する = data-light。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, TenantScoped, Timestamped, UUIDPk
from ..enums import ComplianceCheckType, ComplianceOutcome


class Account(Base, UUIDPk, Timestamped, TenantScoped):
    __tablename__ = "account"

    name: Mapped[str] = mapped_column(String(255))
    # 法人番号（国税庁）を名寄せのアンカーにする。海外法人は None。
    corporate_number: Mapped[str | None] = mapped_column(String(13), index=True)
    country: Mapped[str | None] = mapped_column(String(2))
    industry_template: Mapped[str | None] = mapped_column(String(64))

    # Companion 配置: 既存 CRM 側の ID。ここが system of record の境界。
    external_system: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(128))

    contacts: Mapped[list[Contact]] = relationship(back_populates="account")
    compliance: Mapped[list[ComplianceStatus]] = relationship(
        back_populates="account"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "external_system", "external_id",
                         name="uq_account_external"),
    )


class Contact(Base, UUIDPk, Timestamped, TenantScoped):
    __tablename__ = "contact"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(120))
    org_unit: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    left_company_at: Mapped[date | None] = mapped_column(Date)

    account: Mapped[Account] = relationship(back_populates="contacts")


class ComplianceStatus(Base, UUIDPk, Timestamped, TenantScoped):
    """外部スクリーニングの結論のみを保持する。記事本文等は一切コピーしない。"""

    __tablename__ = "compliance_status"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    check_type: Mapped[ComplianceCheckType] = mapped_column(String(32))
    outcome: Mapped[ComplianceOutcome] = mapped_column(
        String(16), default=ComplianceOutcome.UNKNOWN
    )

    provider: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    evidence_uri: Mapped[str | None] = mapped_column(String(512))
    evidence_hash: Mapped[str | None] = mapped_column(String(64))

    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)

    account: Mapped[Account] = relationship(back_populates="compliance")

    __table_args__ = (
        UniqueConstraint("account_id", "check_type", name="uq_compliance_current"),
        Index("ix_compliance_expiry", "valid_until"),
    )

    @property
    def is_fresh(self) -> bool:
        if self.valid_until is None:
            return False
        return self.valid_until > datetime.now(self.valid_until.tzinfo)
