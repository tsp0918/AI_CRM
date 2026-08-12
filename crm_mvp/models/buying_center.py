"""バイヤー相関図。

二層構造:
  GraphNode / GraphEdge  … 取引先単位。案件を跨いで再利用される（組織・稟議ルート）
  EngagementRole         … 商談単位のオーバーレイ（役割・態度・接触状況）

同じ人物でも案件が変われば役割は変わるが、組織階層は変わらない。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import AccessLevel, BuyingCenterRole, Confidence, EdgeType, Stance


class GraphNode(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    """人物ノード。氏名不明でも立てられる（プレースホルダー）。

    「経理部門に決裁者がいるはずだが誰か分からない」を可視化することが、
    相関図の最大の役割。空白は行動を生まないが、灰色のノードは行動を生む。
    """

    __tablename__ = "graph_node"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contact.id", ondelete="SET NULL")
    )
    # contact_id が None のときに使う仮ラベル（例: "経理部門の決裁者(氏名不明)"）
    placeholder_label: Mapped[str | None] = mapped_column(String(120))
    org_unit: Mapped[str | None] = mapped_column(String(120))
    # 階層レイアウトの縦軸。0=現場, 1=課長, 2=部長, 3=役員, 4=社長
    seniority_layer: Mapped[int] = mapped_column(SmallInteger, default=0)

    contact: Mapped["Contact | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            "contact_id IS NOT NULL OR placeholder_label IS NOT NULL",
            name="ck_node_identifiable",
        ),
        Index("ix_node_account_layer", "account_id", "seniority_layer"),
    )


class GraphEdge(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    """関係エッジ。型を絞らないとスパゲッティになるため 4 種のみ。"""

    __tablename__ = "graph_edge"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_node.id", ondelete="CASCADE"), index=True
    )
    to_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_node.id", ondelete="CASCADE"), index=True
    )
    edge_type: Mapped[EdgeType] = mapped_column(String(24))
    # approves の順序。稟議ルートは組織階層と一致しないため独立に持つ。
    sequence: Mapped[int | None] = mapped_column(SmallInteger)
    strength: Mapped[int] = mapped_column(SmallInteger, default=3)  # 1-5

    confidence: Mapped[Confidence] = mapped_column(
        String(16), default=Confidence.ASSERTED
    )
    evidence_source_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    evidence_quote: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("from_node_id", "to_node_id", "edge_type",
                         name="uq_edge_unique"),
        CheckConstraint("from_node_id <> to_node_id", name="ck_edge_no_self"),
        CheckConstraint("strength BETWEEN 1 AND 5", name="ck_edge_strength"),
    )


class EngagementRole(Base, UUIDPk, Timestamped, Provenance, TenantScoped):
    """商談オーバーレイ。役割と態度は直交する概念として別カラムで持つ。"""

    __tablename__ = "engagement_role"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_node.id", ondelete="CASCADE"), index=True
    )

    roles: Mapped[list[str]] = mapped_column(ARRAY(String(24)), default=list)
    stance: Mapped[Stance] = mapped_column(String(16), default=Stance.UNKNOWN)
    influence: Mapped[int] = mapped_column(SmallInteger, default=3)  # ノードサイズ
    access_level: Mapped[AccessLevel] = mapped_column(
        String(16), default=AccessLevel.NONE
    )
    confidence: Mapped[Confidence] = mapped_column(
        String(16), default=Confidence.ASSERTED
    )
    last_touched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    engagement: Mapped["Engagement"] = relationship(back_populates="roles")

    __table_args__ = (
        UniqueConstraint("engagement_id", "node_id", name="uq_role_per_deal"),
        CheckConstraint("influence BETWEEN 1 AND 5", name="ck_role_influence"),
    )

    def has_role(self, role: BuyingCenterRole) -> bool:
        return role.value in (self.roles or [])


from .engagement import Engagement  # noqa: E402
from .party import Contact  # noqa: E402
