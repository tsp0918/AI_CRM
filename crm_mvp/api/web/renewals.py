"""契約更新(Renewal)管理画面。

契約終了日が近い ACTIVE な契約のうち、まだ更新商談が作られていないものを
一覧する — 「契約中商談のrenewal商談もわかるようにしたい」という要望への
対応(2026-08-13)。ここから直接、更新商談を1クリックで起こせる。

2026-08-14: 見積・契約一覧と同じ担当別/商品別/取引先別の左フィルタ
(Facet Rail)を追加。ただしこちらのキーはstatusではなく残り日数バケット
(超過/〜7日/〜30日/〜90日/90日超)にする。
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...enums import EngagementRelationshipType
from ...models import Contract, Engagement
from ...services.account_hierarchy import get_family_account_ids, list_accounts_tree_ordered
from ...services.engagement_relationships import (
    create_child_engagement, list_renewal_candidates, resolve_renewal_context,
)
from ...services.product_groups import (
    get_family_product_group_ids, list_product_groups_tree_ordered,
)
from ...services.quoting import contract_document_facts, filter_documents
from ...services.users import list_users
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

DIM_CHOICES = [("owner", "担当別"), ("product", "商品別"), ("account", "取引先別")]

BUCKET_LABELS = {
    "overdue": "超過", "within_7": "〜7日", "within_30": "〜30日",
    "within_90": "〜90日", "beyond_90": "90日超",
}
BUCKET_ORDER = ["overdue", "within_7", "within_30", "within_90", "beyond_90"]
BUCKET_BADGE = {
    "overdue": "badge-gate-block", "within_7": "badge-gate-block",
    "within_30": "badge-gate-warn", "within_90": "badge-gate-warn", "beyond_90": "",
}


def _days_bucket(days_remaining: int | None) -> str:
    if days_remaining is None:
        return "beyond_90"
    if days_remaining < 0:
        return "overdue"
    if days_remaining <= 7:
        return "within_7"
    if days_remaining <= 30:
        return "within_30"
    if days_remaining <= 90:
        return "within_90"
    return "beyond_90"


@router.get("/ui/renewals", response_class=HTMLResponse)
def renewals_list(
    request: Request,
    within_days: int = 90,
    dim: str = "",
    owner_user_id: str = "",
    product_group_id: str = "",
    account_id: str = "",
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    dim = dim if dim in ("owner", "product", "account") else ""

    candidates = list_renewal_candidates(
        session, ui_session.tenant_id, within_days=within_days,
    )
    renewal_context = resolve_renewal_context(session, ui_session.tenant_id, candidates)
    facts = contract_document_facts(session, ui_session.tenant_id, candidates)

    def count_for(**kwargs) -> int:
        return len(filter_documents(facts, **kwargs))

    rail_context: dict = {}
    filter_kwargs: dict = {}

    if dim == "owner":
        users = list_users(session, ui_session.tenant_id)
        rail_context["owner_rail"] = [
            {"user": u, "count": count_for(owner_user_id=u.id)} for u in users
        ]
        if owner_user_id:
            filter_kwargs["owner_user_id"] = uuid.UUID(owner_user_id)

    elif dim == "product":
        tree = list_product_groups_tree_ordered(session, ui_session.tenant_id)
        children_by_parent: dict[uuid.UUID, list] = defaultdict(list)
        for g in tree:
            if g.parent_group_id is not None:
                children_by_parent[g.parent_group_id].append(g)

        def product_count(group_id: uuid.UUID) -> int:
            family = get_family_product_group_ids(session, ui_session.tenant_id, group_id)
            return count_for(product_group_ids=family)

        rail_context["product_rail"] = [
            {
                "group": g, "count": product_count(g.id),
                "children": [
                    {"group": c, "count": product_count(c.id)}
                    for c in children_by_parent.get(g.id, [])
                ],
            }
            for g in tree if g.parent_group_id is None
        ]
        if product_group_id:
            filter_kwargs["product_group_ids"] = get_family_product_group_ids(
                session, ui_session.tenant_id, uuid.UUID(product_group_id),
            )

    elif dim == "account":
        tree = list_accounts_tree_ordered(session, ui_session.tenant_id)
        children_by_parent: dict[uuid.UUID, list] = defaultdict(list)
        for a in tree:
            if a.parent_account_id is not None:
                children_by_parent[a.parent_account_id].append(a)

        def account_count(node_id: uuid.UUID) -> int:
            family = get_family_account_ids(session, ui_session.tenant_id, node_id)
            return count_for(account_ids=family)

        rail_context["account_rail"] = [
            {
                "account": a, "count": account_count(a.id),
                "children": [
                    {"account": c, "count": account_count(c.id)}
                    for c in children_by_parent.get(a.id, [])
                ],
            }
            for a in tree if a.parent_account_id is None
        ]
        if account_id:
            filter_kwargs["account_ids"] = get_family_account_ids(
                session, ui_session.tenant_id, uuid.UUID(account_id),
            )

    filtered_facts = filter_documents(facts, **filter_kwargs) if filter_kwargs else facts

    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for f in filtered_facts:
        days_remaining = renewal_context.get(f["document"].id, {}).get("days_remaining")
        by_bucket[_days_bucket(days_remaining)].append(f)
    bucketed = [(b, by_bucket[b]) for b in BUCKET_ORDER if by_bucket.get(b)]

    context = base_context(
        session, ui_session, active_nav="renewals", flash=flash, flash_type=flash_type,
    )
    context.update({
        "within_days": within_days, "dim": dim, "dim_choices": DIM_CHOICES,
        "owner_user_id": owner_user_id, "product_group_id": product_group_id,
        "account_id": account_id,
        "bucketed": bucketed, "bucket_labels": BUCKET_LABELS, "bucket_badge": BUCKET_BADGE,
        "renewal_context": renewal_context,
        "total_count": len(facts), "filtered_count": len(filtered_facts),
        **rail_context,
    })
    return templates.TemplateResponse(request, "renewals.html", context)


@router.post("/ui/renewals/{contract_id}/start")
def start_renewal_ui(
    contract_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    contract = session.get(Contract, contract_id)
    if contract is None or contract.tenant_id != ui_session.tenant_id:
        return redirect_with_flash("/ui/renewals", "契約が見つかりません", "error")

    parent = session.get(Engagement, contract.engagement_id)
    if parent is None:
        return redirect_with_flash("/ui/renewals", "元の商談が見つかりません", "error")

    child = create_child_engagement(
        session, ui_session.tenant_id, parent,
        relationship_type=EngagementRelationshipType.RENEWAL,
        name=f"{parent.name}(更新)",
    )
    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{child.id}", f"更新商談「{child.name}」を作成しました",
    )
