"""FastAPI アプリケーション組み立て。

認証は MVP スコープ外(README.md「MVP に含めていないもの」参照)。
フロントエンドは FastAPI + Jinja2Templates によるサーバーサイド
レンダリングに統一する(web/ パッケージ)。JSON API(sources/proposals/
engagements/accounts/webhooks)は外部連携用にそのまま残す。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from . import accounts, engagements, proposals, sources, web, webhooks
from .web.session import WorkspaceRequired


def create_app() -> FastAPI:
    app = FastAPI(title="Compliance-aware Agentic CRM (MVP)")
    app.include_router(sources.router)
    app.include_router(proposals.router)
    app.include_router(engagements.router)
    app.include_router(accounts.router)
    app.include_router(webhooks.router)
    app.include_router(web.router)

    @app.exception_handler(WorkspaceRequired)
    async def _redirect_to_workspace(
        request: Request, exc: WorkspaceRequired
    ) -> RedirectResponse:
        return RedirectResponse(
            url=f"/ui/workspace?next={quote(exc.next_url, safe='')}",
            status_code=303,
        )

    return app


app = create_app()
