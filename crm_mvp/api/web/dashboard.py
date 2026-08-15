"""ダッシュボード(案件一覧)。骨格 CRM 機能の入口。

2026-08-14 UI/UX見直し: 全テナントの全商談をフラットに出すだけだと、
Userとowner_user_idの構造が入った今でも「自分の担当」を見られなかった。
擬似セッション(Cookie)にUserを紐付けず、クエリパラメータでの絞り込み
(?owner_user_id=...)で対応する(レポートビルダー等と同じSSRパターン)。
既定でクローズ済み(受注/失注)は非表示にし、日次で触る「進行中の案件」
だけが見える状態を既定にする。
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...enums import OutboxStatus, ReviewCaseStatus, Stage
from ...models import (
    Account, Contract, Engagement, OutboxMessage, ReviewCase, WeeklyReview,
)
from ...services.confidence_score import compute_confidence_score
from ...services.engagement_relationships import list_renewal_candidates
from ...services.stage_transitions import load_gate_context
from ...services.users import list_users
from ...services.weekly_review import week_start
from .common import base_context
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


@router.get("/ui/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    flash: str | None = None,
    flash_type: str = "info",
    owner_user_id: str = "",
    show_closed: bool = False,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    query = select(Engagement).where(
        Engagement.tenant_id == ui_session.tenant_id,
        # R&D育成中(§7.5)は商談化承認前のため、通常のパイプラインから
        # 除外する — 専用画面(/ui/rnd-opportunities)で扱う。
        Engagement.exclude_from_pipeline.is_(False),
    )
    if owner_user_id:
        query = query.where(Engagement.owner_user_id == uuid.UUID(owner_user_id))
    if not show_closed:
        query = query.where(
            Engagement.stage.notin_([Stage.CLOSED_WON, Stage.CLOSED_LOST])
        )
    engagements = session.execute(
        query.order_by(Engagement.updated_at.desc())
    ).scalars().all()

    account_ids = {e.account_id for e in engagements}
    accounts = {}
    if account_ids:
        accounts = {
            a.id: a for a in session.execute(
                select(Account).where(Account.id.in_(account_ids))
            ).scalars()
        }

    current_week_start = week_start(date.today())
    reviewed_engagement_ids: set[uuid.UUID] = set()
    if engagements:
        engagement_ids = [e.id for e in engagements]
        reviews = session.execute(
            select(WeeklyReview).where(
                WeeklyReview.tenant_id == ui_session.tenant_id,
                WeeklyReview.week_start_date == current_week_start,
                WeeklyReview.engagement_id.in_(engagement_ids),
            )
        ).scalars().all()
        reviewed_engagement_ids = {
            r.engagement_id for r in reviews if r.rep_comment or r.manager_comment
        }

    rows = []
    for e in engagements:
        ctx = load_gate_context(session, ui_session.tenant_id, e)
        score = compute_confidence_score(
            ctx["slots"], ctx["nodes"], ctx["edges"], ctx["roles"],
        )
        rows.append({
            "engagement": e,
            "account_name": accounts[e.account_id].name
            if e.account_id in accounts else "—",
            "score": score,
            "reviewed_this_week": e.id in reviewed_engagement_ids,
        })

    # 更新未着手の警告(2026-08-14): list_renewal_candidates()は「更新商談が
    # まだ無い」契約のみを返すため、ここに件数が出ている時点で「誰も
    # 動いていない」ことが確定している。0日以内=期限超過を別カウントし
    # 目立たせる(新しいフィルタUIは作らず、契約更新管理への導線のみ)。
    unworked_renewals = list_renewal_candidates(session, ui_session.tenant_id, within_days=90)
    overdue_renewals = [c for c in unworked_renewals if c.end_date <= date.today()]

    # 連携状況サマリーカード(CRM_連携_実装計画.md C4-11)。
    pending_review_count = session.execute(
        select(func.count()).select_from(ReviewCase).where(
            ReviewCase.tenant_id == ui_session.tenant_id,
            ReviewCase.status == ReviewCaseStatus.PENDING,
        )
    ).scalar_one()
    outbox_attention_count = session.execute(
        select(func.count()).select_from(OutboxMessage).where(
            OutboxMessage.tenant_id == ui_session.tenant_id,
            OutboxMessage.status.in_([OutboxStatus.FAILED, OutboxStatus.DLQ]),
        )
    ).scalar_one()
    rnd_incubation_count = session.execute(
        select(func.count()).select_from(Engagement).where(
            Engagement.tenant_id == ui_session.tenant_id,
            Engagement.stage == Stage.RND_INCUBATION,
        )
    ).scalar_one()
    monitoring_alert_count = session.execute(
        select(func.count()).select_from(Contract).where(
            Contract.tenant_id == ui_session.tenant_id, Contract.monitoring_alert.is_(True),
        )
    ).scalar_one()

    context = base_context(
        session, ui_session, active_nav="dashboard", request=request, flash=flash, flash_type=flash_type,
    )
    context.update({
        "rows": rows,
        "users": list_users(session, ui_session.tenant_id),
        "owner_user_id": owner_user_id,
        "show_closed": show_closed,
        "unworked_renewal_count": len(unworked_renewals),
        "overdue_renewal_count": len(overdue_renewals),
        "pending_review_count": pending_review_count,
        "outbox_attention_count": outbox_attention_count,
        "rnd_incubation_count": rnd_incubation_count,
        "monitoring_alert_count": monitoring_alert_count,
    })
    return templates.TemplateResponse(request, "dashboard.html", context)
