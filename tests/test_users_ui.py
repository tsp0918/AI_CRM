"""/ui/users のUIテスト。"""

from __future__ import annotations

from crm_mvp.enums import AuthorityLevel
from crm_mvp.services.users import upsert_user


class TestUsersPage:
    def test_renders_roster(self, ui_client, db_session, tenant_id):
        upsert_user(
            db_session, tenant_id, name="Seiichi Miura", email="seiichi.miura@ts-materials.com",
            function="Board", role="Managing Director", authority=AuthorityLevel.APPROVER_SUPER,
        )
        db_session.commit()

        resp = ui_client.get("/ui/users")
        assert resp.status_code == 200
        assert "Seiichi Miura" in resp.text
        assert "Approver-Super" in resp.text

    def test_empty_state(self, ui_client):
        resp = ui_client.get("/ui/users")
        assert resp.status_code == 200
