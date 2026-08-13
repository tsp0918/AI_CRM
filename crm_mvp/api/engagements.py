"""案件 API(HANDOVER.md §5 Phase3 item 11-14, Phase4 item 15,17)。"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..enums import GateStrength, Stage
from ..models import Engagement, Waiver
from ..services.gate_engine import GateResult
from ..services.graph_export import build_graph_dot, build_graph_json
from ..services.stage_transitions import apply_stage_transition, evaluate_stage_gate, next_stage
from .deps import get_session, get_tenant_id

router = APIRouter(prefix="/engagements", tags=["engagements"])


def _get_engagement(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID,
) -> Engagement:
    engagement = session.get(Engagement, engagement_id)
    if engagement is None or engagement.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")
    return engagement


class MissingItemOut(BaseModel):
    target_type: str
    field_path: str
    reason: str
    play: str | None
    priority: float


class GateOut(BaseModel):
    policy_code: str
    strength: GateStrength
    satisfied: bool
    blocks_transition: bool
    missing: list[MissingItemOut]
    next_best_action: MissingItemOut | None


def _gate_result_to_out(result: GateResult) -> GateOut:
    missing = [
        MissingItemOut(
            target_type=m.target_type, field_path=m.field_path, reason=m.reason,
            play=m.play, priority=m.priority,
        )
        for m in result.missing
    ]
    action = result.next_best_action()
    return GateOut(
        policy_code=result.policy_code,
        strength=GateStrength(result.strength),
        satisfied=result.satisfied,
        blocks_transition=result.blocks_transition,
        missing=missing,
        next_best_action=MissingItemOut(
            target_type=action.target_type, field_path=action.field_path,
            reason=action.reason, play=action.play, priority=action.priority,
        ) if action else None,
    )


@router.get("/{engagement_id}/gate", response_model=GateOut)
def get_gate(
    engagement_id: uuid.UUID,
    to_stage: Stage | None = None,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> GateOut:
    engagement = _get_engagement(session, tenant_id, engagement_id)
    target = to_stage or next_stage(engagement.stage)
    if target is None:
        raise HTTPException(
            status_code=422,
            detail="engagement has no further stage to evaluate a gate for",
        )
    _, result = evaluate_stage_gate(session, tenant_id, engagement, target)
    return _gate_result_to_out(result)


class StageTransitionRequest(BaseModel):
    to_stage: Stage
    actor_id: uuid.UUID
    waiver_id: uuid.UUID | None = None


class StageTransitionOut(BaseModel):
    allowed: bool
    stage: Stage
    gate: GateOut


@router.post("/{engagement_id}/stage", response_model=StageTransitionOut)
def transition_stage(
    engagement_id: uuid.UUID, body: StageTransitionRequest,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> StageTransitionOut:
    engagement = _get_engagement(session, tenant_id, engagement_id)
    try:
        outcome = apply_stage_transition(
            session, tenant_id, engagement, body.to_stage,
            waiver_id=body.waiver_id, actor=f"human:{body.actor_id}",
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not outcome.allowed:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "gate blocks this transition; a waiver is required",
                "gate": _gate_result_to_out(outcome.result).model_dump(mode="json"),
            },
        )
    session.commit()
    return StageTransitionOut(
        allowed=True, stage=engagement.stage,
        gate=_gate_result_to_out(outcome.result),
    )


class WaiverCreate(BaseModel):
    policy_id: uuid.UUID
    approved_by: uuid.UUID
    reason: str
    valid_until: datetime | None = None


class WaiverOut(BaseModel):
    id: uuid.UUID
    engagement_id: uuid.UUID
    policy_id: uuid.UUID
    approved_by: uuid.UUID
    reason: str
    valid_until: datetime | None
    approved_at: datetime


@router.post("/{engagement_id}/waivers", response_model=WaiverOut, status_code=201)
def create_waiver(
    engagement_id: uuid.UUID, body: WaiverCreate,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> Waiver:
    engagement = _get_engagement(session, tenant_id, engagement_id)
    waiver = Waiver(
        tenant_id=tenant_id, engagement_id=engagement.id,
        policy_id=body.policy_id, approved_by=body.approved_by,
        reason=body.reason, valid_until=body.valid_until,
        approved_at=datetime.now(engagement.created_at.tzinfo),
        written_by=f"human:{body.approved_by}",
    )
    session.add(waiver)
    session.commit()
    session.refresh(waiver)
    return waiver


@router.get("/{engagement_id}/graph")
def get_graph(
    engagement_id: uuid.UUID,
    include_sensitive: bool = False,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> dict:
    """§7.3: stance/influence は include_sensitive=true を明示しない限りマスクする。"""
    engagement = _get_engagement(session, tenant_id, engagement_id)
    return build_graph_json(
        session, tenant_id, engagement, include_sensitive=include_sensitive,
    )


@router.get("/{engagement_id}/graph.svg")
def get_graph_svg(
    engagement_id: uuid.UUID,
    include_sensitive: bool = False,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> Response:
    engagement = _get_engagement(session, tenant_id, engagement_id)
    dot = build_graph_dot(
        session, tenant_id, engagement, include_sensitive=include_sensitive,
    )
    svg = dot.pipe(format="svg").decode("utf-8")
    return Response(content=svg, media_type="image/svg+xml")
