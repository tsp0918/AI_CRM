"""見積もり・契約の横断一覧(レポート用)。

個々の見積もり作成・契約発行の操作は engagements.py 側(案件詳細画面)で
行う。ここは「今どれだけ見積もり/契約があるか」を横断で見るための
読み取り専用ビュー — 将来のROI/売上レポートの土台になる。

2026-08-14: 全件がフラットに並ぶと見にくいという要望に対応し、画面左で
担当別/商品別/取引先別のいずれかを選んで絞り込める階層フィルタ(Facet
Rail)を追加。右側の一覧は常にstatusをキーにグルーピングする。
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...services.account_hierarchy import get_family_account_ids, list_accounts_tree_ordered
from ...services.product_groups import (
    get_family_product_group_ids, list_product_groups_tree_ordered,
)
from ...services.quoting import contract_document_facts, filter_documents, quote_document_facts
from ...services.users import list_users
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

DIM_CHOICES = [("owner", "担当別"), ("product", "商品別"), ("account", "取引先別")]

QUOTE_STATUS_LABELS = {
    "draft": "下書き", "sent": "送付済み", "accepted": "承諾",
    "rejected": "却下", "expired": "失効",
}
QUOTE_STATUS_ORDER = ["draft", "sent", "accepted", "rejected", "expired"]
QUOTE_STATUS_BADGE = {
    "draft": "", "sent": "badge-gate-warn", "accepted": "badge-gate-ok",
    "rejected": "badge-gate-block", "expired": "badge-gate-block",
}

CONTRACT_STATUS_LABELS = {
    "draft": "下書き", "sent": "送付済み", "signed": "署名済み",
    "active": "稼働中", "terminated": "解約",
}
CONTRACT_STATUS_ORDER = ["draft", "sent", "signed", "active", "terminated"]
CONTRACT_STATUS_BADGE = {
    "draft": "", "sent": "badge-gate-warn", "signed": "badge-gate-warn",
    "active": "badge-gate-ok", "terminated": "badge-gate-block",
}


def _group_by_status(facts: list[dict], order: list[str]) -> list[tuple[str, list[dict]]]:
    by_status: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_status[f["document"].status].append(f)
    return [(s, by_status[s]) for s in order if by_status.get(s)]


@router.get("/ui/quotes", response_class=HTMLResponse)
def quotes_and_contracts_list(
    request: Request,
    dim: str = "",
    owner_user_id: str = "",
    product_group_id: str = "",
    account_id: str = "",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    dim = dim if dim in ("owner", "product", "account") else ""

    quote_facts = quote_document_facts(session, ui_session.tenant_id)
    contract_facts = contract_document_facts(session, ui_session.tenant_id)

    def combined_count(**kwargs) -> int:
        return len(filter_documents(quote_facts, **kwargs)) \
            + len(filter_documents(contract_facts, **kwargs))

    rail_context: dict = {}
    filter_kwargs: dict = {}

    if dim == "owner":
        users = list_users(session, ui_session.tenant_id)
        rail_context["owner_rail"] = [
            {"user": u, "count": combined_count(owner_user_id=u.id)} for u in users
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
            return combined_count(product_group_ids=family)

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
            return combined_count(account_ids=family)

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

    filtered_quote_facts = filter_documents(quote_facts, **filter_kwargs) if filter_kwargs else quote_facts
    filtered_contract_facts = filter_documents(contract_facts, **filter_kwargs) if filter_kwargs else contract_facts

    context = base_context(session, ui_session, active_nav="quotes", request=request)
    context.update({
        "dim": dim, "dim_choices": DIM_CHOICES,
        "owner_user_id": owner_user_id, "product_group_id": product_group_id,
        "account_id": account_id,
        "quotes_by_status": _group_by_status(filtered_quote_facts, QUOTE_STATUS_ORDER),
        "contracts_by_status": _group_by_status(filtered_contract_facts, CONTRACT_STATUS_ORDER),
        "quote_status_labels": QUOTE_STATUS_LABELS, "contract_status_labels": CONTRACT_STATUS_LABELS,
        "quote_status_badge": QUOTE_STATUS_BADGE, "contract_status_badge": CONTRACT_STATUS_BADGE,
        "total_quote_count": len(quote_facts), "total_contract_count": len(contract_facts),
        "filtered_quote_count": len(filtered_quote_facts),
        "filtered_contract_count": len(filtered_contract_facts),
        **rail_context,
    })
    return templates.TemplateResponse(request, "quotes.html", context)
