"""PipelineSnapshot 日次バッチ(HANDOVER.md §5 Phase4, item18)。

§7.5(2026-08-13 解消): evidence_score の算出式は confidence_score.py の
客観的クロージング確度スコア(証拠強度・稟議到達度・鮮度・単一窓口係数)を
正式採用する。「ステージ確率ではなく証拠強度の分布から算出する」という
当初の方針をそのまま満たし、フォーキャストリスクレーダー(§6)が
スナップショット間の推移を追う際の値としてもこのスコアをそのまま使う。
PipelineSnapshot の既存スケール(0-1)に合わせて 100 で正規化して保持する。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Stage
from ..models import Engagement, PipelineSnapshot
from .confidence_score import compute_confidence_score
from .stage_transitions import load_gate_context

OPEN_STAGES: list[Stage] = [
    Stage.LEAD, Stage.PROSPECT, Stage.QUALIFIED, Stage.PROPOSAL,
    Stage.NEGOTIATION,
]


def compute_evidence_score(
    slots: dict, nodes: dict, edges: list, roles: dict,
    now: datetime | None = None,
) -> float:
    return compute_confidence_score(slots, nodes, edges, roles, now=now).total / 100.0


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

        ctx = load_gate_context(session, tenant_id, engagement)
        session.add(PipelineSnapshot(
            tenant_id=tenant_id, snapshot_date=snapshot_date,
            engagement_id=engagement.id, stage=engagement.stage,
            amount=engagement.amount,
            evidence_score=compute_evidence_score(
                ctx["slots"], ctx["nodes"], ctx["edges"], ctx["roles"],
            ),
            expected_close_date=engagement.expected_close_date,
        ))
        created += 1

    session.flush()
    return created
