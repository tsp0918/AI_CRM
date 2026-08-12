"""ステージ遷移とクローズ日逆算(HANDOVER.md §5 Phase3 item 12, 14)。

DB からゲート評価に必要な dict を組み立てて gate_engine.evaluate_gate に渡す
薄いオーケストレーション層。gate_engine 自体は DB を知らない
(tests/test_gate_engine.py はこの層を経由せず素の dict で検証している)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Criterion, GateKind, GateStrength, Stage
from ..models import (
    ComplianceStatus, Engagement, EngagementRole, GatePolicy, GraphEdge,
    GraphNode, QualificationSlot, StageTransition, Waiver,
)
from .gate_engine import GateResult, derive_close_date, evaluate_gate

STAGE_ORDER: list[Stage] = [
    Stage.LEAD, Stage.PROSPECT, Stage.QUALIFIED, Stage.PROPOSAL,
    Stage.NEGOTIATION, Stage.CLOSED_WON,
]


def next_stage(current: Stage) -> Stage | None:
    if current not in STAGE_ORDER:
        return None
    idx = STAGE_ORDER.index(current)
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


def load_gate_context(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
) -> dict:
    """gate_engine.evaluate_gate が要求する dict 群を DB から組み立てる。"""
    slots = {
        row.criterion: row
        for row in session.execute(
            select(QualificationSlot).where(
                QualificationSlot.tenant_id == tenant_id,
                QualificationSlot.engagement_id == engagement.id,
            )
        ).scalars()
    }

    node_rows = session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == tenant_id,
            GraphNode.account_id == engagement.account_id,
        )
    ).scalars().all()
    nodes = {n.id: {"id": n.id} for n in node_rows}

    edge_rows = session.execute(
        select(GraphEdge).where(
            GraphEdge.tenant_id == tenant_id,
            GraphEdge.account_id == engagement.account_id,
        )
    ).scalars().all()
    edges = [
        {
            "edge_type": e.edge_type, "confidence": e.confidence,
            "from_node_id": e.from_node_id, "to_node_id": e.to_node_id,
        }
        for e in edge_rows
    ]

    role_rows = session.execute(
        select(EngagementRole).where(
            EngagementRole.tenant_id == tenant_id,
            EngagementRole.engagement_id == engagement.id,
        )
    ).scalars().all()
    roles = {
        r.node_id: {"access_level": r.access_level, "roles": r.roles or []}
        for r in role_rows
    }

    compliance_rows = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == tenant_id,
            ComplianceStatus.account_id == engagement.account_id,
        )
    ).scalars().all()
    compliance = {
        c.check_type: {"is_fresh": c.is_fresh, "outcome": c.outcome}
        for c in compliance_rows
    }

    return {
        "slots": slots, "nodes": nodes, "edges": edges,
        "roles": roles, "compliance": compliance,
    }


def find_gate_policy(
    session: Session, tenant_id: uuid.UUID, to_stage: Stage,
    industry_template: str = "manufacturing",
) -> GatePolicy | None:
    return session.execute(
        select(GatePolicy).where(
            GatePolicy.tenant_id == tenant_id,
            GatePolicy.kind == GateKind.STAGE,
            GatePolicy.to_stage == to_stage,
            GatePolicy.industry_template == industry_template,
            GatePolicy.is_active.is_(True),
        ).order_by(GatePolicy.version.desc())
    ).scalars().first()


def evaluate_stage_gate(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
    to_stage: Stage, industry_template: str = "manufacturing",
) -> tuple[GatePolicy | None, GateResult]:
    policy = find_gate_policy(session, tenant_id, to_stage, industry_template)
    if policy is None:
        # ポリシー未投入なら無条件で許可する(§3.5: 入口は緩くが既定)
        return None, GateResult(
            policy_code=f"stage.{to_stage.value}",
            strength=GateStrength.ADVISORY, satisfied=True,
        )

    ctx = load_gate_context(session, tenant_id, engagement)
    policy_dict = {
        "code": policy.code, "strength": policy.strength,
        "conditions": policy.conditions,
    }
    result = evaluate_gate(
        policy_dict, ctx["slots"], ctx["nodes"], ctx["edges"],
        ctx["roles"], ctx["compliance"],
    )
    return policy, result


@dataclass(slots=True)
class TransitionOutcome:
    allowed: bool
    result: GateResult
    transition: StageTransition | None = None


def apply_stage_transition(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
    to_stage: Stage, *, waiver_id: uuid.UUID | None = None,
    actor: str = "human:unknown", industry_template: str = "manufacturing",
) -> TransitionOutcome:
    """ゲート評価 → (block なら Waiver 必須) → StageTransition 記録 → Engagement 更新。

    §3.5: BLOCK を使ってよいのは Artifact Gate だけ、という原則はポリシー側
    (strength)の設計責務。ここでは blocks_transition の結果をそのまま尊重する。

    waiver_id は「渡されたら無条件で通す」トークンにしない。この
    engagement・このポリシーに対して発行された、期限切れでない実在の
    Waiver かどうかを検証する(でなければ誰でも任意の UUID でゲートを
    素通りできてしまい、Waiver がコンプライアンス証跡として機能しない)。
    """
    policy, result = evaluate_stage_gate(
        session, tenant_id, engagement, to_stage, industry_template,
    )
    if result.blocks_transition:
        if waiver_id is None:
            return TransitionOutcome(allowed=False, result=result)
        waiver = session.get(Waiver, waiver_id)
        if (
            waiver is None
            or waiver.tenant_id != tenant_id
            or waiver.engagement_id != engagement.id
            or (policy is not None and waiver.policy_id != policy.id)
        ):
            raise ValueError("waiver does not match this engagement/policy")
        if waiver.valid_until is not None and waiver.valid_until < datetime.now(
            timezone.utc
        ):
            raise ValueError("waiver has expired")

    transition = StageTransition(
        tenant_id=tenant_id, engagement_id=engagement.id,
        from_stage=engagement.stage, to_stage=to_stage,
        occurred_at=datetime.now(timezone.utc),
        gate_snapshot={
            "policy_code": result.policy_code,
            "strength": GateStrength(result.strength).value,
            "satisfied": result.satisfied,
            "missing": [m.field_path for m in result.missing],
        },
        waiver_id=waiver_id,
        written_by=actor,
    )
    session.add(transition)
    engagement.stage = to_stage
    session.flush()
    return TransitionOutcome(allowed=True, result=result, transition=transition)


def recompute_derived_close_date(session: Session, engagement: Engagement) -> None:
    """paper_process スロットの承認階層数からクローズ日を逆算する(§5 item14)。

    HANDOVER.md §9 で疎通確認済みの gate_engine.derive_close_date をそのまま使う。
    """
    slot = session.execute(
        select(QualificationSlot).where(
            QualificationSlot.tenant_id == engagement.tenant_id,
            QualificationSlot.engagement_id == engagement.id,
            QualificationSlot.criterion == Criterion.PAPER_PROCESS,
        )
    ).scalar_one_or_none()
    if slot is None or not slot.value:
        return

    approval_layers = slot.value.get("approval_layers")
    if approval_layers is None:
        return
    legal_review_days = 5 if slot.value.get("legal_review_required") else 0

    engagement.derived_close_date = derive_close_date(
        approval_layers=int(approval_layers),
        legal_review_days=legal_review_days,
    ).date()
    session.flush()
