"""『次の一手』のタスク化(ロードマップ§6)。

gate_engine.GateResult.next_best_action() は毎回のゲート評価から導出
される揮発的な値(常に1件だけを示す設計 — MissingItem.priority 参照)。
これを ActionItem として1件アサインすることで、マネージャーが担当者に
指示し、完了状況を追跡できるようにする。「次の一手を言うだけ」から
「割り当てて追跡する」運用に引き上げる。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ActionItemStatus
from ..models import ActionItem
from .gate_engine import MissingItem


def assign_action_item(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID,
    missing_item: MissingItem, *, assigned_to: str, assigned_by: str,
) -> ActionItem:
    item = ActionItem(
        tenant_id=tenant_id, engagement_id=engagement_id,
        field_path=missing_item.field_path, reason=missing_item.reason,
        play=missing_item.play, assigned_to=assigned_to, written_by=assigned_by,
    )
    session.add(item)
    session.flush()
    return item


def complete_action_item(
    action_item: ActionItem, *, note: str | None = None,
) -> ActionItem:
    action_item.status = ActionItemStatus.DONE
    action_item.completed_at = datetime.now(timezone.utc)
    action_item.completed_note = note
    return action_item


def dismiss_action_item(
    action_item: ActionItem, *, note: str | None = None,
) -> ActionItem:
    action_item.status = ActionItemStatus.DISMISSED
    action_item.completed_at = datetime.now(timezone.utc)
    action_item.completed_note = note
    return action_item


def list_open_action_items(
    session: Session, tenant_id: uuid.UUID, engagement_id: uuid.UUID,
) -> list[ActionItem]:
    return session.execute(
        select(ActionItem).where(
            ActionItem.tenant_id == tenant_id,
            ActionItem.engagement_id == engagement_id,
            ActionItem.status == ActionItemStatus.OPEN,
        ).order_by(ActionItem.created_at.desc())
    ).scalars().all()
