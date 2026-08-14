"""action_items.py のユニットテスト。"""

from __future__ import annotations

import uuid

from crm_mvp.enums import ActionItemStatus
from crm_mvp.models import ActionItem
from crm_mvp.services import action_items as ai
from crm_mvp.services.gate_engine import MissingItem

from .conftest import create_account_and_engagement


def make_missing_item(**overrides) -> MissingItem:
    defaults = dict(
        target_type="qualification_slot", field_path="criterion:champion",
        reason="champion が asserted に達していない", play="チャンピオン候補に接触する",
    )
    defaults.update(overrides)
    return MissingItem(**defaults)


class TestAssignActionItem:
    def test_creates_open_item_with_snapshot_of_missing_item(
        self, db_session, tenant_id,
    ):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = ai.assign_action_item(
            db_session, tenant_id, engagement.id, make_missing_item(),
            assigned_to="佐藤 健", assigned_by="human:manager-1",
        )
        db_session.commit()

        assert item.status == ActionItemStatus.OPEN
        assert item.assigned_to == "佐藤 健"
        assert item.written_by == "human:manager-1"
        assert item.field_path == "criterion:champion"
        assert item.play == "チャンピオン候補に接触する"


class TestCompleteAndDismiss:
    def _make_open_item(self, db_session, tenant_id, engagement_id):
        return ai.assign_action_item(
            db_session, tenant_id, engagement_id, make_missing_item(),
            assigned_to="鈴木 花子", assigned_by="human:manager-1",
        )

    def test_complete_sets_status_and_note(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = self._make_open_item(db_session, tenant_id, engagement.id)

        ai.complete_action_item(item, note="接触完了、次回MTG設定済み")
        db_session.commit()

        assert item.status == ActionItemStatus.DONE
        assert item.completed_at is not None
        assert item.completed_note == "接触完了、次回MTG設定済み"

    def test_dismiss_sets_status(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = self._make_open_item(db_session, tenant_id, engagement.id)

        ai.dismiss_action_item(item)
        db_session.commit()

        assert item.status == ActionItemStatus.DISMISSED
        assert item.completed_at is not None


class TestListOpenActionItems:
    def test_excludes_completed_and_other_engagements(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        _, other_engagement = create_account_and_engagement(db_session, tenant_id)

        open_item = ai.assign_action_item(
            db_session, tenant_id, engagement.id, make_missing_item(),
            assigned_to="A", assigned_by="human:m",
        )
        done_item = ai.assign_action_item(
            db_session, tenant_id, engagement.id, make_missing_item(),
            assigned_to="B", assigned_by="human:m",
        )
        ai.complete_action_item(done_item)
        ai.assign_action_item(
            db_session, tenant_id, other_engagement.id, make_missing_item(),
            assigned_to="C", assigned_by="human:m",
        )
        db_session.commit()

        result = ai.list_open_action_items(db_session, tenant_id, engagement.id)
        assert [r.id for r in result] == [open_item.id]

    def test_other_tenant_items_are_invisible(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        ai.assign_action_item(
            db_session, tenant_id, engagement.id, make_missing_item(),
            assigned_to="A", assigned_by="human:m",
        )
        db_session.commit()

        result = ai.list_open_action_items(db_session, uuid.uuid4(), engagement.id)
        assert result == []


class TestDueAt:
    def test_assign_action_item_stores_due_at(self, db_session, tenant_id):
        from datetime import datetime, timezone

        _, engagement = create_account_and_engagement(db_session, tenant_id)
        due = datetime(2026, 8, 20, tzinfo=timezone.utc)
        item = ai.assign_action_item(
            db_session, tenant_id, engagement.id, make_missing_item(),
            assigned_to="佐藤 健", assigned_by="human:manager-1", due_at=due,
        )
        db_session.commit()

        assert item.due_at == due

    def test_assign_action_item_due_at_defaults_to_none(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = ai.assign_action_item(
            db_session, tenant_id, engagement.id, make_missing_item(),
            assigned_to="佐藤 健", assigned_by="human:manager-1",
        )
        assert item.due_at is None


class TestCreateManualActionItem:
    def test_creates_open_item_without_gate_context(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        item = ai.create_manual_action_item(
            db_session, tenant_id, engagement.id,
            assigned_to="鈴木 花子", task="来週までに提案資料をレビューする",
            assigned_by="human:manager-1",
        )
        db_session.commit()

        assert item.status == ActionItemStatus.OPEN
        assert item.field_path == "manual"
        assert item.reason == "来週までに提案資料をレビューする"
        assert item.play is None

    def test_appears_in_list_open_action_items(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        ai.create_manual_action_item(
            db_session, tenant_id, engagement.id,
            assigned_to="鈴木 花子", task="タスクA", assigned_by="human:manager-1",
        )
        db_session.commit()

        result = ai.list_open_action_items(db_session, tenant_id, engagement.id)
        assert len(result) == 1
        assert result[0].reason == "タスクA"
