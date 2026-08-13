"""フォーキャストリスクレーダー(HANDOVER.md ロードマップ §6)。

確度スコア(confidence_score.py)のスナップショット推移・鮮度・単一窓口
リスクから「予測未達になりうる案件」を機械的にランキングする。
マネージャー・経営層が1画面で次のアクションを判断できるようにする目的。

新しいトラッキングは追加しない。PipelineSnapshot(既に evidence_score として
確度スコアを日次で積んでいる)と confidence_score.py の再利用のみで完結する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Engagement, PipelineSnapshot
from .confidence_score import ConfidenceScore, compute_confidence_score
from .snapshot import OPEN_STAGES
from .stage_transitions import load_gate_context

CLOSE_DATE_WINDOW_DAYS = 30
LOW_SCORE_THRESHOLD = 60
CRITICAL_CRITERIA = frozenset({"economic_buyer", "budget"})
TOP_QUARTILE = 0.75


@dataclass(slots=True)
class RiskAssessment:
    engagement: Engagement
    score: ConfidenceScore
    reasons: list[str] = field(default_factory=list)

    @property
    def at_risk(self) -> bool:
        return bool(self.reasons)

    @property
    def weighted_amount(self):
        """担当者申告額の単純合計ではなく、確度スコアで重み付けした金額。"""
        if self.engagement.amount is None:
            return None
        return self.engagement.amount * (Decimal(self.score.total) / 100)


def _amount_top_quartile_threshold(engagements: list[Engagement]) -> float | None:
    amounts = sorted(float(e.amount) for e in engagements if e.amount is not None)
    if len(amounts) < 4:
        return None
    idx = min(int(len(amounts) * TOP_QUARTILE), len(amounts) - 1)
    return amounts[idx]


def _is_declining(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID,
) -> bool:
    rows = session.execute(
        select(PipelineSnapshot.evidence_score).where(
            PipelineSnapshot.tenant_id == tenant_id,
            PipelineSnapshot.engagement_id == engagement_id,
        ).order_by(PipelineSnapshot.snapshot_date.desc()).limit(2)
    ).scalars().all()
    if len(rows) < 2:
        return False
    latest, previous = rows[0], rows[1]
    return latest < previous


def _close_date(engagement: Engagement) -> date | None:
    return engagement.derived_close_date or engagement.expected_close_date


def assess_forecast_risk(
    session: Session, tenant_id: uuid.UUID, today: date | None = None,
) -> list[RiskAssessment]:
    """進行中の全案件を評価し、リスク要因が1つ以上ある案件を
    重み付き金額の降順で返す。"""
    today = today or date.today()

    engagements = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == tenant_id,
            Engagement.stage.in_(OPEN_STAGES),
        )
    ).scalars().all()
    amount_threshold = _amount_top_quartile_threshold(engagements)

    assessments: list[RiskAssessment] = []
    for engagement in engagements:
        ctx = load_gate_context(session, tenant_id, engagement)
        score = compute_confidence_score(
            ctx["slots"], ctx["nodes"], ctx["edges"], ctx["roles"],
        )
        reasons: list[str] = []

        if _is_declining(session, tenant_id, engagement.id):
            reasons.append("確度スコアが直近2回のスナップショットで低下傾向")

        close_date = _close_date(engagement)
        if (
            close_date is not None
            and close_date <= today + timedelta(days=CLOSE_DATE_WINDOW_DAYS)
            and score.total < LOW_SCORE_THRESHOLD
        ):
            reasons.append(
                f"クローズ想定({close_date})まで30日未満なのにスコアが{score.total}点"
            )

        if (
            score.single_threaded
            and engagement.amount is not None
            and amount_threshold is not None
            and float(engagement.amount) >= amount_threshold
        ):
            reasons.append("シングルスレッドかつ金額が上位25%")

        critical_decaying = [
            c.value for c, _ in score.decaying_soon if c.value in CRITICAL_CRITERIA
        ]
        if critical_decaying:
            reasons.append(f"重要項目({'、'.join(critical_decaying)})の証拠がまもなく失効")

        assessments.append(
            RiskAssessment(engagement=engagement, score=score, reasons=reasons)
        )

    at_risk = [a for a in assessments if a.at_risk]
    at_risk.sort(key=lambda a: (a.weighted_amount or 0), reverse=True)
    return at_risk
