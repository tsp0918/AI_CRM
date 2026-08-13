"""売上レポート(2026-08-13 ユーザー要望)。

商品グループ・セールスグループ・取引先(法人グループのロールアップ)・
関係性(新規/更新/Upsell/Cross-sell)の4軸でclosed_won商談の実売上を集計する。

2026-08-14: 各集計行をクリックすると、その軸で絞り込んだ商品別内訳・
該当商談一覧に遷移できるドリルダウンを追加(平面的なレポートで終わらせ
ず、紐づいている内容へ分解して辿れるようにする、というユーザー要望)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...services.revenue_report import (
    RELATIONSHIP_TYPE_REPORT_LABELS, aggregate_by, closed_won_revenue_rows,
    facts_by_engagement, filter_facts, line_item_facts, product_group_revenue,
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
        lambda r: r["root_account"].id if r["root_account"] else None,
    )
    by_sales_group = aggregate_by(
        rows, lambda r: r["sales_group"].name if r["sales_group"] else "未設定",
        lambda r: r["sales_group"].id if r["sales_group"] else None,
    )
    by_relationship = aggregate_by(
        rows,
        lambda r: RELATIONSHIP_TYPE_REPORT_LABELS.get(
            r["relationship_type"], r["relationship_type"],
        ),
        lambda r: r["relationship_type"],
    )
    by_product_group = product_group_revenue(session, ui_session.tenant_id)

    context = base_context(session, ui_session, active_nav="revenue_report")
    context.update({
        "total_revenue": total_revenue, "deal_count": len(rows),
        "by_account": by_account, "by_sales_group": by_sales_group,
        "by_relationship": by_relationship, "by_product_group": by_product_group,
    })
    return templates.TemplateResponse(request, "revenue_report.html", context)


@router.get("/ui/reports/revenue/drill-down", response_class=HTMLResponse)
def revenue_drilldown_page(
    request: Request,
    product_group_id: str = "",
    sales_group_id: str = "",
    relationship_type: str = "",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    facts = line_item_facts(session, ui_session.tenant_id)
    filtered = filter_facts(
        facts,
        product_group_id=uuid.UUID(product_group_id) if product_group_id else None,
        sales_group_id=uuid.UUID(sales_group_id) if sales_group_id else None,
        relationship_type=relationship_type or None,
    )

    total_revenue = sum((f["amount"] for f in filtered), Decimal("0"))
    by_product = aggregate_by(
        filtered, lambda f: f["product"].name if f["product"] else "未分類",
    )
    deals = facts_by_engagement(filtered)

    filter_labels = []
    if product_group_id:
        sample = next((f for f in filtered if f["product_group"]), None)
        filter_labels.append(("商品グループ", sample["product_group"].name if sample else "—"))
    if sales_group_id:
        sample = next((f for f in filtered if f["sales_group"]), None)
        filter_labels.append(("セールスグループ", sample["sales_group"].name if sample else "—"))
    if relationship_type:
        filter_labels.append((
            "関係性",
            RELATIONSHIP_TYPE_REPORT_LABELS.get(relationship_type, relationship_type),
        ))

    context = base_context(session, ui_session, active_nav="revenue_report")
    context.update({
        "filter_labels": filter_labels, "total_revenue": total_revenue,
        "by_product": by_product, "deals": deals,
    })
    return templates.TemplateResponse(request, "revenue_drilldown.html", context)
