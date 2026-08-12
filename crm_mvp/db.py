"""DB エンジン・セッション。

テナント分離は §7.4 で未決のため、アプリケーション層のフィルタリング
(各クエリで明示的に WHERE tenant_id = ...)のみを前提とする。
RLS 等の DB レベル隔離を導入する際もこの層はそのまま使える。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://localhost:5432/crm_mvp"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
