"""契約済み(クロージング)案件からの継続/Upsell/Cross-sellを追跡する。

Engagement.parent_engagement_id / relationship_type による親子構造は
Quote/Contract と同じく明示的なサービス関数経由でのみ操作する
(ORMのrelationship()は使わない — このアプリの既存の流儀に合わせる)。

「契約中商談のrenewal商談もわかるようにしたい」という要望に対応するため、
契約終了日が近い ACTIVE な Contract を検出する list_renewal_candidates も
ここに置く(2026-08-13 ユーザー要望)。半導体製造材料では契約期間管理は
本来あまり一般的ではないが、現時点ではこの運用(契約期間ベースの更新管理)
で進める、というユーザー決定に基づく。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ContractStatus, EngagementRelationshipType, Stage
from ..models import Account, Contract, Engagement, QualificationSlot
from .compliance_screening import ensure_account_screened
from .decay_policy import compute_decays_at
from .party_compliance import check_party_clearance

RENEWAL_CARRYOVER_ACTOR = "system:renewal-carryover"


def create_child_engagement(
    session: Session, tenant_id: uuid.UUID, parent: Engagement, *,
    relationship_type: EngagementRelationshipType, name: str,
    owner: str | None = None,
) -> Engagement:
    """親案件(通常は closed_won の契約済み案件)と同じ取引先で、継続/Upsell/
    Cross-sell の新しい商談を起こす。ステージは常に LEAD から開始する。

    Upsell/Cross-sellは別商材の検討になるため、予算・決裁者・タイミングを
    商談ごとに再確認する(親のクオリフィケーション結果は自動継承しない)。
    一方Renewalは「同じ内容の契約を継続するだけ」であることが多いため、
    2026-08-14: 親の直近のQualificationSlotを子に引き継ぐ(そのまま
    通用するかはproposal/verifyで都度更新できる — あくまで初期値の
    複製であり、証跡自体を丸ごと信頼済み扱いにするわけではない)。"""
    if not name.strip():
        raise ValueError("商談名を入力してください")

    account = session.get(Account, parent.account_id)
    if account is not None:
        ensure_account_screened(session, tenant_id, account)
        party_block_reason = check_party_clearance(session, tenant_id, account_id=account.id)
        if party_block_reason is not None:
            raise ValueError(party_block_reason)

    if relationship_type == EngagementRelationshipType.RENEWAL:
        # §7.3 IF-16: 継続監視アラートが立っている契約からの更新商談起票を
        # ブロックする。
        alerted = session.execute(
            select(Contract).where(
                Contract.tenant_id == tenant_id, Contract.engagement_id == parent.id,
                Contract.monitoring_alert.is_(True),
            )
        ).scalars().first()
        if alerted is not None:
            raise ValueError(
                f"契約 {alerted.contract_number} に継続監視アラートが発生しているため、"
                "更新商談を起票できません"
            )

    child = Engagement(
        tenant_id=tenant_id, account_id=parent.account_id, name=name.strip(),
        stage=Stage.LEAD, owner=owner,
        parent_engagement_id=parent.id, relationship_type=relationship_type,
    )
    session.add(child)
    session.flush()

    if relationship_type == EngagementRelationshipType.RENEWAL:
        _carry_over_qualification_slots(session, tenant_id, parent, child)

    return child


def _carry_over_qualification_slots(
    session: Session, tenant_id: uuid.UUID, parent: Engagement, child: Engagement,
) -> None:
    parent_slots = session.execute(
        select(QualificationSlot).where(
            QualificationSlot.tenant_id == tenant_id,
            QualificationSlot.engagement_id == parent.id,
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for slot in parent_slots:
        session.add(QualificationSlot(
            tenant_id=tenant_id, engagement_id=child.id, criterion=slot.criterion,
            value=slot.value, confidence=slot.confidence,
            asserted_at=now, decays_at=compute_decays_at(slot.criterion, now),
            written_by=RENEWAL_CARRYOVER_ACTOR,
        ))
    session.flush()


def list_child_engagements(
    session: Session, tenant_id: uuid.UUID, parent_id: uuid.UUID,
) -> list[Engagement]:
    return session.execute(
        select(Engagement).where(
            Engagement.tenant_id == tenant_id,
            Engagement.parent_engagement_id == parent_id,
        ).order_by(Engagement.created_at)
    ).scalars().all()


def list_renewal_candidates(
    session: Session, tenant_id: uuid.UUID, *,
    within_days: int = 90, today: date | None = None,
) -> list[Contract]:
    """契約終了日が迫っている(または既に過ぎている) ACTIVE な契約のうち、
    まだ更新商談(RENEWAL)が作成されていないものを、終了日が近い順に返す。"""
    today = today or date.today()
    threshold = today + timedelta(days=within_days)

    contracts = session.execute(
        select(Contract).where(
            Contract.tenant_id == tenant_id, Contract.status == ContractStatus.ACTIVE,
            Contract.end_date.is_not(None), Contract.end_date <= threshold,
        ).order_by(Contract.end_date)
    ).scalars().all()
    if not contracts:
        return []

    already_renewing = {
        row[0] for row in session.execute(
            select(Engagement.parent_engagement_id).where(
                Engagement.tenant_id == tenant_id,
                Engagement.relationship_type == EngagementRelationshipType.RENEWAL,
                Engagement.parent_engagement_id.is_not(None),
            )
        )
    }
    return [c for c in contracts if c.engagement_id not in already_renewing]


def resolve_renewal_context(
    session: Session, tenant_id: uuid.UUID, contracts: list[Contract],
) -> dict[uuid.UUID, dict]:
    """レポート表示用に、各Contractに紐づく案件名・取引先名をまとめて引く。"""
    engagement_ids = {c.engagement_id for c in contracts}
    engagements = {
        e.id: e for e in session.execute(
            select(Engagement).where(Engagement.id.in_(engagement_ids))
        ).scalars()
    }
    account_ids = {e.account_id for e in engagements.values()}
    accounts = {
        a.id: a for a in session.execute(
            select(Account).where(Account.id.in_(account_ids))
        ).scalars()
    } if account_ids else {}

    context = {}
    for c in contracts:
        engagement = engagements.get(c.engagement_id)
        account = accounts.get(engagement.account_id) if engagement else None
        context[c.id] = {
            "engagement": engagement,
            "account": account,
            "days_remaining": (c.end_date - date.today()).days if c.end_date else None,
        }
    return context
