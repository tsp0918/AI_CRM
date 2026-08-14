"""Campaign管理のUI(ロードマップ§7)。

ROI集計の単位となる軽量な入れ物。作成・一覧のみのシンプルな管理画面。
Leadの獲得経路として紐付けることで、マーケ活動の効果測定につなげる。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...enums import CampaignChannelType
from ...models import Campaign, Lead
from .common import base_context, redirect_with_flash
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])

CAMPAIGN_CHANNEL_LABELS = {
    "event": "イベント", "content": "コンテンツ", "paid_ads": "広告",
    "outbound_sequence": "アウトバウンド", "partner": "パートナー",
}


@router.get("/ui/campaigns", response_class=HTMLResponse)
def campaigns_list(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    campaigns = session.execute(
        select(Campaign).where(Campaign.tenant_id == ui_session.tenant_id)
        .order_by(Campaign.created_at.desc())
    ).scalars().all()

    # campaign毎に2クエリ(N+1)ではなく、GROUP BYでテナント全体を2クエリで
    # まとめて集計する。
    lead_counts = dict(session.execute(
        select(Lead.source_campaign_id, func.count()).where(
            Lead.tenant_id == ui_session.tenant_id, Lead.source_campaign_id.is_not(None),
        ).group_by(Lead.source_campaign_id)
    ).all())
    converted_counts = dict(session.execute(
        select(Lead.source_campaign_id, func.count()).where(
            Lead.tenant_id == ui_session.tenant_id, Lead.source_campaign_id.is_not(None),
            Lead.status == "converted",
        ).group_by(Lead.source_campaign_id)
    ).all())

    rows = []
    for c in campaigns:
        lead_count = lead_counts.get(c.id, 0)
        converted_count = converted_counts.get(c.id, 0)
        rows.append({
            "campaign": c, "lead_count": lead_count, "converted_count": converted_count,
            "conversion_rate": round(converted_count / lead_count * 100) if lead_count else 0,
        })

    context = base_context(
        session, ui_session, active_nav="campaigns", request=request, flash=flash, flash_type=flash_type,
    )
    context.update({
        "rows": rows, "channel_types": list(CampaignChannelType),
        "channel_labels": CAMPAIGN_CHANNEL_LABELS,
    })
    return templates.TemplateResponse(request, "campaigns.html", context)


@router.post("/ui/campaigns/new")
def campaign_new_submit(
    name: str = Form(...),
    channel_type: str = Form(...),
    owner_team: str = Form(""),
    cost: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    if not name.strip():
        return redirect_with_flash("/ui/campaigns", "Campaign名を入力してください", "error")

    campaign = Campaign(
        tenant_id=ui_session.tenant_id, name=name.strip(),
        channel_type=CampaignChannelType(channel_type),
        owner_team=owner_team.strip() or None,
        cost=float(cost) if cost.strip() else None,
        start_date=date.fromisoformat(start_date) if start_date.strip() else None,
        end_date=date.fromisoformat(end_date) if end_date.strip() else None,
    )
    session.add(campaign)
    session.commit()

    return redirect_with_flash("/ui/campaigns", f"Campaign「{campaign.name}」を作成しました")
