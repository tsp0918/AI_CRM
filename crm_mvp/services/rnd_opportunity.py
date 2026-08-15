"""R&D案件からの商談自動作成(2026-08-15, CRM_連携引き継ぎ書.md §7.5 IF-14)。

AI_TMのR&D連携から届いたイベントを、専用ステージ`Stage.RND_INCUBATION`の
`Engagement`として隔離する。`exclude_from_pipeline=True`によりパイプライン
予実集計から除外し、「まだ商談化していない開発案件」が数字を歪めないように
する。商談化の承認(`promote_rnd_engagement`)を経て初めて通常ステージへ
移行する。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..enums import Stage
from ..models import Account, Engagement
from .compliance_screening import ensure_account_screened
from .outbox import enqueue_outbox


def process_rnd_opportunity(
    session: Session, tenant_id: uuid.UUID, payload: dict, *,
    actor: str = "system:aitm-rnd-webhook",
) -> Engagement:
    customer = payload.get("customer") or {}
    account = None
    crm_account_id = customer.get("crm_account_id")
    if customer.get("is_existing") and crm_account_id:
        account = session.get(Account, uuid.UUID(crm_account_id))

    if account is None:
        account = Account(
            tenant_id=tenant_id, name=customer.get("legal_name", "(不明な取引先)"),
            country=customer.get("country"), aitm_party_id=customer.get("aitm_party_id"),
        )
        session.add(account)
        session.flush()
        # 新規取引先は即スクリーニングする(§7.5の暗黙の前提 — R&D起点でも
        # 輸出管理上の懸念があるかは早期に把握したい)。
        ensure_account_screened(session, tenant_id, account)

    engagement = Engagement(
        tenant_id=tenant_id, account_id=account.id,
        name=payload.get("rnd_case_title", "R&D案件"),
        stage=Stage.RND_INCUBATION, exclude_from_pipeline=True,
        aitm_rnd_case_id=payload.get("rnd_case_id"),
    )
    session.add(engagement)
    session.flush()

    # AI_TMへ商談IDを返す(Webhookのレスポンスではなく別APIで、§7.5)。
    enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.rnd.link_opportunity",
        payload={
            "rnd_case_id": payload.get("rnd_case_id"),
            "crm_engagement_id": str(engagement.id),
            "crm_account_id": str(account.id),
        },
        ref_type="engagement", ref_id=str(engagement.id), actor=actor,
    )
    return engagement


def promote_rnd_engagement(session: Session, tenant_id: uuid.UUID, engagement: Engagement) -> Engagement:
    """R&D育成中の商談を、通常のパイプライン(LEAD)へ商談化承認する。"""
    if engagement.stage != Stage.RND_INCUBATION:
        raise ValueError("R&D育成中の商談のみ商談化できます")
    engagement.stage = Stage.LEAD
    engagement.exclude_from_pipeline = False
    session.flush()
    return engagement
