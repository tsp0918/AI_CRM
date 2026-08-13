"""Contact 登録・案件紐付けのテスト。"""

from __future__ import annotations

from crm_mvp.enums import AccessLevel, BuyingCenterRole, Stance
from crm_mvp.models import Contact, EngagementRole, GraphNode
from crm_mvp.services.contacts import register_contact_and_link

from .conftest import create_account_and_engagement


class TestRegisterContactAndLink:
    def test_creates_contact_node_and_role(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)

        contact, role = register_contact_and_link(
            db_session, tenant_id, engagement,
            full_name="山田 太郎", title="購買部長", org_unit="購買部",
            email="yamada@example.com",
            roles=[BuyingCenterRole.CHAMPION.value], stance=Stance.UNKNOWN,
            access_level=AccessLevel.CONTACTED, written_by="human:tester",
        )
        db_session.commit()

        assert contact.full_name == "山田 太郎"
        assert contact.account_id == engagement.account_id

        node = db_session.query(GraphNode).filter_by(
            tenant_id=tenant_id, contact_id=contact.id,
        ).one()
        assert node.org_unit == "購買部"

        assert role.node_id == node.id
        assert role.access_level == AccessLevel.CONTACTED
        assert role.roles == [BuyingCenterRole.CHAMPION.value]

    def test_reuses_existing_node_when_linking_again(self, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        contact, _ = register_contact_and_link(
            db_session, tenant_id, engagement, full_name="鈴木 花子",
            roles=[], stance=Stance.UNKNOWN, access_level=AccessLevel.CONTACTED,
            written_by="human:tester",
        )
        db_session.flush()

        from crm_mvp.services.contacts import (
            get_or_create_node_for_contact, link_contact_to_engagement,
        )
        node_again = get_or_create_node_for_contact(
            db_session, tenant_id, engagement.account_id, contact,
        )
        link_contact_to_engagement(
            db_session, tenant_id, engagement, node_again,
            roles=[BuyingCenterRole.USER.value], stance=Stance.SUPPORTER,
            access_level=AccessLevel.ENGAGED, written_by="human:tester2",
        )
        db_session.commit()

        nodes = db_session.query(GraphNode).filter_by(
            tenant_id=tenant_id, contact_id=contact.id,
        ).all()
        assert len(nodes) == 1  # 重複作成されない

        roles = db_session.query(EngagementRole).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node_again.id,
        ).all()
        assert len(roles) == 1
        assert roles[0].access_level == AccessLevel.ENGAGED  # 更新されている


class TestAddContactUi:
    def test_registers_contact_via_form(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/graph/contacts",
            data={
                "full_name": "田中 次郎", "title": "工場長",
                "org_unit": "製造部", "role": "decider",
                "access_level": "engaged",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "graph" in resp.headers["location"]

        contact = db_session.query(Contact).filter_by(
            tenant_id=tenant_id, full_name="田中 次郎",
        ).one()
        node = db_session.query(GraphNode).filter_by(
            tenant_id=tenant_id, contact_id=contact.id,
        ).one()
        role = db_session.query(EngagementRole).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node.id,
        ).one()
        assert role.access_level == AccessLevel.ENGAGED
        assert role.roles == ["decider"]

    def test_blank_name_is_rejected(self, ui_client, db_session, tenant_id):
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        resp = ui_client.post(
            f"/ui/engagements/{engagement.id}/graph/contacts",
            data={"full_name": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
