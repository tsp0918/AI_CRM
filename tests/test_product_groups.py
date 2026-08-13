"""services/product_groups.py のユニットテスト。"""

from __future__ import annotations

import uuid

import pytest

from crm_mvp.services import product_groups as pg


class TestCreateProductGroup:
    def test_creates_top_level_group(self, db_session, tenant_id):
        group = pg.create_product_group(db_session, tenant_id, name="検査装置")
        db_session.commit()

        assert group.name == "検査装置"
        assert group.parent_group_id is None

    def test_creates_child_group(self, db_session, tenant_id):
        parent = pg.create_product_group(db_session, tenant_id, name="検査装置")
        child = pg.create_product_group(
            db_session, tenant_id, name="光学式", parent_group_id=parent.id,
        )
        db_session.commit()

        assert child.parent_group_id == parent.id

    def test_rejects_blank_name(self, db_session, tenant_id):
        with pytest.raises(ValueError):
            pg.create_product_group(db_session, tenant_id, name="  ")

    def test_rejects_missing_parent(self, db_session, tenant_id):
        with pytest.raises(ValueError):
            pg.create_product_group(
                db_session, tenant_id, name="光学式", parent_group_id=uuid.uuid4(),
            )

    def test_rejects_parent_from_other_tenant(self, db_session, tenant_id):
        other_tenant = uuid.uuid4()
        from tests.conftest import set_tenant_context

        set_tenant_context(db_session, other_tenant)
        other_parent = pg.create_product_group(db_session, other_tenant, name="他社グループ")
        db_session.flush()
        set_tenant_context(db_session, tenant_id)

        with pytest.raises(ValueError):
            pg.create_product_group(
                db_session, tenant_id, name="光学式", parent_group_id=other_parent.id,
            )


class TestListProductGroups:
    def test_scoped_to_tenant_and_ordered_by_name(self, db_session, tenant_id):
        pg.create_product_group(db_session, tenant_id, name="B")
        pg.create_product_group(db_session, tenant_id, name="A")
        db_session.commit()

        groups = pg.list_product_groups(db_session, tenant_id)
        assert [g.name for g in groups] == ["A", "B"]


class TestListProductGroupsTreeOrdered:
    def test_children_follow_their_parent(self, db_session, tenant_id):
        inspection = pg.create_product_group(db_session, tenant_id, name="検査装置")
        pg.create_product_group(db_session, tenant_id, name="標準機", parent_group_id=inspection.id)
        pg.create_product_group(db_session, tenant_id, name="オプション", parent_group_id=inspection.id)
        pg.create_product_group(db_session, tenant_id, name="消耗品")
        db_session.commit()

        ordered = pg.list_product_groups_tree_ordered(db_session, tenant_id)
        names = [g.name for g in ordered]
        assert names == ["検査装置", "オプション", "標準機", "消耗品"]


class TestGetFamilyProductGroupIds:
    def test_includes_self_and_children(self, db_session, tenant_id):
        parent = pg.create_product_group(db_session, tenant_id, name="検査装置")
        child = pg.create_product_group(
            db_session, tenant_id, name="標準機", parent_group_id=parent.id,
        )
        other = pg.create_product_group(db_session, tenant_id, name="消耗品")
        db_session.commit()

        family = pg.get_family_product_group_ids(db_session, tenant_id, parent.id)
        assert family == {parent.id, child.id}
        assert other.id not in family

    def test_leaf_group_returns_only_itself(self, db_session, tenant_id):
        parent = pg.create_product_group(db_session, tenant_id, name="検査装置")
        child = pg.create_product_group(
            db_session, tenant_id, name="標準機", parent_group_id=parent.id,
        )
        db_session.commit()

        assert pg.get_family_product_group_ids(db_session, tenant_id, child.id) == {child.id}
