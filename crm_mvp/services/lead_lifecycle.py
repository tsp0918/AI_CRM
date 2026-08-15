"""Leadのライフサイクルと案件化(コンバージョン)(ロードマップ§7)。

NEW → WORKING → MQL → SQL → CONVERTED / DISQUALIFIED。

MQLへの昇格はスコア閾値による自動提案を基本としつつ、Waiverと同じ思想で
人が理由付きで手動昇格・保留できるようにする(disqualify_reason /
promoted_by 相当は呼び出し側の written_by で残す)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import AccessLevel, LeadStatus, Stage, Stance, TouchChannel
from ..models import Account, Engagement, Lead, Touch
from .compliance_screening import ensure_account_screened
from .contacts import register_contact_and_link
from .lead_scoring import LeadScore, compute_lead_score
from .party_compliance import check_party_clearance

MQL_SCORE_THRESHOLD = 50


def record_touch(
    session: Session, tenant_id: uuid.UUID, lead: Lead, *, channel: TouchChannel,
    occurred_at: datetime | None = None, campaign_id: uuid.UUID | None = None,
    source_system: str = "manual", actor: str | None = None,
    raw_payload: dict | None = None,
) -> Touch:
    touch = Touch(
        tenant_id=tenant_id, lead_id=lead.id, campaign_id=campaign_id,
        channel=channel, occurred_at=occurred_at or datetime.now(timezone.utc),
        source_system=source_system, actor=actor, raw_payload=raw_payload or {},
    )
    session.add(touch)
    if lead.status == LeadStatus.NEW:
        lead.status = LeadStatus.WORKING
    session.flush()
    return touch


def maybe_promote_to_mql(lead: Lead, score: LeadScore) -> bool:
    """スコアが閾値を超えたらMQLへ自動昇格を提案する。呼び出し側が
    実際にステータスを変えるかは委ねる(常に自動反映はしない)。"""
    return (
        lead.status == LeadStatus.WORKING
        and score.company_score >= MQL_SCORE_THRESHOLD
        and score.person_score >= MQL_SCORE_THRESHOLD
    )


def promote_lead(session: Session, lead: Lead, to_status: LeadStatus) -> Lead:
    lead.status = to_status
    session.flush()
    return lead


def disqualify_lead(session: Session, lead: Lead, *, reason: str) -> Lead:
    lead.status = LeadStatus.DISQUALIFIED
    lead.disqualify_reason = reason
    session.flush()
    return lead


def _build_conversion_snapshot(lead: Lead, touches: list[Touch], score: LeadScore) -> dict:
    channel_counts: dict[str, int] = {}
    for t in touches:
        channel_counts[t.channel] = channel_counts.get(t.channel, 0) + 1

    days_as_lead = None
    if lead.created_at:
        days_as_lead = (datetime.now(timezone.utc) - lead.created_at).days

    return {
        "company_score": score.company_score,
        "person_score": score.person_score,
        "quadrant": score.quadrant,
        "company_reasons": score.company_reasons,
        "person_reasons": score.person_reasons,
        "touch_count": len(touches),
        "touch_channel_counts": channel_counts,
        "source_channel": lead.source_channel,
        "source_campaign_id": str(lead.source_campaign_id) if lead.source_campaign_id else None,
        "lead_status_before_conversion": lead.status,
        "days_as_lead": days_as_lead,
    }


def convert_lead(
    session: Session, tenant_id: uuid.UUID, lead: Lead, *, actor: str,
) -> Engagement:
    """Lead → Account(既存 or 新規)/Contact/Engagement への引き渡し。

    Engagement.originating_lead_id を残すことで、Lead自体が将来アーカイブ
    されてもチャネル別ROI集計の帰属を遡って辿れるようにする。案件化した
    瞬間のスコア・接点サマリーは lead.conversion_snapshot に固定して残し、
    Engagement側から「なぜこのタイミングで案件化したか」を辿れるようにする。
    """
    if lead.status == LeadStatus.CONVERTED:
        raise ValueError("この Lead は既に案件化済みです")

    touches = list_touches(session, tenant_id, lead.id)
    existing_account = (
        session.get(Account, lead.matched_account_id) if lead.matched_account_id else None
    )
    score = compute_lead_score(lead, existing_account, touches)
    snapshot = _build_conversion_snapshot(lead, touches, score)

    account = existing_account
    if account is None:
        account = Account(tenant_id=tenant_id, name=lead.company_name)
        session.add(account)
        session.flush()

    # 既存取引先なら鮮度チェック・再スクリーニング(C2-5)、新規なら初回
    # スクリーニング(C2-4)。いずれもensure_account_screened側でfreshなら
    # 再実行しない。
    ensure_account_screened(session, tenant_id, account)
    party_block_reason = check_party_clearance(session, tenant_id, account_id=account.id)
    if party_block_reason is not None:
        raise ValueError(party_block_reason)

    engagement = Engagement(
        tenant_id=tenant_id, account_id=account.id,
        name=f"{lead.company_name}({lead.full_name}様経由)",
        stage=Stage.LEAD, originating_lead_id=lead.id,
    )
    session.add(engagement)
    session.flush()

    register_contact_and_link(
        session, tenant_id, engagement,
        full_name=lead.full_name, title=lead.title, email=lead.email,
        roles=[], stance=Stance.UNKNOWN, access_level=AccessLevel.CONTACTED,
        written_by=actor,
    )

    lead.status = LeadStatus.CONVERTED
    lead.converted_at = datetime.now(timezone.utc)
    lead.converted_engagement_id = engagement.id
    lead.matched_account_id = account.id
    lead.conversion_snapshot = snapshot
    session.flush()
    return engagement


def list_touches(session: Session, tenant_id: uuid.UUID, lead_id: uuid.UUID) -> list[Touch]:
    return session.execute(
        select(Touch).where(
            Touch.tenant_id == tenant_id, Touch.lead_id == lead_id,
        ).order_by(Touch.occurred_at.desc())
    ).scalars().all()
