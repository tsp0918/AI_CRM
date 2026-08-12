"""PipelineSnapshot 日次バッチ(HANDOVER.md §5 Phase4, item18)。

§7.5: evidence_score の算出式は未決。「ステージ確率ではなく証拠強度の
分布から算出する」という方針のみ決まっている。ここでは
「有効な(失効していない)QualificationSlot の証拠強度ランクの平均」を
暫定式として置く。正式な算出式が決まったら compute_evidence_score だけ
差し替えれば良いように、他のロジックとは分離してある。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import CONFIDENCE_ORDER, Confidence, Stage
from ..models import Engagement, PipelineSnapshot, QualificationSlot

_MAX_CONFIDENCE_RANK = max(CONFIDENCE_ORDER.values())

OPEN_STAGES: list[Stage] = [
    Stage.LEAD, Stage.PROSPECT, Stage.QUALIFIED, Stage.PROPOSAL,
    Stage.NEGOTIATION,
]


def compute_evidence_score(
    slots: list[QualificationSlot], now: datetime | None = None,
) -> float:
    """暫定式(§7.5 未決)。有効な slot が無ければ 0.0。"""
    valid = [s for s in slots if s.meets(Confidence.ASSERTED, now)]
    if not valid:
        return 0.0
    total = sum(CONFIDENCE_ORDER[s.confidence] for s in valid)
    return total / (len(valid) * _MAX_CONFIDENCE_RANK)


def create_daily_snapshots(
    session: Session, tenant_id: uuid.UUID, snapshot_date: date | None = None,
) -> int:
    """進行中(CLOSED_* でない)の Engagement について当日分の
    PipelineSnapshot を作成する。同日分が既にあればスキップする(冪等)。
    """
    snapshot_date = snapshot_date or date.today()

    engagements = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == tenant_id,
            Engagement.stage.in_(OPEN_STAGES),
        )
    ).scalars().all()

    created = 0
    for engagement in engagements:
        existing = session.execute(
            select(PipelineSnapshot).where(
                PipelineSnapshot.tenant_id == tenant_id,
                PipelineSnapshot.engagement_id == engagement.id,
                PipelineSnapshot.snapshot_date == snapshot_date,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        slots = session.execute(
            select(QualificationSlot).where(
                QualificationSlot.tenant_id == tenant_id,
                QualificationSlot.engagement_id == engagement.id,
            )
        ).scalars().all()

        session.add(PipelineSnapshot(
            tenant_id=tenant_id, snapshot_date=snapshot_date,
            engagement_id=engagement.id, stage=engagement.stage,
            amount=engagement.amount,
            evidence_score=compute_evidence_score(list(slots)),
            expected_close_date=engagement.expected_close_date,
        ))
        created += 1

    session.flush()
    return created
