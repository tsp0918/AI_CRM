"""見積もり・契約の横断一覧(レポート用)。

個々の見積もり作成・契約発行の操作は engagements.py 側(案件詳細画面)で
行う。ここは「今どれだけ見積もり/契約があるか」を横断で見るための
読み取り専用ビュー — 将来のROI/売上レポートの土台になる。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Engagement
from ...services.quoting import list_contracts, list_quotes
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/quotes", response_class=HTMLResponse)
def quotes_and_contracts_list(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    quotes = list_quotes(session, ui_session.tenant_id)
    contracts = list_contracts(session, ui_session.tenant_id)

    engagement_ids = {q.engagement_id for q in quotes} | {c.engagement_id for c in contracts}
    engagements = {}
    if engagement_ids:
        engagements = {
            e.id: e for e in session.execute(
                select(Engagement).where(Engagement.id.in_(engagement_ids))
            ).scalars()
        }

    context = base_context(session, ui_session, active_nav="quotes")
    context.update({"quotes": quotes, "contracts": contracts, "engagements": engagements})
    return templates.TemplateResponse(request, "quotes.html", context)
