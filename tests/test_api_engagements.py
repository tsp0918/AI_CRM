"""gate / stage / waiver / graph API のテスト
(HANDOVER.md §5 Phase3 item11-14, Phase4 item15,17)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from crm_mvp.enums import GateKind, GateStrength, Stage
from crm_mvp.models import GatePolicy, GraphNode, StageTransition, Waiver

from .conftest import create_account_and_engagement


def _seed_policy(
    db_session, tenant_id, to_stage, strength, conditions, code=None,
) -> GatePolicy:
    policy = GatePolicy(
        tenant_id=tenant_id, code=code or f"stage.{to_stage.value}", version=1,
        industry_template="manufacturing", kind=GateKind.STAGE, strength=strength,
        to_stage=to_stage, conditions=conditions, is_active=True,
    )
    db_session.add(policy)
    db_session.flush()
    return policy


class TestGetGate:
    def test_no_policy_defaults_to_advisory_allow(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = api_client.get(f"/engagements/{engagement.id}/gate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["satisfied"] is True
        assert body["blocks_transition"] is False

    def test_reports_missing_and_next_best_action(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        _seed_policy(
            db_session, tenant_id, Stage.PROSPECT, GateStrength.WARN,
            {"slots": [{"criterion": "identified_pain", "min_confidence": "asserted"}]},
        )
        db_session.commit()

        resp = api_client.get(f"/engagements/{engagement.id}/gate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["satisfied"] is False
        assert body["blocks_transition"] is False
        assert body["next_best_action"]["field_path"] == "criterion:identified_pain"

    def test_engagement_not_found_returns_404(self, api_client, tenant_id):
        resp = api_client.get(f"/engagements/{uuid.uuid4()}/gate")
        assert resp.status_code == 404


class TestTransitionStage:
    def test_warn_gate_does_not_block_transition(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        _seed_policy(
            db_session, tenant_id, Stage.PROSPECT, GateStrength.WARN,
            {"slots": [{"criterion": "identified_pain", "min_confidence": "asserted"}]},
        )
        db_session.commit()

        resp = api_client.post(
            f"/engagements/{engagement.id}/stage",
            json={"to_stage": "prospect", "actor_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is True
        assert body["stage"] == "prospect"
        assert body["gate"]["satisfied"] is False

        transition = db_session.query(StageTransition).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert transition.to_stage == "prospect"
        assert transition.gate_snapshot["satisfied"] is False

    def test_require_approval_blocks_without_waiver(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)
        _seed_policy(
            db_session, tenant_id, Stage.QUALIFIED, GateStrength.REQUIRE_APPROVAL,
            {"slots": [{"criterion": "budget", "min_confidence": "verified"}]},
        )
        db_session.commit()

        resp = api_client.post(
            f"/engagements/{engagement.id}/stage",
            json={"to_stage": "qualified", "actor_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 409
        assert db_session.query(StageTransition).count() == 0

    def test_valid_waiver_allows_blocked_transition(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)
        policy = _seed_policy(
            db_session, tenant_id, Stage.QUALIFIED, GateStrength.REQUIRE_APPROVAL,
            {"slots": [{"criterion": "budget", "min_confidence": "verified"}]},
        )
        approver = uuid.uuid4()
        waiver = Waiver(
            tenant_id=tenant_id, engagement_id=engagement.id, policy_id=policy.id,
            approved_by=approver, reason="経営判断により先行",
            approved_at=datetime.now(timezone.utc),
        )
        db_session.add(waiver)
        db_session.flush()
        db_session.commit()

        resp = api_client.post(
            f"/engagements/{engagement.id}/stage",
            json={
                "to_stage": "qualified", "actor_id": str(uuid.uuid4()),
                "waiver_id": str(waiver.id),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["allowed"] is True

        transition = db_session.query(StageTransition).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
        ).one()
        assert transition.waiver_id == waiver.id

    def test_mismatched_waiver_is_rejected(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)
        _seed_policy(
            db_session, tenant_id, Stage.QUALIFIED, GateStrength.REQUIRE_APPROVAL,
            {"slots": [{"criterion": "budget", "min_confidence": "verified"}]},
        )
        db_session.commit()

        resp = api_client.post(
            f"/engagements/{engagement.id}/stage",
            json={
                "to_stage": "qualified", "actor_id": str(uuid.uuid4()),
                "waiver_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 422

    def test_expired_waiver_is_rejected(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id, Stage.PROSPECT)
        policy = _seed_policy(
            db_session, tenant_id, Stage.QUALIFIED, GateStrength.REQUIRE_APPROVAL,
            {"slots": [{"criterion": "budget", "min_confidence": "verified"}]},
        )
        waiver = Waiver(
            tenant_id=tenant_id, engagement_id=engagement.id, policy_id=policy.id,
            approved_by=uuid.uuid4(), reason="期限切れテスト",
            approved_at=datetime.now(timezone.utc) - timedelta(days=10),
            valid_until=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add(waiver)
        db_session.flush()
        db_session.commit()

        resp = api_client.post(
            f"/engagements/{engagement.id}/stage",
            json={
                "to_stage": "qualified", "actor_id": str(uuid.uuid4()),
                "waiver_id": str(waiver.id),
            },
        )
        assert resp.status_code == 422


class TestCreateWaiver:
    def test_creates_waiver(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        policy = _seed_policy(
            db_session, tenant_id, Stage.QUALIFIED, GateStrength.REQUIRE_APPROVAL, {},
        )
        db_session.commit()

        resp = api_client.post(
            f"/engagements/{engagement.id}/waivers",
            json={
                "policy_id": str(policy.id), "approved_by": str(uuid.uuid4()),
                "reason": "経営判断",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["engagement_id"] == str(engagement.id)


class TestGraph:
    def test_graph_json_reflects_nodes(self, api_client, db_session, tenant_id):
        account, engagement = create_account_and_engagement(db_session, tenant_id)
        node = GraphNode(
            tenant_id=tenant_id, account_id=account.id,
            placeholder_label="決裁者(氏名未確認)", seniority_layer=3,
        )
        db_session.add(node)
        db_session.flush()
        db_session.commit()

        resp = api_client.get(f"/engagements/{engagement.id}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["nodes"]) == 1
        assert body["nodes"][0]["label"] == "決裁者(氏名未確認)"
        assert body["nodes"][0]["is_placeholder"] is True

    def test_stance_and_influence_are_masked_by_default(
        self, api_client, db_session, tenant_id,
    ):
        """§7.3: 実在の個人に対する主観評価は既定で非表示にする。"""
        from crm_mvp.enums import BuyingCenterRole, Stance
        from crm_mvp.models import EngagementRole, GraphNode

        account, engagement = create_account_and_engagement(db_session, tenant_id)
        node = GraphNode(
            tenant_id=tenant_id, account_id=account.id,
            placeholder_label="決裁者(氏名未確認)",
        )
        db_session.add(node)
        db_session.flush()
        db_session.add(EngagementRole(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node.id,
            roles=[BuyingCenterRole.DECIDER.value], stance=Stance.OPPONENT,
            influence=5,
        ))
        db_session.commit()

        masked = api_client.get(f"/engagements/{engagement.id}/graph").json()
        assert masked["nodes"][0]["stance"] is None
        assert masked["nodes"][0]["influence"] is None
        # roles / access_level は主観評価ではないのでマスクしない
        assert masked["nodes"][0]["roles"] == ["decider"]

        unmasked = api_client.get(
            f"/engagements/{engagement.id}/graph?include_sensitive=true"
        ).json()
        assert unmasked["nodes"][0]["stance"] == "opponent"
        assert unmasked["nodes"][0]["influence"] == 5

    def test_graph_svg_does_not_color_by_stance_by_default(
        self, api_client, db_session, tenant_id,
    ):
        from crm_mvp.enums import AccessLevel, Stance
        from crm_mvp.models import EngagementRole, GraphNode

        account, engagement = create_account_and_engagement(db_session, tenant_id)
        node = GraphNode(
            tenant_id=tenant_id, account_id=account.id, placeholder_label="A",
        )
        db_session.add(node)
        db_session.flush()
        # 塗り色(fillcolor)は style に "filled" が無い(=未接触/dashed)と
        # Graphviz 側で無視されるため、接触済みノードで検証する。
        db_session.add(EngagementRole(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node.id,
            stance=Stance.OPPONENT, access_level=AccessLevel.CONTACTED,
        ))
        db_session.commit()

        masked = api_client.get(f"/engagements/{engagement.id}/graph.svg")
        # OPPONENT の塗り色(#f7c5c5)が既定では出ない
        assert "#f7c5c5" not in masked.text

        unmasked = api_client.get(
            f"/engagements/{engagement.id}/graph.svg?include_sensitive=true"
        )
        assert "#f7c5c5" in unmasked.text

    def test_graph_svg_renders(self, api_client, db_session, tenant_id):
        account, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.add(GraphNode(
            tenant_id=tenant_id, account_id=account.id, placeholder_label="A",
        ))
        db_session.commit()

        resp = api_client.get(f"/engagements/{engagement.id}/graph.svg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in resp.content


class TestVerifySlot:
    def _make_slot(self, db_session, tenant_id, engagement, criterion, **kw):
        from crm_mvp.enums import Confidence
        from crm_mvp.models import QualificationSlot

        slot = QualificationSlot(
            tenant_id=tenant_id, engagement_id=engagement.id, criterion=criterion,
            value={"amount": 1}, confidence=Confidence.CORROBORATED, **kw,
        )
        db_session.add(slot)
        db_session.flush()
        db_session.commit()
        return slot

    def test_customer_document_requires_evidence_uri(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        self._make_slot(db_session, tenant_id, engagement, "budget")

        resp = api_client.post(
            f"/engagements/{engagement.id}/slots/budget/verify",
            json={"verified_by": str(uuid.uuid4()), "method": "customer_document"},
        )
        assert resp.status_code == 422

    def test_customer_document_promotes_to_verified(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        self._make_slot(db_session, tenant_id, engagement, "budget")
        verifier = uuid.uuid4()

        resp = api_client.post(
            f"/engagements/{engagement.id}/slots/budget/verify",
            json={
                "verified_by": str(verifier), "method": "customer_document",
                "evidence_uri": "s3://bucket/customer-email.eml",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["confidence"] == "verified"
        assert body["verified_by"] == str(verifier)
        assert body["decays_at"] is not None

    def test_manager_confirmation_requires_note(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        self._make_slot(db_session, tenant_id, engagement, "budget")

        resp = api_client.post(
            f"/engagements/{engagement.id}/slots/budget/verify",
            json={"verified_by": str(uuid.uuid4()), "method": "manager_confirmation"},
        )
        assert resp.status_code == 422

    def test_manager_confirmation_with_note_succeeds(
        self, api_client, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        self._make_slot(db_session, tenant_id, engagement, "budget")

        resp = api_client.post(
            f"/engagements/{engagement.id}/slots/budget/verify",
            json={
                "verified_by": str(uuid.uuid4()), "method": "manager_confirmation",
                "note": "課長が顧客電話で予算確保済みと確認",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["confidence"] == "verified"

    def test_missing_slot_returns_404(self, api_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        resp = api_client.post(
            f"/engagements/{engagement.id}/slots/budget/verify",
            json={
                "verified_by": str(uuid.uuid4()), "method": "manager_confirmation",
                "note": "x",
            },
        )
        assert resp.status_code == 404
