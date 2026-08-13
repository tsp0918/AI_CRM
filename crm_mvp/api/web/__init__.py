"""FastAPI + Jinja2Templates による SSR フロントエンド(HANDOVER.md §5
Phase4-16)。JSON API(sources.py / proposals.py / engagements.py 等、
web/ の外側にある同名モジュール)はそのまま外部連携用に残し、
このパッケージは画面表示専用の別経路として並走させる。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    dashboard, engagements, forecast_risk, graph, leads, proposals, sources,
    workspace,
)
from .session import WorkspaceRequired

router = APIRouter()
router.include_router(workspace.router)
router.include_router(dashboard.router)
router.include_router(engagements.router)
router.include_router(proposals.router)
router.include_router(sources.router)
router.include_router(graph.router)
router.include_router(forecast_risk.router)
router.include_router(leads.router)

__all__ = ["router", "WorkspaceRequired"]
