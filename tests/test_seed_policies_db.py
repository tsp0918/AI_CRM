"""seed_policies の投入関数 — 実 DB に対する冪等性の検証。

外側トランザクションはテスト終了時に必ずロールバックするため、
crm_mvp データベースに副作用は残らない（tests/conftest.py の db_session 参照）。
"""

from __future__ import annotations

import uuid

from crm_mvp.services.seed_policies import (
    DEFAULT_AUTONOMY, MANUFACTURING_TEMPLATE, upsert_default_autonomy,
    upsert_gate_policies,
)


class TestUpsertGatePolicies:
    def test_inserts_all_template_rows(self, db_session):
        count = upsert_gate_policies(
            db_session, tenant_id=uuid.uuid4(), industry_template="test-template",
        )
        assert count == len(MANUFACTURING_TEMPLATE)

    def test_is_idempotent(self, db_session):
        tenant_id = uuid.uuid4()
        upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template",
        )
        second = upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template",
        )
        assert second == 0

    def test_different_industry_templates_are_independent(self, db_session):
        tenant_id = uuid.uuid4()
        upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template-a",
        )
        count_b = upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template-b",
        )
        assert count_b == len(MANUFACTURING_TEMPLATE)

    def test_different_tenants_are_independent(self, db_session):
        upsert_gate_policies(
            db_session, tenant_id=uuid.uuid4(), industry_template="test-template",
        )
        count_t2 = upsert_gate_policies(
            db_session, tenant_id=uuid.uuid4(), industry_template="test-template",
        )
        assert count_t2 == len(MANUFACTURING_TEMPLATE)


class TestUpsertDefaultAutonomy:
    def test_inserts_all_default_rows(self, db_session):
        count = upsert_default_autonomy(db_session, tenant_id=uuid.uuid4())
        assert count == len(DEFAULT_AUTONOMY)

    def test_is_idempotent_per_tenant(self, db_session):
        tenant_id = uuid.uuid4()
        upsert_default_autonomy(db_session, tenant_id=tenant_id)
        second = upsert_default_autonomy(db_session, tenant_id=tenant_id)
        assert second == 0

    def test_different_tenants_are_independent(self, db_session):
        upsert_default_autonomy(db_session, tenant_id=uuid.uuid4())
        count_t2 = upsert_default_autonomy(db_session, tenant_id=uuid.uuid4())
        assert count_t2 == len(DEFAULT_AUTONOMY)
