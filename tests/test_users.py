"""services/users.py のユニットテスト。"""

from __future__ import annotations

from crm_mvp.enums import AuthorityLevel
from crm_mvp.services import users as us


class TestUpsertUser:
    def test_creates_new_user(self, db_session, tenant_id):
        user = us.upsert_user(
            db_session, tenant_id, name="Seiichi Miura", email="seiichi.miura@ts-materials.com",
            function="Board", role="Managing Director", authority=AuthorityLevel.APPROVER_SUPER,
        )
        db_session.commit()

        assert user.name == "Seiichi Miura"
        assert user.authority == "approver_super"

    def test_reimporting_same_email_updates_in_place(self, db_session, tenant_id):
        us.upsert_user(
            db_session, tenant_id, name="旧名前", email="test@ts-materials.com",
            function="Sales", role="AM",
        )
        db_session.commit()

        updated = us.upsert_user(
            db_session, tenant_id, name="新名前", email="TEST@ts-materials.com",
            function="Sales", role="KAM", authority=AuthorityLevel.APPROVER,
        )
        db_session.commit()

        users = us.list_users(db_session, tenant_id)
        assert len(users) == 1
        assert updated.name == "新名前"
        assert updated.role == "KAM"

    def test_defaults_authority_to_none(self, db_session, tenant_id):
        user = us.upsert_user(
            db_session, tenant_id, name="X", email="x@ts-materials.com",
            function="Sales", role="CS",
        )
        db_session.commit()
        assert user.authority == "none"


class TestListUsers:
    def test_scoped_to_tenant_ordered_by_function_then_name(self, db_session, tenant_id):
        us.upsert_user(db_session, tenant_id, name="B", email="b@x.com", function="Sales", role="AM")
        us.upsert_user(db_session, tenant_id, name="A", email="a@x.com", function="Board", role="MD")
        db_session.commit()

        users = us.list_users(db_session, tenant_id)
        assert [u.name for u in users] == ["A", "B"]
