"""商品グループ(親子階層)の作成・一覧。

作成のみをサポートする(既存グループの再親子付け替えは今回のスコープ外)。
新規グループは作成時点で子を持たないため、循環参照は起こり得ない —
指定された parent_group_id が同テナントに実在することだけを確認する。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ProductGroup


def create_product_group(
    session: Session, tenant_id: uuid.UUID, *,
    name: str, parent_group_id: uuid.UUID | None = None, description: str | None = None,
) -> ProductGroup:
    if not name.strip():
        raise ValueError("グループ名を入力してください")

    if parent_group_id is not None:
        parent = session.get(ProductGroup, parent_group_id)
        if parent is None or parent.tenant_id != tenant_id:
            raise ValueError("親グループが見つかりません")

    group = ProductGroup(
        tenant_id=tenant_id, name=name.strip(), parent_group_id=parent_group_id,
        description=description.strip() if description else None,
    )
    session.add(group)
    session.flush()
    return group


def list_product_groups(session: Session, tenant_id: uuid.UUID) -> list[ProductGroup]:
    return session.execute(
        select(ProductGroup).where(ProductGroup.tenant_id == tenant_id)
        .order_by(ProductGroup.name)
    ).scalars().all()


def list_product_groups_tree_ordered(session: Session, tenant_id: uuid.UUID) -> list[ProductGroup]:
    """親グループの直後にその子が続く順序で返す(表示・選択肢の並び用)。"""
    groups = list_product_groups(session, tenant_id)
    by_parent: dict[uuid.UUID | None, list[ProductGroup]] = {}
    for group in groups:
        by_parent.setdefault(group.parent_group_id, []).append(group)

    ordered: list[ProductGroup] = []

    def walk(parent_id: uuid.UUID | None) -> None:
        for group in by_parent.get(parent_id, []):
            ordered.append(group)
            walk(group.id)

    walk(None)
    return ordered
