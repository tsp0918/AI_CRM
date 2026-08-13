"""services/sales_groups.py のユニットテスト。"""

from __future__ import annotations

import uuid

import pytest

from crm_mvp.services import sales_groups as sg


class TestCreateSalesGroup:
    def test_creates_top_level_group(self, db_session, tenant_id):
        group = sg.create_sales_group(db_session, tenant_id, name="東日本営業部")
        db_session.commit()
        assert group.name == "東日本営業部"
        assert group.parent_group_id is None

    def test_creates_child_group(self, db_session, tenant_id):
        parent = sg.create_sales_group(db_session, tenant_id, name="営業本部")
        child = sg.create_sales_group(
            db_session, tenant_id, name="東日本営業部", parent_group_id=parent.id,
        )
        db_session.commit()
        assert child.parent_group_id == parent.id

    def test_rejects_blank_name(self, db_session, tenant_id):
        with pytest.raises(ValueError):
            sg.create_sales_group(db_session, tenant_id, name="  ")

    def test_rejects_missing_parent(self, db_session, tenant_id):
        with pytest.raises(ValueError):
            sg.create_sales_group(
                db_session, tenant_id, name="X", parent_group_id=uuid.uuid4(),
            )


class TestListSalesGroupsTreeOrdered:
    def test_children_follow_parent(self, db_session, tenant_id):
        parent = sg.create_sales_group(db_session, tenant_id, name="営業本部")
        sg.create_sales_group(db_session, tenant_id, name="東日本営業部", parent_group_id=parent.id)
        sg.create_sales_group(db_session, tenant_id, name="西日本営業部", parent_group_id=parent.id)
        db_session.commit()

        ordered = sg.list_sales_groups_tree_ordered(db_session, tenant_id)
        names = [g.name for g in ordered]
        assert names[0] == "営業本部"
        assert set(names[1:3]) == {"東日本営業部", "西日本営業部"}
