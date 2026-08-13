from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crm_mvp.enums import Confidence, Criterion
from crm_mvp.models import QualificationSlot

# §7.4: crm_app は非 superuser ロール。RLS は superuser を無条件に
# 素通りするため、admin ロール(owner)で接続すると分離が検証されない
# まま通ってしまう。テストも本番同様 crm_app 経由で RLS を通す。
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
)


@pytest.fixture
def new_id():
    return lambda: uuid.uuid4()


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


def set_tenant_context(session: Session, tenant_id: uuid.UUID) -> None:
    """§7.4: RLS のテナント文脈を明示的に切り替える。

    db_session は tenant_id フィクスチャの値で自動設定されるが、1テスト内で
    複数テナントを扱う場合(例: test_seed_policies_db.py の独立性テスト)は
    この関数で都度切り替えること。
    """
    # SET は値にバインドパラメータを取れないため set_config() を使う。
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, false)"),
        {"tid": str(tenant_id)},
    )


@pytest.fixture
def db_session(tenant_id):
    """外側トランザクションをテスト後に必ずロールバックする DB セッション。

    §7.4: RLS が参照する app.current_tenant_id を tenant_id フィクスチャの
    値で既定設定する。PostgreSQL に接続できない環境では自動的にスキップする。
    """
    try:
        engine = create_engine(DATABASE_URL)
        connection = engine.connect()
    except Exception as exc:  # pragma: no cover - 環境依存
        pytest.skip(f"PostgreSQL に接続できません: {exc}")

    trans = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    set_tenant_context(session, tenant_id)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def api_client(db_session, tenant_id):
    """DB に触れる API テスト用の TestClient。get_session を db_session に
    差し替え、X-Tenant-Id を既定ヘッダとして付与する。"""
    from fastapi.testclient import TestClient

    from crm_mvp.api import deps
    from crm_mvp.api.app import app

    app.dependency_overrides[deps.get_session] = lambda: db_session
    client = TestClient(app, headers={"X-Tenant-Id": str(tenant_id)})
    try:
        yield client
    finally:
        app.dependency_overrides.pop(deps.get_session, None)
        app.dependency_overrides.pop(deps.get_extractor, None)
        app.dependency_overrides.pop(deps.get_screening_port, None)


def make_slot(
    criterion: Criterion,
    confidence: Confidence,
    value: dict | None = None,
    decays_at=None,
) -> QualificationSlot:
    """未永続化の QualificationSlot。DB なしで .meets() のロジックだけを使う。"""
    return QualificationSlot(
        criterion=criterion,
        confidence=confidence,
        value=value if value is not None else {"present": True},
        decays_at=decays_at,
    )


def create_account_and_engagement(session, tenant_id: uuid.UUID, stage=None):
    """テスト用に Account + Engagement を1件ずつ永続化して返す。"""
    from crm_mvp.enums import Stage
    from crm_mvp.models import Account, Engagement

    account = Account(tenant_id=tenant_id, name="テスト株式会社")
    session.add(account)
    session.flush()

    engagement = Engagement(
        tenant_id=tenant_id, account_id=account.id, name="テスト案件",
        stage=stage or Stage.LEAD,
    )
    session.add(engagement)
    session.flush()
    return account, engagement
