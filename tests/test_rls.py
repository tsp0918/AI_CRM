"""§7.4: Row-Level Security の実効性を直接証明するテスト。

他のテストは apply_proposal / API 経由の間接的な検証だが、ここでは
生の SELECT/INSERT で RLS ポリシーそのものを検証する。crm_app が
superuser でないこと自体も確認する(superuser は RLS を無条件に
素通りするため、このロールで検証する意味が消えてしまう)。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from .conftest import set_tenant_context


def test_crm_app_role_is_not_superuser(db_session):
    row = db_session.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False


def test_unset_tenant_context_sees_zero_rows(db_session, tenant_id):
    from crm_mvp.models import Account

    db_session.add(Account(tenant_id=tenant_id, name="見えるはずのテナント"))
    db_session.flush()

    db_session.execute(text("SELECT set_config('app.current_tenant_id', '', true)"))
    count = db_session.execute(text("SELECT count(*) FROM account")).scalar()
    assert count == 0


def test_insert_for_other_tenant_is_rejected(db_session, tenant_id):
    from sqlalchemy.exc import ProgrammingError

    other_tenant = uuid.uuid4()
    with pytest.raises(ProgrammingError, match="row-level security"):
        db_session.execute(
            text(
                "INSERT INTO account (id, tenant_id, name, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :tid, 'x', now(), now())"
            ),
            {"tid": str(other_tenant)},
        )


def test_select_only_returns_current_tenant_rows(db_session, tenant_id):
    from crm_mvp.models import Account

    db_session.add(Account(tenant_id=tenant_id, name="自テナント"))
    db_session.flush()

    other_tenant = uuid.uuid4()
    set_tenant_context(db_session, other_tenant)
    db_session.add(Account(tenant_id=other_tenant, name="他テナント"))
    db_session.flush()

    # other_tenant のコンテキストなので自テナントの行は見えない
    names = db_session.execute(text("SELECT name FROM account")).scalars().all()
    assert names == ["他テナント"]

    set_tenant_context(db_session, tenant_id)
    names = db_session.execute(text("SELECT name FROM account")).scalars().all()
    assert names == ["自テナント"]
