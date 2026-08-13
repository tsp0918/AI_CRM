"""DB エンジン・セッション。

§7.4: テナント分離は Row-Level Security(RLS)に確定(2026-08-13)。
アプリケーションは非 superuser ロール crm_app で接続する
— RLS は superuser を無条件に素通りするため、実際に隔離を効かせるには
非 superuser 接続が必須(scripts/provision_app_role.sql 参照)。
Alembic マイグレーション(DDL)は引き続き所有者ロールで接続すること。

各リクエストの tenant_id を RLS ポリシーに渡す SET LOCAL は
crm_mvp/api/deps.py の get_tenant_scoped_session が担う。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
