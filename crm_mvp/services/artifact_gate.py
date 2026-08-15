"""ARTIFACT種別のゲート評価(見積・契約発行の可否)。

`crm_mvp/services/stage_transitions.py`のSTAGE版と対になるオーケストレーション
層。`gate_engine.evaluate_gate()`自体は種別非依存(STAGE/ARTIFACT問わず同じ
関数)なので、ここでは`GatePolicy.artifact_type`で検索する薄い配線層のみを
書く。`seed_policies.py`の`artifact.quote`/`artifact.contract`ポリシーは
Phase 1a以前から投入済みだったが、これまで評価する関数が存在せず完全に
無効だった(CRM_連携_実装計画.md Phase 1a)。

Waiver連携(BLOCK時にWaiverで通す仕組み)は`apply_stage_transition`が持つが、
今回は含めない — BLOCK時は素直に拒否のみ(必要になった時点で追加)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ArtifactType, GateKind, GateStrength
from ..models import Engagement, GatePolicy
from .gate_engine import GateResult, evaluate_gate
from .stage_transitions import load_gate_context


def find_artifact_gate_policy(
    session: Session, tenant_id: uuid.UUID, artifact_type: ArtifactType,
    industry_template: str = "manufacturing",
) -> GatePolicy | None:
    return session.execute(
        select(GatePolicy).where(
            GatePolicy.tenant_id == tenant_id,
            GatePolicy.kind == GateKind.ARTIFACT,
            GatePolicy.artifact_type == artifact_type,
            GatePolicy.industry_template == industry_template,
            GatePolicy.is_active.is_(True),
        ).order_by(GatePolicy.version.desc())
    ).scalars().first()


def evaluate_artifact_gate(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
    artifact_type: ArtifactType, industry_template: str = "manufacturing",
) -> tuple[GatePolicy | None, GateResult]:
    policy = find_artifact_gate_policy(session, tenant_id, artifact_type, industry_template)
    if policy is None:
        # ポリシー未投入なら無条件で許可する(stage_transitions.pyと同じ既定)。
        return None, GateResult(
            policy_code=f"artifact.{artifact_type.value}",
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
