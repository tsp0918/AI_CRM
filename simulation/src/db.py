"""CRM DBへの直接アクセス(`scripts/seed_demo_pipeline.py`と同じ確立済み
パターン)。Account/Product/ErpMaterial等のマスタCRUDは公開JSON APIの
スコープ外のため、シミュレーションのマスタ投入(P2)ではこの経路を使う。

`tenant_session()`は1回のフローの中で何度も呼ばれる(P4規模では1商談
あたり10回前後)。以前は呼び出しごとに`create_engine()`していたため、
60商談規模の実行でPostgreSQLの接続上限(max_connections)を使い切って
`FATAL: remaining connection slots are reserved...`で全滅した
(2026-08-16のP4実行で発覚) — engineはプロセス内で使い回す。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

DEFAULT_DATABASE_URL = "postgresql+psycopg://crm_app@localhost:5432/crm_mvp"

_engines: dict[str, "sqlalchemy.engine.Engine"] = {}  # noqa: F821


def _get_engine(database_url: str | None):
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)
        _engines[url] = engine
    return engine


@contextmanager
def tenant_session(tenant_id: uuid.UUID, database_url: str | None = None):
    engine = _get_engine(database_url)
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, false)"),
            {"tid": str(tenant_id)},
        )
        yield session
