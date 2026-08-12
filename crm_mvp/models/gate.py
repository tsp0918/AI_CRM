"""ゲート。入口を緩く、出口を固く。

強度を4段階で持つことで「ライトに導入」と「ちゃんと止める」を両立させる。
ポリシーはコードではなくデータとして持ち、バージョンを Evaluation に残す。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Provenance, TenantScoped, Timestamped, UUIDPk
from ..enums import ArtifactType, GateKind, GateStrength, Stage


class GatePolicy(Base, UUIDPk, Timestamped, TenantScoped):
    """製品側が定義する標準ポリシー。顧客は差分パラメータのみ設定する。"""

    __tablename__ = "gate_policy"

    code: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(default=1)
    industry_template: Mapped[str | None] = mapped_column(String(64))

    kind: Mapped[GateKind] = mapped_column(String(16))
    strength: Mapped[GateStrength] = mapped_column(
        String(24), default=GateStrength.WARN
    )
    # STAGE の場合の適用対象
    to_stage: Mapped[Stage | None] = mapped_column(String(24))
    # ARTIFACT の場合の適用対象
    artifact_type: Mapped[ArtifactType | None] = mapped_column(String(24))

    # 条件の宣言。エンジンが解釈する。
    #   {"slots": [{"criterion": "economic_buyer", "min_confidence": "verified"}],
    #    "graph": [{"rule": "path_to_decider", "min_confidence": "verified"},
    #              {"rule": "min_engaged_contacts", "value": 2}],
    #    "compliance": [{"check_type": "anti_social", "must_be_fresh": true}]}
    conditions: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class GateEvaluation(Base, UUIDPk, TenantScoped):
    """評価結果のスナップショット。遷移時に必ず1件残す。"""

    __tablename__ = "gate_evaluation"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gate_policy.id"))
    policy_version: Mapped[int] = mapped_column()

    satisfied: Mapped[bool] = mapped_column(Boolean, default=False)
    # 未充足の条件と、それを埋めるための一手
    missing: Mapped[list] = mapped_column(JSONB, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Waiver(Base, UUIDPk, Provenance, TenantScoped):
    """例外承認。コンプライアンス証跡であり、最良のコーチング材料でもある。"""

    __tablename__ = "waiver"

    engagement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gate_policy.id"))
    approved_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
