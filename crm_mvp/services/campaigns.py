"""Campaignの効果測定(2026-08-14)。

もとは crm_mvp/api/web/campaigns.py に直書きされていた集計ロジックを
サービス層に抽出したもの — Webルートと経営レポート・スナップショット
機能(report_snapshot.py)の両方から同じ集計を再利用できるようにする。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Campaign, Lead


def list_campaign_effectiveness(
    session: Session, tenant_id: uuid.UUID, as_of: datetime | None = None,
) -> list[dict]:
    """Campaignごとに、紐づくLead数・案件化数・転換率をまとめる。

    as_of を指定すると「その時点までに獲得・案件化していたLead」だけに
    絞る(経営レポートのスナップショットで過去時点を再構成するために使う)。
    現在の画面(as_of=None)の挙動は変えない。"""
    campaigns = session.execute(
        select(Campaign).where(Campaign.tenant_id == tenant_id)
        .order_by(Campaign.created_at.desc())
    ).scalars().all()
    if not campaigns:
        return []

    # campaign毎に2クエリ(N+1)ではなく、GROUP BYでテナント全体を2クエリで
    # まとめて集計する。
    lead_query = select(Lead.source_campaign_id, func.count()).where(
        Lead.tenant_id == tenant_id, Lead.source_campaign_id.is_not(None),
    )
    converted_query = select(Lead.source_campaign_id, func.count()).where(
        Lead.tenant_id == tenant_id, Lead.source_campaign_id.is_not(None),
        Lead.status == "converted",
    )
    if as_of is not None:
        lead_query = lead_query.where(Lead.created_at <= as_of)
        converted_query = converted_query.where(Lead.converted_at <= as_of)

    lead_counts = dict(session.execute(lead_query.group_by(Lead.source_campaign_id)).all())
    converted_counts = dict(
        session.execute(converted_query.group_by(Lead.source_campaign_id)).all()
    )

    rows = []
    for c in campaigns:
        lead_count = lead_counts.get(c.id, 0)
        converted_count = converted_counts.get(c.id, 0)
        rows.append({
            "campaign": c, "lead_count": lead_count, "converted_count": converted_count,
            "conversion_rate": round(converted_count / lead_count * 100) if lead_count else 0,
        })
    return rows
