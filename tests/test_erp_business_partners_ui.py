"""ERP取引先マスタUIの統合テスト。"""

from __future__ import annotations

from crm_mvp.models import ErpBusinessPartner


class TestErpBusinessPartnersPage:
    def test_import_creates_partner(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/erp-business-partners/import",
            data={
                "bp_code": "BP-3000001", "name": "Apex Foundry Corporation",
                "country": "TW", "roles": "CUSTOMER", "currency": "USD",
                "credit_limit": "20000000",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        partner = db_session.query(ErpBusinessPartner).filter_by(
            tenant_id=tenant_id, bp_code="BP-3000001",
        ).one()
        assert partner.country == "TW"

        resp = ui_client.get("/ui/erp-business-partners")
        assert resp.status_code == 200
        assert "Apex Foundry Corporation" in resp.text

    def test_reimport_upserts_in_place(self, ui_client, db_session, tenant_id):
        ui_client.post(
            "/ui/erp-business-partners/import",
            data={"bp_code": "BP-1", "name": "旧名称", "roles": "CUSTOMER"},
        )
        ui_client.post(
            "/ui/erp-business-partners/import",
            data={"bp_code": "BP-1", "name": "新名称", "roles": "CUSTOMER"},
        )
        partners = db_session.query(ErpBusinessPartner).filter_by(
            tenant_id=tenant_id, bp_code="BP-1",
        ).all()
        assert len(partners) == 1
        assert partners[0].name == "新名称"

    def test_blank_code_is_rejected(self, ui_client):
        resp = ui_client.post(
            "/ui/erp-business-partners/import",
            data={"bp_code": "  ", "name": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]

    def test_denied_party_shows_block_badge(self, ui_client, db_session):
        ui_client.post(
            "/ui/erp-business-partners/import",
            data={
                "bp_code": "BP-9", "name": "制限対象企業", "roles": "CUSTOMER",
                "is_denied_party": "true",
            },
        )
        resp = ui_client.get("/ui/erp-business-partners")
        assert resp.status_code == 200
        assert "取引制限" in resp.text
