"""services/erp_materials.py のユニットテスト。"""

from __future__ import annotations

from decimal import Decimal

from crm_mvp.services import erp_materials as em


class TestUpsertErpMaterial:
    def test_creates_new_material(self, db_session, tenant_id):
        material = em.upsert_erp_material(
            db_session, tenant_id, material_code="MAT-0001",
            description="ArFフォトレジスト", material_type="ROH", base_unit="L",
            standard_price=Decimal("50000"), currency="JPY",
        )
        db_session.commit()

        assert material.material_code == "MAT-0001"
        assert material.material_type == "ROH"
        assert material.standard_price == Decimal("50000")

    def test_reimporting_same_code_updates_in_place(self, db_session, tenant_id):
        em.upsert_erp_material(
            db_session, tenant_id, material_code="MAT-0001", description="旧説明",
            material_type="FERT", base_unit="PC", standard_price=Decimal("100000"),
            currency="JPY",
        )
        db_session.commit()

        updated = em.upsert_erp_material(
            db_session, tenant_id, material_code="MAT-0001", description="新説明",
            material_type="FERT", base_unit="PC", standard_price=Decimal("120000"),
            currency="JPY",
        )
        db_session.commit()

        materials = em.list_erp_materials(db_session, tenant_id)
        assert len(materials) == 1
        assert updated.description == "新説明"
        assert updated.standard_price == Decimal("120000")

    def test_defaults_fefta_judgment_to_unknown(self, db_session, tenant_id):
        material = em.upsert_erp_material(
            db_session, tenant_id, material_code="MAT-0002", description="装置",
            material_type="FERT", base_unit="PC", standard_price=Decimal("1000"),
            currency="JPY",
        )
        db_session.commit()

        assert material.fefta_judgment == "UNKNOWN"


class TestListErpMaterials:
    def test_scoped_to_tenant_and_ordered_by_code(self, db_session, tenant_id):
        em.upsert_erp_material(
            db_session, tenant_id, material_code="MAT-0002", description="B",
            material_type="FERT", base_unit="PC", standard_price=Decimal("1"),
            currency="JPY",
        )
        em.upsert_erp_material(
            db_session, tenant_id, material_code="MAT-0001", description="A",
            material_type="FERT", base_unit="PC", standard_price=Decimal("1"),
            currency="JPY",
        )
        db_session.commit()

        materials = em.list_erp_materials(db_session, tenant_id)
        assert [m.material_code for m in materials] == ["MAT-0001", "MAT-0002"]
