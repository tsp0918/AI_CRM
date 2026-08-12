"""FastAPI アプリケーション組み立て。

認証は MVP スコープ外(README.md「MVP に含めていないもの」参照)。
"""

from __future__ import annotations

from fastapi import FastAPI

from . import accounts, engagements, proposals, sources, webhooks


def create_app() -> FastAPI:
    app = FastAPI(title="Compliance-aware Agentic CRM (MVP)")
    app.include_router(sources.router)
    app.include_router(proposals.router)
    app.include_router(engagements.router)
    app.include_router(accounts.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
