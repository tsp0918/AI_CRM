"""artifact_gate.py(見積・契約発行のARTIFACTゲート評価)のテスト。

gate_engine.evaluate_gate 自体は tests/test_gate_engine.py で検証済みのため、
ここではオーケストレーション層(ポリシー検索・DBからのコンテキスト構築)の
配線のみを検証する(CRM_連携_実装計画.md Phase 1a)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crm_mvp.enums import ArtifactType, ComplianceCheckType, ComplianceOutcome, GateKind, GateStrength
from crm_mvp.models import ComplianceStatus, GatePolicy
from crm_mvp.services.artifact_gate import evaluate_artifact_gate

from .conftest import create_account_and_engagement


def make_artifact_policy(db_session, tenant_id, artifact_type: ArtifactType) -> GatePolicy:
    policy = GatePolicy(
        tenant_id=tenant_id, code=f"artifact.{artifact_type.value}",
        kind=GateKind.ARTIFACT, artifact_type=artifact_type,
        strength=GateStrength.BLOCK, industry_template="manufacturing",
        conditions={"compliance": [{"check_type": "anti_social", "must_be_fresh": True}]},
    )
    db_session.add(policy)
    db_session.flush()
    return policy


class TestEvaluateArtifactGate:
    def test_allows_when_no_policy_seeded(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        policy, result = evaluate_artifact_gate(
            db_session, tenant_id, engagement, ArtifactType.QUOTE,
        )
        assert policy is None
        assert result.satisfied is True
        assert result.blocks_transition is False

    def test_blocks_when_compliance_not_fresh(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        make_artifact_policy(db_session, tenant_id, ArtifactType.QUOTE)

        policy, result = evaluate_artifact_gate(
            db_session, tenant_id, engagement, ArtifactType.QUOTE,
        )
        assert policy is not None
        assert result.satisfied is False
        assert result.blocks_transition is True
        assert result.next_best_action() is not None

    def test_allows_when_compliance_is_fresh(self, db_session, tenant_id):
        account, engagement = create_account_and_engagement(db_session, tenant_id)
        make_artifact_policy(db_session, tenant_id, ArtifactType.QUOTE)
        db_session.add(ComplianceStatus(
            tenant_id=tenant_id, account_id=account.id,
            check_type=ComplianceCheckType.ANTI_SOCIAL, outcome=ComplianceOutcome.CLEAR,
            valid_until=datetime.now(timezone.utc) + timedelta(days=30),
        ))
        db_session.flush()

        policy, result = evaluate_artifact_gate(
            db_session, tenant_id, engagement, ArtifactType.QUOTE,
        )
        assert result.satisfied is True
        assert result.blocks_transition is False

    def test_quote_and_contract_policies_are_independent(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        make_artifact_policy(db_session, tenant_id, ArtifactType.QUOTE)
        # contract用ポリシーは投入していない -> ARTIFACT.CONTRACT は無条件許可のまま
        policy, result = evaluate_artifact_gate(
            db_session, tenant_id, engagement, ArtifactType.CONTRACT,
        )
        assert policy is None
        assert result.satisfied is True
