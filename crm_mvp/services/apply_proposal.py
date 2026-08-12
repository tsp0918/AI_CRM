"""ExtractionProposal を業務テーブルへ反映する唯一の経路。

HANDOVER.md §3.1: すべての AI 由来の書き込みは ExtractionProposal を経由する。
このモジュールはその「経由した後」の適用ロジックであり、自動適用(status が
AUTO_APPLIED になった直後)でも、人が /proposals/{id}/accept で承認した場合でも、
必ずこの一本の経路を通す。書き込み経路を1つに保つことが監査可能性・
取り消し可能性を成立させる前提。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import AccessLevel, Criterion, EdgeType, Stance
from ..models import (
    Engagement, EngagementRole, ExtractionProposal, GraphEdge,
    QualificationSlot,
)
from .extraction_pipeline import confidence_for_ai_write
from .graph_resolution import resolve_or_create_node
from .stage_transitions import recompute_derived_close_date

NEVER_AI_FIELDS: frozenset[str] = frozenset({
    "qualification_slot:confidence:verified",
    "engagement:stage",
    "engagement:amount",
    "waiver:approved_by",
})


def apply_proposal(session: Session, proposal: ExtractionProposal) -> None:
    key = f"{proposal.target_type}:{proposal.field_path}"
    if key in NEVER_AI_FIELDS:
        raise RuntimeError(
            f"invariant violation: {key} must never reach apply_proposal "
            "(§3.2)"
        )

    if proposal.engagement_id is None:
        raise ValueError("proposal has no engagement_id; cannot resolve account")
    engagement = session.get(Engagement, proposal.engagement_id)
    if engagement is None:
        raise ValueError(f"engagement not found: {proposal.engagement_id}")

    handler = _HANDLERS.get(proposal.target_type)
    if handler is None:
        raise ValueError(f"unsupported target_type: {proposal.target_type}")
    handler(session, proposal, engagement, engagement.account_id, proposal.tenant_id)


def _apply_qualification_slot(
    session: Session, proposal: ExtractionProposal, engagement: Engagement,
    account_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    criterion = Criterion(proposal.field_path.split(":", 1)[1])
    value = dict(proposal.proposed_value)

    if (criterion == Criterion.ECONOMIC_BUYER
            and "node_id" not in value and value.get("name")):
        node, _ = resolve_or_create_node(
            session, tenant_id, account_id, name=value["name"],
        )
        value["node_id"] = str(node.id)

    slot = session.execute(
        select(QualificationSlot).where(
            QualificationSlot.tenant_id == tenant_id,
            QualificationSlot.engagement_id == engagement.id,
            QualificationSlot.criterion == criterion,
        )
    ).scalar_one_or_none()
    existing_confidence = slot.confidence if slot is not None else None

    if slot is None:
        slot = QualificationSlot(
            tenant_id=tenant_id, engagement_id=engagement.id, criterion=criterion,
        )
        session.add(slot)

    slot.value = value
    slot.confidence = confidence_for_ai_write(existing_confidence)
    slot.evidence_source_id = proposal.source_id
    slot.evidence_quote = proposal.evidence_quote
    slot.asserted_at = datetime.now(timezone.utc)
    slot.written_by = f"ai:proposal:{proposal.id}"
    slot.source_id = proposal.source_id
    session.flush()

    if criterion == Criterion.PAPER_PROCESS:
        recompute_derived_close_date(session, engagement)


def _apply_engagement_role(
    session: Session, proposal: ExtractionProposal, engagement: Engagement,
    account_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    value = proposal.proposed_value
    node_name = value.get("node_name")
    if not node_name:
        raise ValueError("engagement_role proposal missing node_name")

    node, _ = resolve_or_create_node(session, tenant_id, account_id, name=node_name)
    role = session.execute(
        select(EngagementRole).where(
            EngagementRole.tenant_id == tenant_id,
            EngagementRole.engagement_id == engagement.id,
            EngagementRole.node_id == node.id,
        )
    ).scalar_one_or_none()
    existing_confidence = role.confidence if role is not None else None

    if role is None:
        role = EngagementRole(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node.id,
        )
        session.add(role)

    if proposal.field_path == "stance":
        role.stance = Stance(value["stance"])
    elif proposal.field_path == "access_level":
        role.access_level = AccessLevel(value["access_level"])
    else:
        raise ValueError(
            f"unsupported engagement_role field_path: {proposal.field_path}"
        )

    role.confidence = confidence_for_ai_write(existing_confidence)
    role.written_by = f"ai:proposal:{proposal.id}"
    role.source_id = proposal.source_id
    role.last_touched_at = datetime.now(timezone.utc)
    session.flush()


def _apply_graph_edge(
    session: Session, proposal: ExtractionProposal, engagement: Engagement,
    account_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    if proposal.field_path != "approves":
        raise ValueError(f"unsupported graph_edge field_path: {proposal.field_path}")

    value = proposal.proposed_value
    from_name, to_name = value.get("from_name"), value.get("to_name")
    if not from_name or not to_name:
        raise ValueError("graph_edge proposal missing from_name/to_name")

    from_node, _ = resolve_or_create_node(session, tenant_id, account_id, name=from_name)
    to_node, _ = resolve_or_create_node(session, tenant_id, account_id, name=to_name)
    if from_node.id == to_node.id:
        raise ValueError("graph_edge cannot self-reference (ck_edge_no_self)")

    edge = session.execute(
        select(GraphEdge).where(
            GraphEdge.tenant_id == tenant_id,
            GraphEdge.from_node_id == from_node.id,
            GraphEdge.to_node_id == to_node.id,
            GraphEdge.edge_type == EdgeType.APPROVES,
        )
    ).scalar_one_or_none()
    existing_confidence = edge.confidence if edge is not None else None

    if edge is None:
        edge = GraphEdge(
            tenant_id=tenant_id, account_id=account_id,
            from_node_id=from_node.id, to_node_id=to_node.id,
            edge_type=EdgeType.APPROVES,
        )
        session.add(edge)

    if "sequence" in value:
        edge.sequence = value["sequence"]
    edge.confidence = confidence_for_ai_write(existing_confidence)
    edge.evidence_source_id = proposal.source_id
    edge.evidence_quote = proposal.evidence_quote
    edge.written_by = f"ai:proposal:{proposal.id}"
    edge.source_id = proposal.source_id
    session.flush()


def _apply_engagement_field(
    session: Session, proposal: ExtractionProposal, engagement: Engagement,
    account_id: uuid.UUID, tenant_id: uuid.UUID,
) -> None:
    if proposal.field_path != "expected_close_date":
        raise ValueError(f"unsupported engagement field_path: {proposal.field_path}")
    raw = proposal.proposed_value.get("date")
    if not raw:
        raise ValueError("engagement.expected_close_date proposal missing 'date'")
    engagement.expected_close_date = date.fromisoformat(raw)
    session.flush()


_HANDLERS = {
    "qualification_slot": _apply_qualification_slot,
    "engagement_role": _apply_engagement_role,
    "graph_edge": _apply_graph_edge,
    "engagement": _apply_engagement_field,
}
