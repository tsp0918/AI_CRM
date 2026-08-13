"""セールスグループ(親子階層)の作成・一覧。services/product_groups.py と
同じ設計 — 作成のみをサポートし、循環参照は「親が同テナントに実在する
か」の確認だけで防ぐ(新規グループは子を持ち得ないため)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SalesGroup


def create_sales_group(
    session: Session, tenant_id: uuid.UUID, *,
    name: str, parent_group_id: uuid.UUID | None = None, description: str | None = None,
) -> SalesGroup:
    if not name.strip():
        raise ValueError("グループ名を入力してください")

    if parent_group_id is not None:
        parent = session.get(SalesGroup, parent_group_id)
        if parent is None or parent.tenant_id != tenant_id:
            raise ValueError("親グループが見つかりません")

    group = SalesGroup(
        tenant_id=tenant_id, name=name.strip(), parent_group_id=parent_group_id,
        description=description.strip() if description else None,
    )
    session.add(group)
    session.flush()
    return group


def list_sales_groups(session: Session, tenant_id: uuid.UUID) -> list[SalesGroup]:
    return session.execute(
        select(SalesGroup).where(SalesGroup.tenant_id == tenant_id)
        .order_by(SalesGroup.name)
    ).scalars().all()


def list_sales_groups_tree_ordered(session: Session, tenant_id: uuid.UUID) -> list[SalesGroup]:
    """親グループの直後にその子が続く順序で返す(表示・選択肢の並び用)。"""
    groups = list_sales_groups(session, tenant_id)
    by_parent: dict[uuid.UUID | None, list[SalesGroup]] = {}
    for group in groups:
        by_parent.setdefault(group.parent_group_id, []).append(group)

    ordered: list[SalesGroup] = []

    def walk(parent_id: uuid.UUID | None) -> None:
        for group in by_parent.get(parent_id, []):
            ordered.append(group)
            walk(group.id)

    walk(None)
    return ordered
