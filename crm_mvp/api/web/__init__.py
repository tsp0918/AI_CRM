"""FastAPI + Jinja2Templates による SSR フロントエンド(HANDOVER.md §5
Phase4-16)。JSON API(sources.py / proposals.py / engagements.py 等、
web/ の外側にある同名モジュール)はそのまま外部連携用に残し、
このパッケージは画面表示専用の別経路として並走させる。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    accounts, campaigns, dashboard, engagements, erp_business_partners,
    erp_materials, forecast_risk, graph, integration_status, leads, products,
    proposals, quotes, renewals, report_snapshots, reports, rnd_opportunities,
    sales_groups, sequences, sources, users, workspace,
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
router.include_router(sequences.router)
router.include_router(campaigns.router)
router.include_router(erp_business_partners.router)
router.include_router(erp_materials.router)
router.include_router(products.router)
router.include_router(quotes.router)
router.include_router(renewals.router)
router.include_router(accounts.router)
router.include_router(sales_groups.router)
router.include_router(reports.router)
router.include_router(report_snapshots.router)
router.include_router(users.router)
router.include_router(integration_status.router)
router.include_router(rnd_opportunities.router)

__all__ = ["router", "WorkspaceRequired"]
