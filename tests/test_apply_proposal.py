"""apply_proposal.py の直接テスト。ExtractionProposal を経由した唯一の
書き込み経路が、対象ごとに正しく業務テーブルへ反映することを検証する。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from crm_mvp.enums import Criterion, EdgeType, ProposalStatus
from crm_mvp.models import (
    ExtractionProposal, GraphEdge, GraphNode, IngestionSource, QualificationSlot,
)
from crm_mvp.services.apply_proposal import apply_proposal

from .conftest import create_account_and_engagement


def _make_proposal(
    db_session, tenant_id, engagement, target_type, field_path, value,
) -> ExtractionProposal:
    source = IngestionSource(
        tenant_id=tenant_id, engagement_id=engagement.id, kind="free_note",
        raw_text="メモ",
    )
    db_session.add(source)
    db_session.flush()

    proposal = ExtractionProposal(
        tenant_id=tenant_id, source_id=source.id, engagement_id=engagement.id,
        target_type=target_type, field_path=field_path, proposed_value=value,
        model_score=0.8, rationale="test", evidence_quote="根拠",
        status=ProposalStatus.PENDING,
    )
    db_session.add(proposal)
    db_session.flush()
    return proposal


class TestNeverAiDefense:
    def test_never_ai_field_raises_runtime_error(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = _make_proposal(
            db_session, tenant_id, engagement, "engagement", "stage",
            {"stage": "closed_won"},
        )
        with pytest.raises(RuntimeError):
            apply_proposal(db_session, proposal)


class TestApplyGraphEdge:
    def test_creates_edge_resolving_nodes_by_name(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = _make_proposal(
            db_session, tenant_id, engagement, "graph_edge", "approves",
            {"from_name": "鈴木", "to_name": "佐藤部長", "sequence": 1},
        )
        apply_proposal(db_session, proposal)

        edge = db_session.query(GraphEdge).filter_by(
            tenant_id=tenant_id, edge_type=EdgeType.APPROVES,
        ).one()
        assert edge.sequence == 1

        from_node = db_session.get(GraphNode, edge.from_node_id)
        to_node = db_session.get(GraphNode, edge.to_node_id)
        assert from_node.placeholder_label == "鈴木(氏名未確認)"
        assert to_node.placeholder_label == "佐藤部長(氏名未確認)"

    def test_rejects_self_reference(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = _make_proposal(
            db_session, tenant_id, engagement, "graph_edge", "approves",
            {"from_name": "鈴木", "to_name": "鈴木"},
        )
        with pytest.raises(ValueError):
            apply_proposal(db_session, proposal)


class TestApplyEngagementField:
    def test_sets_expected_close_date(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = _make_proposal(
            db_session, tenant_id, engagement, "engagement", "expected_close_date",
            {"date": "2026-09-30"},
        )
        apply_proposal(db_session, proposal)
        assert engagement.expected_close_date == date(2026, 9, 30)


class TestApplyQualificationSlotPaperProcessTriggersCloseDate:
    def test_paper_process_recomputes_derived_close_date(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = _make_proposal(
            db_session, tenant_id, engagement,
            "qualification_slot", "criterion:paper_process",
            {"approval_layers": 3, "legal_review_required": True},
        )
        apply_proposal(db_session, proposal)

        assert engagement.derived_close_date is not None
        slot = db_session.query(QualificationSlot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.PAPER_PROCESS,
        ).one()
        assert slot.value == {"approval_layers": 3, "legal_review_required": True}


class TestApplyQualificationSlotSetsDecaysAt:
    def test_sets_decays_at_per_criterion_policy(self, db_session, tenant_id):
        """§7.1: 適用のたびに criterion 別の失効基準で decays_at を引き直す。"""
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        proposal = _make_proposal(
            db_session, tenant_id, engagement,
            "qualification_slot", "criterion:timing",
            {"target_date": "2026-12-01"},
        )
        apply_proposal(db_session, proposal)

        slot = db_session.query(QualificationSlot).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id,
            criterion=Criterion.TIMING,
        ).one()
        assert slot.decays_at is not None
        assert (slot.decays_at - slot.asserted_at).days == 90
