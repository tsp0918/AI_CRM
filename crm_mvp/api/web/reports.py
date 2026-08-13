"""売上レポート(2026-08-13 ユーザー要望)。

商品グループ・セールスグループ・取引先(法人グループのロールアップ)・
関係性(新規/更新/Upsell/Cross-sell)の4軸でclosed_won商談の実売上を集計する。
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...services.revenue_report import (
    RELATIONSHIP_TYPE_REPORT_LABELS, aggregate_by, closed_won_revenue_rows,
    product_group_revenue,
)
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/reports/revenue", response_class=HTMLResponse)
def revenue_report_page(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    rows = closed_won_revenue_rows(session, ui_session.tenant_id)

    total_revenue = sum((r["amount"] for r in rows), Decimal("0"))

    by_account = aggregate_by(
        rows, lambda r: r["root_account"].name if r["root_account"] else "—",
    )
    by_sales_group = aggregate_by(
        rows, lambda r: r["sales_group"].name if r["sales_group"] else "未設定",
    )
    by_relationship = aggregate_by(
        rows,
        lambda r: RELATIONSHIP_TYPE_REPORT_LABELS.get(
            r["relationship_type"], r["relationship_type"],
        ),
    )
    by_product_group = product_group_revenue(session, ui_session.tenant_id)

    context = base_context(session, ui_session, active_nav="revenue_report")
    context.update({
        "total_revenue": total_revenue, "deal_count": len(rows),
        "by_account": by_account, "by_sales_group": by_sales_group,
        "by_relationship": by_relationship, "by_product_group": by_product_group,
    })
    return templates.TemplateResponse(request, "revenue_report.html", context)
