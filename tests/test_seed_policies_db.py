"""seed_policies の投入関数 — 実 DB に対する冪等性の検証。

外側トランザクションはテスト終了時に必ずロールバックするため、
crm_mvp データベースに副作用は残らない（tests/conftest.py の db_session 参照）。

§7.4: RLS 下では書き込み先の tenant_id と session の
app.current_tenant_id が一致しない INSERT は拒否される。複数テナストを
1テスト内で扱うたびに set_tenant_context で明示的に切り替える。
"""

from __future__ import annotations

import uuid

from crm_mvp.services.seed_policies import (
    DEFAULT_AUTONOMY, MANUFACTURING_TEMPLATE, upsert_default_autonomy,
    upsert_gate_policies,
)

from .conftest import set_tenant_context


class TestUpsertGatePolicies:
    def test_inserts_all_template_rows(self, db_session, tenant_id):
        count = upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template",
        )
        assert count == len(MANUFACTURING_TEMPLATE)

    def test_is_idempotent(self, db_session, tenant_id):
        upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template",
        )
        second = upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template",
        )
        assert second == 0

    def test_different_industry_templates_are_independent(self, db_session, tenant_id):
        upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template-a",
        )
        count_b = upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template-b",
        )
        assert count_b == len(MANUFACTURING_TEMPLATE)

    def test_different_tenants_are_independent(self, db_session, tenant_id):
        upsert_gate_policies(
            db_session, tenant_id=tenant_id, industry_template="test-template",
        )
        # upsert は呼び出し側の commit/flush 任せの設計(CLI は直後に commit する)。
        # テナント文脈を切り替える前に、直前の書き込みを確定させておく。
        db_session.flush()

        tenant_2 = uuid.uuid4()
        set_tenant_context(db_session, tenant_2)
        count_t2 = upsert_gate_policies(
            db_session, tenant_id=tenant_2, industry_template="test-template",
        )
        assert count_t2 == len(MANUFACTURING_TEMPLATE)


class TestUpsertDefaultAutonomy:
    def test_inserts_all_default_rows(self, db_session, tenant_id):
        count = upsert_default_autonomy(db_session, tenant_id=tenant_id)
        assert count == len(DEFAULT_AUTONOMY)

    def test_is_idempotent_per_tenant(self, db_session, tenant_id):
        upsert_default_autonomy(db_session, tenant_id=tenant_id)
        second = upsert_default_autonomy(db_session, tenant_id=tenant_id)
        assert second == 0

    def test_different_tenants_are_independent(self, db_session, tenant_id):
        upsert_default_autonomy(db_session, tenant_id=tenant_id)
        db_session.flush()

        tenant_2 = uuid.uuid4()
        set_tenant_context(db_session, tenant_2)
        count_t2 = upsert_default_autonomy(db_session, tenant_id=tenant_2)
        assert count_t2 == len(DEFAULT_AUTONOMY)
