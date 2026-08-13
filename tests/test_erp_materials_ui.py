"""ERP品目マスタ・商品グループ・商品(価格表)拡張UIの統合テスト。"""

from __future__ import annotations

from decimal import Decimal

from crm_mvp.models import ErpMaterial, Product, ProductGroup


class TestErpMaterialsPage:
    def test_import_creates_material(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/erp-materials/import",
            data={
                "material_code": "MAT-0001", "description": "ArFフォトレジスト",
                "material_type": "ROH", "base_unit": "L", "standard_price": "50000",
                "currency": "JPY",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        material = db_session.query(ErpMaterial).filter_by(
            tenant_id=tenant_id, material_code="MAT-0001",
        ).one()
        assert material.standard_price == Decimal("50000")

        resp = ui_client.get("/ui/erp-materials")
        assert resp.status_code == 200
        assert "ArFフォトレジスト" in resp.text

    def test_reimport_upserts_in_place(self, ui_client, db_session, tenant_id):
        ui_client.post(
            "/ui/erp-materials/import",
            data={
                "material_code": "MAT-0002", "description": "旧説明",
                "material_type": "FERT", "base_unit": "PC", "standard_price": "100000",
                "currency": "JPY",
            },
        )
        ui_client.post(
            "/ui/erp-materials/import",
            data={
                "material_code": "MAT-0002", "description": "新説明",
                "material_type": "FERT", "base_unit": "PC", "standard_price": "120000",
                "currency": "JPY",
            },
        )
        materials = db_session.query(ErpMaterial).filter_by(
            tenant_id=tenant_id, material_code="MAT-0002",
        ).all()
        assert len(materials) == 1
        assert materials[0].description == "新説明"

    def test_blank_code_is_rejected(self, ui_client):
        resp = ui_client.post(
            "/ui/erp-materials/import",
            data={
                "material_code": "  ", "description": "x", "material_type": "FERT",
                "standard_price": "1", "currency": "JPY",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]


class TestProductGroups:
    def test_creates_top_level_group(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/product-groups/new", data={"name": "検査装置"}, follow_redirects=False,
        )
        assert resp.status_code == 303

        group = db_session.query(ProductGroup).filter_by(tenant_id=tenant_id, name="検査装置").one()
        assert group.parent_group_id is None

    def test_creates_child_group(self, ui_client, db_session, tenant_id):
        ui_client.post("/ui/product-groups/new", data={"name": "検査装置"})
        parent = db_session.query(ProductGroup).filter_by(tenant_id=tenant_id, name="検査装置").one()

        resp = ui_client.post(
            "/ui/product-groups/new",
            data={"name": "光学式", "parent_group_id": str(parent.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        child = db_session.query(ProductGroup).filter_by(tenant_id=tenant_id, name="光学式").one()
        assert child.parent_group_id == parent.id

    def test_products_page_shows_group_hierarchy(self, ui_client, db_session, tenant_id):
        ui_client.post("/ui/product-groups/new", data={"name": "検査装置"})

        resp = ui_client.get("/ui/products")
        assert resp.status_code == 200
        assert "検査装置" in resp.text


class TestProductFromErpMaterial:
    def test_create_product_linked_to_erp_material_shows_margin(
        self, ui_client, db_session, tenant_id,
    ):
        ui_client.post(
            "/ui/erp-materials/import",
            data={
                "material_code": "MAT-0003", "description": "検査装置 標準モデル",
                "material_type": "FERT", "base_unit": "PC", "standard_price": "700000",
                "currency": "JPY",
            },
        )
        material = db_session.query(ErpMaterial).filter_by(
            tenant_id=tenant_id, material_code="MAT-0003",
        ).one()

        resp = ui_client.post(
            "/ui/products/new",
            data={
                "name": "検査装置 標準モデル", "list_price": "1000000", "currency": "JPY",
                "erp_material_id": str(material.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        product = db_session.query(Product).filter_by(
            tenant_id=tenant_id, name="検査装置 標準モデル",
        ).one()
        assert product.erp_material_id == material.id

        resp = ui_client.get("/ui/products")
        assert resp.status_code == 200
        assert "30.00%" in resp.text

    def test_dummy_product_without_erp_material_still_works(self, ui_client, db_session, tenant_id):
        resp = ui_client.post(
            "/ui/products/new",
            data={"name": "保守サポート(年間)", "list_price": "200000", "currency": "JPY"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        product = db_session.query(Product).filter_by(
            tenant_id=tenant_id, name="保守サポート(年間)",
        ).one()
        assert product.erp_material_id is None

    def test_unknown_erp_material_id_is_rejected(self, ui_client):
        import uuid
        resp = ui_client.post(
            "/ui/products/new",
            data={
                "name": "検査装置", "list_price": "1000000", "currency": "JPY",
                "erp_material_id": str(uuid.uuid4()),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "flash_type=error" in resp.headers["location"]
