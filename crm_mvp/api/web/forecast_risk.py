"""フォーキャストリスクレーダー画面(ロードマップ§6)。

マネージャー・経営層がポートフォリオを俯瞰し、予測未達になりうる案件と
次のアクションを1画面で確認できるようにする。
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Account
from ...services.forecast_risk import assess_forecast_risk
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/forecast-risk", response_class=HTMLResponse)
def forecast_risk_page(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    assessments = assess_forecast_risk(session, ui_session.tenant_id)

    account_ids = {a.engagement.account_id for a in assessments}
    accounts = {}
    if account_ids:
        accounts = {
            a.id: a for a in session.execute(
                select(Account).where(Account.id.in_(account_ids))
            ).scalars()
        }

    total_weighted = sum(
        (a.weighted_amount for a in assessments if a.weighted_amount is not None),
        Decimal("0"),
    )

    context = base_context(session, ui_session, active_nav="forecast_risk", request=request)
    context.update({
        "assessments": assessments,
        "accounts": accounts,
        "total_weighted": total_weighted,
    })
    return templates.TemplateResponse(request, "forecast_risk.html", context)
