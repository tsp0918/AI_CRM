"""services/erp_business_partners.py のユニットテスト。"""

from __future__ import annotations

from decimal import Decimal

from crm_mvp.services import erp_business_partners as ebp


class TestUpsertErpBusinessPartner:
    def test_creates_new_partner(self, db_session, tenant_id):
        partner = ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-3000001", name="Apex Foundry Corporation",
            country="TW", roles="CUSTOMER", currency="USD", credit_limit=Decimal("20000000"),
        )
        db_session.commit()

        assert partner.bp_code == "BP-3000001"
        assert partner.country == "TW"
        assert partner.credit_limit == Decimal("20000000")

    def test_reimporting_same_code_updates_in_place(self, db_session, tenant_id):
        ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-1000001", name="旧名称", roles="CUSTOMER",
        )
        db_session.commit()

        updated = ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-1000001", name="新名称", roles="CUSTOMER,VENDOR",
        )
        db_session.commit()

        partners = ebp.list_erp_business_partners(db_session, tenant_id)
        assert len(partners) == 1
        assert updated.name == "新名称"
        assert updated.roles == "CUSTOMER,VENDOR"

    def test_defaults_is_denied_party_false(self, db_session, tenant_id):
        partner = ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-9999999", name="テスト取引先",
        )
        db_session.commit()

        assert partner.is_denied_party is False


class TestListErpBusinessPartners:
    def test_scoped_to_tenant_and_ordered_by_code(self, db_session, tenant_id):
        ebp.upsert_erp_business_partner(db_session, tenant_id, bp_code="BP-2", name="B")
        ebp.upsert_erp_business_partner(db_session, tenant_id, bp_code="BP-1", name="A")
        db_session.commit()

        partners = ebp.list_erp_business_partners(db_session, tenant_id)
        assert [p.bp_code for p in partners] == ["BP-1", "BP-2"]

    def test_filters_by_role(self, db_session, tenant_id):
        ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-1", name="顧客", roles="CUSTOMER",
        )
        ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-2", name="仕入先", roles="VENDOR",
        )
        ebp.upsert_erp_business_partner(
            db_session, tenant_id, bp_code="BP-3", name="両方", roles="CUSTOMER,VENDOR",
        )
        db_session.commit()

        customers = ebp.list_erp_business_partners(db_session, tenant_id, role="CUSTOMER")
        assert {p.bp_code for p in customers} == {"BP-1", "BP-3"}
