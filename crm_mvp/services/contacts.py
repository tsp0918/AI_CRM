"""取引先担当者(Contact)の登録と、案件への紐付け。

Lead の概念は「誰から/誰に」の情報を伴って初めて動かせる。相関図
(GraphNode/EngagementRole)は AI 抽出やプレースホルダーからも育つ設計
だが、この経路は人間が最初の接点を明示的に登録するためのもの
(§4: 参加者の同定は AI が直接書ける領域だが、起点の登録は人が行う)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import AccessLevel, Stance
from ..models import Contact, Engagement, EngagementRole, GraphNode


def create_contact(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID,
    *, full_name: str, title: str | None = None, org_unit: str | None = None,
    email: str | None = None,
) -> Contact:
    contact = Contact(
        tenant_id=tenant_id, account_id=account_id, full_name=full_name,
        title=title, org_unit=org_unit, email=email,
    )
    session.add(contact)
    session.flush()
    return contact


def get_or_create_node_for_contact(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID,
    contact: Contact,
) -> GraphNode:
    node = session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == tenant_id, GraphNode.contact_id == contact.id,
        )
    ).scalar_one_or_none()
    if node is not None:
        return node
    node = GraphNode(
        tenant_id=tenant_id, account_id=account_id, contact_id=contact.id,
        org_unit=contact.org_unit,
    )
    session.add(node)
    session.flush()
    return node


def link_contact_to_engagement(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
    node: GraphNode, *, roles: list[str], stance: Stance,
    access_level: AccessLevel, written_by: str,
) -> EngagementRole:
    role = session.execute(
        select(EngagementRole).where(
            EngagementRole.tenant_id == tenant_id,
            EngagementRole.engagement_id == engagement.id,
            EngagementRole.node_id == node.id,
        )
    ).scalar_one_or_none()
    if role is None:
        role = EngagementRole(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node.id,
        )
        session.add(role)
    role.roles = roles
    role.stance = stance
    role.access_level = access_level
    role.last_touched_at = datetime.now(timezone.utc)
    role.written_by = written_by
    session.flush()
    return role


def register_contact_and_link(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
    *, full_name: str, title: str | None = None, org_unit: str | None = None,
    email: str | None = None, roles: list[str], stance: Stance,
    access_level: AccessLevel, written_by: str,
) -> tuple[Contact, EngagementRole]:
    """新規 Contact を登録し、その場でこの案件に紐付ける(1画面完結の登録経路)。"""
    contact = create_contact(
        session, tenant_id, engagement.account_id, full_name=full_name,
        title=title, org_unit=org_unit, email=email,
    )
    node = get_or_create_node_for_contact(
        session, tenant_id, engagement.account_id, contact,
    )
    role = link_contact_to_engagement(
        session, tenant_id, engagement, node, roles=roles, stance=stance,
        access_level=access_level, written_by=written_by,
    )
    return contact, role
