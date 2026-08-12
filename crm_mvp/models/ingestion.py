"""取り込みと提案。

AI-native 設計の中核。AI は業務テーブルに直接書き込まない。
必ず ExtractionProposal を経由させることで以下が同時に成立する:

  1. 監査可能性  … 誰が(どのモデルが)何を根拠にその値を書いたか追跡できる
  2. 取り消し可能性 … AI 由来の書き込みだけを機械的にロールバックできる
  3. 自己調整   … フィールド別の承認率を実測でき、自動適用の範囲を
                   人の勘ではなくデータで決められる
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, TenantScoped, Timestamped, UUIDPk
from ..enums import AutonomyMode, ProposalStatus, SourceKind


class IngestionSource(Base, UUIDPk, Timestamped, TenantScoped):
    """入力の唯一の受け口。担当者はここに投げるだけでよい。

    「どのフィールドに入れればいいか分からない」への回答は
    「入れる場所は最初から1つしかない」であるべき。
    """

    __tablename__ = "ingestion_source"

    engagement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[SourceKind] = mapped_column(String(24))

    # 録画・録音は外部ストレージ参照のみ。本文はテナント設定次第で保持しない。
    uri: Mapped[str | None] = mapped_column(String(512))
    raw_text: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[int | None] = mapped_column(Integer)

    # 話者と CRM 上の Contact / GraphNode の対応付け結果
    participants: Mapped[list] = mapped_column(JSONB, default=list)

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)
    # 抽出に使ったモデルとプロンプト版。再現性のために必須。
    extractor_version: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_source_unprocessed", "processed_at"),
    )


class ExtractionProposal(Base, UUIDPk, Timestamped, TenantScoped):
    """AI が提案する単一の書き込み。承認されて初めて業務テーブルに反映される。"""

    __tablename__ = "extraction_proposal"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_source.id", ondelete="CASCADE"), index=True
    )
    engagement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("engagement.id", ondelete="CASCADE"), index=True
    )

    # 書き込み先。'qualification_slot' / 'graph_node' / 'graph_edge'
    # / 'engagement_role' / 'engagement'
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    # 例: 'criterion:economic_buyer' / 'stance' / 'expected_close_date'
    field_path: Mapped[str] = mapped_column(String(96))

    current_value: Mapped[dict | None] = mapped_column(JSONB)
    proposed_value: Mapped[dict] = mapped_column(JSONB)

    # モデルの自己申告確信度（0-1）。証拠強度(Confidence)とは別概念。
    model_score: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    evidence_offset: Mapped[list | None] = mapped_column(JSONB)  # [start, end]

    status: Mapped[ProposalStatus] = mapped_column(
        String(16), default=ProposalStatus.PENDING, index=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 却下時に人が入れた正しい値。次の精度改善の教師データになる。
    corrected_value: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_proposal_pending", "engagement_id", "status"),
    )


class FieldAutonomyPolicy(Base, UUIDPk, Timestamped, TenantScoped):
    """フィールド単位の自動化度。人が割合を決めるのではなく、実測で決まる。

    accept_rate が閾値を超え、かつサンプル数が足りていれば自動適用に昇格。
    下回れば自動的に確認モードへ降格する。
    """

    __tablename__ = "field_autonomy_policy"

    target_type: Mapped[str] = mapped_column(String(32))
    field_path: Mapped[str] = mapped_column(String(96))

    mode: Mapped[AutonomyMode] = mapped_column(
        String(24), default=AutonomyMode.AUTO_IF_TRUSTED
    )
    auto_apply_threshold: Mapped[float] = mapped_column(Float, default=0.90)
    min_samples: Mapped[int] = mapped_column(Integer, default=30)
    min_model_score: Mapped[float] = mapped_column(Float, default=0.75)

    # 直近ウィンドウの実測値（バッチで更新）
    rolling_accept_rate: Mapped[float] = mapped_column(Float, default=0.0)
    rolling_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    last_recomputed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "target_type", "field_path",
                         name="uq_autonomy_field"),
    )

    def should_auto_apply(self, model_score: float) -> bool:
        # 値による比較(==)を使うこと。DB から再読込した mode は
        # プレーンな str になり、enum シングルトンとの `is` 比較は
        # StrEnum であっても常に False になって自動適用が壊れて黙って
        # 確認待ちに落ちる(seed_policies で投入したポリシーが効かない)。
        if self.mode == AutonomyMode.NEVER_AI:
            return False
        if self.mode == AutonomyMode.ALWAYS_CONFIRM:
            return False
        if self.mode == AutonomyMode.ALWAYS_AUTO:
            return True
        return (
            model_score >= self.min_model_score
            and self.rolling_sample_size >= self.min_samples
            and self.rolling_accept_rate >= self.auto_apply_threshold
        )
