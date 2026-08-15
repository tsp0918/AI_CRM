"""Account単位のコンプライアンススクリーニング共通ロジック(2026-08-15)。

`crm_mvp/api/accounts.py`の`submit_compliance_check`(手動トリガー、JSON API)
が元々持っていたロジックをここに切り出し、Account作成時の自動フック
(CRM_連携引き継ぎ書.md C2-4)・鮮度チェック時の再スクリーニング(C2-5)からも
再利用できるようにする。

スクリーニング先は`ScreeningPort`(`crm_mvp/ports/screening.py`)— 未接続
環境では`MockScreeningAdapter`(常にCLEARを返す)がデフォルトになる、という
既存コードベースの挙動をそのまま踏襲する(Phase 0/1a以降の「未設定なら
明示的に失敗させる」Outbox方式とは異なる設計判断だが、この同期スクリーニング
経路は本セッション以前から存在する既存パターンであり、ここで変更しない)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComplianceCheckType
from ..models import Account, ComplianceStatus
from ..ports.screening import MockScreeningAdapter, ScreeningPort

FRESHNESS_WINDOW_DAYS = 180

# Account作成時に自動で走らせる既定のチェック種別(§8.3のブロック判定に
# 使われるのはHITの有無のみだが、種別を分けておくと後から個別に再走査できる)。
DEFAULT_AUTO_SCREENING_CHECK_TYPES = (
    ComplianceCheckType.SANCTIONS, ComplianceCheckType.ANTI_SOCIAL,
)


def run_compliance_check(
    session: Session, tenant_id: uuid.UUID, account: Account,
    check_type: ComplianceCheckType, screening: ScreeningPort,
) -> ComplianceStatus:
    """1回分のスクリーニングを実行し、ComplianceStatusをupsertして返す。"""
    result = screening.screen(account.name, check_type)

    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == tenant_id,
            ComplianceStatus.account_id == account.id,
            ComplianceStatus.check_type == check_type,
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if status is None:
        status = ComplianceStatus(
            tenant_id=tenant_id, account_id=account.id, check_type=check_type,
        )
        session.add(status)

    status.outcome = result.outcome
    status.provider = result.provider
    status.provider_request_id = result.provider_request_id
    status.checked_at = now
    status.valid_until = now + timedelta(days=FRESHNESS_WINDOW_DAYS)
    session.flush()
    return status


def ensure_account_screened(
    session: Session, tenant_id: uuid.UUID, account: Account, *,
    check_types: tuple[ComplianceCheckType, ...] = DEFAULT_AUTO_SCREENING_CHECK_TYPES,
    screening: ScreeningPort | None = None,
) -> list[ComplianceStatus]:
    """未実施または期限切れのチェック種別だけを自動で走らせる(C2-4/C2-5)。

    新規Account作成時、および見積作成前の鮮度チェックの両方から呼ばれる
    共通経路。既にfreshなComplianceStatusがあれば再スクリーニングしない
    (無駄なAPI呼び出しを避ける)。
    """
    screening = screening or MockScreeningAdapter()
    existing = {
        s.check_type: s for s in session.execute(
            select(ComplianceStatus).where(
                ComplianceStatus.tenant_id == tenant_id,
                ComplianceStatus.account_id == account.id,
                ComplianceStatus.check_type.in_(check_types),
            )
        ).scalars()
    }

    results: list[ComplianceStatus] = []
    for check_type in check_types:
        current = existing.get(check_type)
        if current is not None and current.is_fresh:
            results.append(current)
            continue
        results.append(run_compliance_check(session, tenant_id, account, check_type, screening))
    return results
