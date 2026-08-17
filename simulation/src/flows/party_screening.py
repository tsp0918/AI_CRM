"""取引先の実スクリーニング結果をCRMのComplianceStatusへ反映する。

CRMの`get_screening_port()`(`crm_mvp/api/deps.py`)は常に`MockScreeningAdapter`
(常にCLEARを返す)を使う実装になっており、実`AITMScreeningAdapter`への
差し替えは「本セッション以前からの既存パターンとして変更しない」と明記
されたドキュメント化済みの意図的な選択(`crm_mvp/services/compliance_screening.py`
冒頭コメント)。そのためアプリ側のDIは変更せず、シミュレータ自身が
`AITMScreeningAdapter`相当の変換ロジックだけを借りてComplianceStatusを
直接書く(P2のマスタ投入と同じ「DBへの読み書きは検証・セットアップ目的の
み」という方針の延長)。§6.2の制裁ヒット/possible_matchシナリオを実際の
AI_TMスクリーニング結果で再現するために使う。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from crm_mvp.enums import ComplianceCheckType, ComplianceOutcome
from crm_mvp.models import ComplianceStatus
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clients.aitm import AitmClient

FRESHNESS_WINDOW_DAYS = 180

# `crm_mvp/ports/screening.py`のAITMScreeningAdapter._OUTCOME_MAPと同一。
_OUTCOME_MAP = {
    "match": ComplianceOutcome.HIT,
    "possible_match": ComplianceOutcome.NEEDS_REVIEW,
    "clear": ComplianceOutcome.CLEAR,
}


def screen_and_record(
    session: Session, tenant_id: uuid.UUID, aitm: AitmClient, *,
    account_id: uuid.UUID, legal_name: str, country: str | None,
    check_type: ComplianceCheckType = ComplianceCheckType.SANCTIONS,
) -> ComplianceOutcome:
    result = aitm.screen_counterparty(legal_name, country=country)
    if result is None:
        # フェイルクローズ(§6.2「UNKNOWNがCLEARに化けてはいけない」)。
        outcome = ComplianceOutcome.UNKNOWN
        provider_request_id = None
    else:
        outcome = _OUTCOME_MAP.get(result.get("result_status", ""), ComplianceOutcome.UNKNOWN)
        provider_request_id = result.get("id")

    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == tenant_id, ComplianceStatus.account_id == account_id,
            ComplianceStatus.check_type == check_type,
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if status is None:
        status = ComplianceStatus(tenant_id=tenant_id, account_id=account_id, check_type=check_type)
        session.add(status)
    status.outcome = outcome
    status.provider = "aitm-screening"
    status.provider_request_id = provider_request_id
    status.checked_at = now
    status.valid_until = now + timedelta(days=FRESHNESS_WINDOW_DAYS)
    session.flush()
    return outcome
