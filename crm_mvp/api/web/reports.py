"""売上レポート(2026-08-13 ユーザー要望)。

商品グループ・セールスグループ・取引先(法人グループのロールアップ)・
関係性(新規/更新/Upsell/Cross-sell)の4軸でclosed_won商談の実売上を集計する。

2026-08-14: 各集計行をクリックすると、その軸で絞り込んだ商品別内訳・
該当商談一覧に遷移できるドリルダウンを追加(平面的なレポートで終わらせ
ず、紐づいている内容へ分解して辿れるようにする、というユーザー要望)。

2026-08-14 Phase2: 「最終的には自由にオブジェクトを選択して、動的レポート
作成できるようにしたい」という要望に応え、/ui/reports/builder を追加。
行・列の軸を自由に選べる(2軸まで)クロス集計ビルダーで、対象範囲も
受注実績のみ/パイプライン込みの全ステージを切り替えられる。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...services.product_groups import list_product_groups
from ...services.revenue_report import (
    RELATIONSHIP_TYPE_REPORT_LABELS, STAGE_REPORT_LABELS, aggregate_by,
    all_stage_line_item_facts, build_dimensions, closed_won_revenue_rows,
    facts_by_engagement, filter_facts, line_item_facts, pivot,
    product_group_revenue, totals_by_currency,
)
from ...services.sales_groups import list_sales_groups
from ...services.users import list_users
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

DIMENSION_CHOICES = [
    ("product", "商品"), ("product_group", "商品グループ"),
    ("account", "取引先(法人グループ)"), ("sales_group", "セールスグループ"),
    ("relationship_type", "関係性"), ("stage", "ステージ"),
    ("owner", "担当者"), ("period", "期間"),
]


@router.get("/ui/reports/revenue", response_class=HTMLResponse)
def revenue_report_page(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    rows = closed_won_revenue_rows(session, ui_session.tenant_id)

    total_revenue_by_currency = totals_by_currency(rows)

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

    context = base_context(session, ui_session, active_nav="revenue_report", request=request)
    context.update({
        "total_revenue_by_currency": total_revenue_by_currency, "deal_count": len(rows),
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

    total_revenue_by_currency = totals_by_currency(filtered)
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

    context = base_context(session, ui_session, active_nav="revenue_report", request=request)
    context.update({
        "filter_labels": filter_labels, "total_revenue_by_currency": total_revenue_by_currency,
        "by_product": by_product, "deals": deals,
    })
    return templates.TemplateResponse(request, "revenue_drilldown.html", context)


@router.get("/ui/reports/builder", response_class=HTMLResponse)
def report_builder_page(
    request: Request,
    rows: str = "product_group",
    cols: str = "",
    scope: str = "closed_won",
    granularity: str = "month",
    product_group_id: str = "",
    sales_group_id: str = "",
    relationship_type: str = "",
    stage: str = "",
    owner_user_id: str = "",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    granularity = granularity if granularity in ("month", "quarter") else "month"
    dimensions = build_dimensions(period_granularity=granularity)
    row_dim = dimensions.get(rows) or dimensions["product_group"]
    col_dim = dimensions.get(cols) if cols else None

    facts = (
        all_stage_line_item_facts(session, ui_session.tenant_id)
        if scope == "all" else line_item_facts(session, ui_session.tenant_id)
    )
    filtered = filter_facts(
        facts,
        product_group_id=uuid.UUID(product_group_id) if product_group_id else None,
        sales_group_id=uuid.UUID(sales_group_id) if sales_group_id else None,
        relationship_type=relationship_type or None,
        stage=stage or None,
        owner_user_id=uuid.UUID(owner_user_id) if owner_user_id else None,
    )
    result = pivot(filtered, row_dim, col_dim)

    context = base_context(session, ui_session, active_nav="report_builder", request=request)
    context.update({
        "dimension_choices": DIMENSION_CHOICES,
        "selected_rows": row_dim.key, "selected_cols": col_dim.key if col_dim else "",
        "row_dim_label": row_dim.label,
        "scope": scope, "granularity": granularity,
        "product_group_id": product_group_id, "sales_group_id": sales_group_id,
        "relationship_type": relationship_type, "stage": stage,
        "owner_user_id": owner_user_id,
        "product_groups": list_product_groups(session, ui_session.tenant_id),
        "sales_groups": list_sales_groups(session, ui_session.tenant_id),
        "users": list_users(session, ui_session.tenant_id),
        "relationship_type_choices": list(RELATIONSHIP_TYPE_REPORT_LABELS.items()),
        "stage_choices": list(STAGE_REPORT_LABELS.items()),
        "is_pivot": col_dim is not None,
        "total_amount_by_currency": totals_by_currency(filtered),
        "deal_count": len(filtered),
        **result,
    })
    return templates.TemplateResponse(request, "report_builder.html", context)
