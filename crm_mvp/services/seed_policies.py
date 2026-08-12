"""標準ゲートポリシー（製造業テンプレート）。

原則: 入口を緩く、出口を固く。
案件化ゲートを厳しくすると担当者はリードを登録しなくなり、
パイプラインが痩せるだけで何の統制にもならない。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ArtifactType, GateKind, GateStrength, Stage

MANUFACTURING_TEMPLATE: list[dict] = [
    {
        "code": "stage.qualified",
        "kind": GateKind.STAGE,
        "to_stage": Stage.QUALIFIED,
        "strength": GateStrength.ADVISORY,   # 案件化は止めない
        "conditions": {
            "slots": [
                {"criterion": "identified_pain", "min_confidence": "asserted"},
                {"criterion": "timing", "min_confidence": "asserted"},
            ],
        },
    },
    {
        "code": "stage.proposal",
        "kind": GateKind.STAGE,
        "to_stage": Stage.PROPOSAL,
        "strength": GateStrength.WARN,
        "conditions": {
            "slots": [
                {"criterion": "metrics", "min_confidence": "asserted"},
                {"criterion": "decision_criteria", "min_confidence": "asserted"},
                {"criterion": "budget", "min_confidence": "asserted"},
            ],
            "graph": [{"rule": "min_engaged_contacts", "value": 2}],
        },
    },
    {
        "code": "stage.negotiation",
        "kind": GateKind.STAGE,
        "to_stage": Stage.NEGOTIATION,
        "strength": GateStrength.REQUIRE_APPROVAL,
        "conditions": {
            "slots": [
                {"criterion": "economic_buyer", "min_confidence": "corroborated"},
                {"criterion": "paper_process", "min_confidence": "asserted"},
                {"criterion": "competition", "min_confidence": "asserted"},
            ],
            "graph": [
                {"rule": "path_to_decider", "min_confidence": "corroborated"},
            ],
        },
    },
    {
        "code": "artifact.quote",
        "kind": GateKind.ARTIFACT,
        "artifact_type": ArtifactType.QUOTE,
        "strength": GateStrength.BLOCK,
        "conditions": {
            "compliance": [
                {"check_type": "anti_social", "must_be_fresh": True},
            ],
        },
    },
    {
        "code": "artifact.contract",
        "kind": GateKind.ARTIFACT,
        "artifact_type": ArtifactType.CONTRACT,
        "strength": GateStrength.BLOCK,
        "conditions": {
            "slots": [
                {"criterion": "economic_buyer", "min_confidence": "verified"},
                {"criterion": "paper_process", "min_confidence": "verified"},
            ],
            "graph": [
                {"rule": "path_to_decider", "min_confidence": "verified"},
            ],
            "compliance": [
                {"check_type": "anti_social", "must_be_fresh": True},
                {"check_type": "credit", "must_be_fresh": True},
                {"check_type": "export_control", "must_be_fresh": True},
            ],
        },
    },
]

# フィールド別の初期自動化設定。
# 「割合」を人が決めるのではなく、承認率の実測で自動的に移動する。
DEFAULT_AUTONOMY: list[dict] = [
    # 事実の記録は最初から全自動（可逆・低リスク）
    {"target_type": "ingestion_source", "field_path": "participants",
     "mode": "always_auto"},
    {"target_type": "engagement_role", "field_path": "last_touched_at",
     "mode": "always_auto"},
    {"target_type": "engagement_role", "field_path": "access_level",
     "mode": "always_auto"},
    # 判断に影響するものは承認率次第で昇格
    {"target_type": "qualification_slot", "field_path": "criterion:metrics",
     "mode": "auto_if_trusted", "auto_apply_threshold": 0.90},
    {"target_type": "qualification_slot", "field_path": "criterion:competition",
     "mode": "auto_if_trusted", "auto_apply_threshold": 0.88},
    {"target_type": "graph_edge", "field_path": "approves",
     "mode": "auto_if_trusted", "auto_apply_threshold": 0.92},
    # 人の主観評価が入るものは常に確認
    {"target_type": "engagement_role", "field_path": "stance",
     "mode": "always_confirm"},
    {"target_type": "qualification_slot", "field_path": "criterion:champion",
     "mode": "always_confirm"},
    # 人のみ
    {"target_type": "engagement", "field_path": "stage", "mode": "never_ai"},
    {"target_type": "engagement", "field_path": "amount", "mode": "never_ai"},
]


def upsert_gate_policies(
    session: Session,
    tenant_id: uuid.UUID,
    industry_template: str = "manufacturing",
) -> int:
    """MANUFACTURING_TEMPLATE を tenant_id 単位で投入する。

    同一 (tenant_id, code, industry_template) が既に存在する場合はスキップする
    （version を上げるのは明示的な改版のときのみで、投入コマンドの
    再実行では上書きしない）。

    GatePolicy に tenant_id を持たせるかどうかは §7.4 の分離方式が
    決まるまでの暫定であり、本来「製品側が定義する標準ポリシー」は
    テナント非依存であるべきという設計意図と現状は一致していない。
    """
    from ..models import GatePolicy

    inserted = 0
    for spec in MANUFACTURING_TEMPLATE:
        existing = session.execute(
            select(GatePolicy).where(
                GatePolicy.tenant_id == tenant_id,
                GatePolicy.code == spec["code"],
                GatePolicy.industry_template == industry_template,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(GatePolicy(
            tenant_id=tenant_id, industry_template=industry_template, **spec
        ))
        inserted += 1
    return inserted


def upsert_default_autonomy(session: Session, tenant_id: uuid.UUID) -> int:
    """DEFAULT_AUTONOMY を tenant_id 単位で投入する。

    テナント分離の方式自体は HANDOVER.md §7.4 で未決。ここでは
    FieldAutonomyPolicy が既に持つ tenant_id カラムをそのまま使う
    最小の前提のみを置いている。
    """
    from ..models import FieldAutonomyPolicy

    inserted = 0
    for spec in DEFAULT_AUTONOMY:
        existing = session.execute(
            select(FieldAutonomyPolicy).where(
                FieldAutonomyPolicy.tenant_id == tenant_id,
                FieldAutonomyPolicy.target_type == spec["target_type"],
                FieldAutonomyPolicy.field_path == spec["field_path"],
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(FieldAutonomyPolicy(tenant_id=tenant_id, **spec))
        inserted += 1
    return inserted
