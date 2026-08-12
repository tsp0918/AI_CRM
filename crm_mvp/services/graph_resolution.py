"""名前や連絡先から GraphNode / Contact を解決する共通ロジック。

話者同定(§5 Phase2-7)と、抽出提案の graph_edge / engagement_role への
適用(§5 Phase2-9,10)の両方から使う。一致しない場合は氏名不明の
プレースホルダー GraphNode を立てる。

buying_center.py の設計思想: 「経理部門に決裁者がいるはずだが誰か分からない」
を可視化することが相関図の最大の役割。空白は行動を生まないが、
灰色のノードは行動を生む。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Contact, GraphNode


def resolve_contact(
    session: Session,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    email: str | None = None,
    name: str | None = None,
) -> Contact | None:
    """メールアドレス一致 → 氏名の部分一致、の順に Contact を探す。"""
    if email:
        contact = session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.account_id == account_id,
                Contact.email == email,
            )
        ).scalar_one_or_none()
        if contact is not None:
            return contact

    if name:
        return session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.account_id == account_id,
                Contact.full_name.ilike(f"%{name}%"),
            )
        ).scalar_one_or_none()

    return None


def resolve_or_create_node(
    session: Session,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str,
    email: str | None = None,
) -> tuple[GraphNode, bool]:
    """名前から GraphNode を解決する。無ければプレースホルダーを作成する。

    戻り値は (node, created_placeholder)。
    """
    contact = resolve_contact(session, tenant_id, account_id, email=email, name=name)

    if contact is not None:
        node = session.execute(
            select(GraphNode).where(
                GraphNode.tenant_id == tenant_id,
                GraphNode.contact_id == contact.id,
            )
        ).scalar_one_or_none()
        if node is not None:
            return node, False
        node = GraphNode(
            tenant_id=tenant_id, account_id=account_id, contact_id=contact.id,
        )
        session.add(node)
        session.flush()
        return node, False

    existing_placeholder = session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == tenant_id,
            GraphNode.account_id == account_id,
            GraphNode.contact_id.is_(None),
            GraphNode.placeholder_label == f"{name}(氏名未確認)",
        )
    ).scalar_one_or_none()
    if existing_placeholder is not None:
        return existing_placeholder, False

    node = GraphNode(
        tenant_id=tenant_id, account_id=account_id,
        placeholder_label=f"{name}(氏名未確認)",
    )
    session.add(node)
    session.flush()
    return node, True
