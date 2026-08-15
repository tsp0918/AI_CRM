"""取引先・エンドユーザーのworse-case-winsコンプライアンスゲート(2026-08-15)。

CRM_連携引き継ぎ書.md §5.2(エンドユーザーの分離管理)・§8.1(2系統のゲート)・
§8.3(商談・見積・契約の作成ブロック)を実装する。

新しい外部連携は不要 — 既存の`ComplianceStatus`(Account単位・チェック種別
単位、`/accounts/{id}/compliance-checks`と`/webhooks/sanctions-list-updated`
経由で既に送受信されている)をそのままゲートに使う。§8.3は「取引先が
HIT」「エンドユーザーがHIT」の2条件のみをブロック対象とするため、今回は
`ComplianceOutcome.HIT`のみを判定する(`NEEDS_REVIEW`/`BLOCKED`/`UNKNOWN`は
既存enumの意味を変えずに済むよう、今回のスコープには含めない — 将来の
拡張課題)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComplianceOutcome
from ..models import Account, ComplianceStatus

# 表示用の重大度順(重大 -> 軽微)。ブロック判定にはHITのみ使うが、UIバッジは
# 「その取引先の中で最も重大な状態」を一目で示したいためこの順序を使う。
_SEVERITY_ORDER = [
    ComplianceOutcome.HIT, ComplianceOutcome.NEEDS_REVIEW,
    ComplianceOutcome.BLOCKED, ComplianceOutcome.UNKNOWN, ComplianceOutcome.CLEAR,
]


def worst_compliance_outcome(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID,
) -> ComplianceOutcome | None:
    """当該Accountの全ComplianceStatus行の中で最も重大なoutcomeを返す。

    行が1件も無ければNone(=未実施。ブロック判定では「クリア」とは区別する
    が、§8.3はHITのみを見るため未実施はブロックしない)。
    """
    outcomes = set(session.execute(
        select(ComplianceStatus.outcome).where(
            ComplianceStatus.tenant_id == tenant_id,
            ComplianceStatus.account_id == account_id,
        )
    ).scalars())
    if not outcomes:
        return None
    for candidate in _SEVERITY_ORDER:
        if candidate in outcomes:
            return candidate
    return ComplianceOutcome.CLEAR


def check_party_clearance(
    session: Session, tenant_id: uuid.UUID, *,
    account_id: uuid.UUID, end_user_account_id: uuid.UUID | None = None,
) -> str | None:
    """取引先・エンドユーザーいずれかがHITならブロック理由(表示用メッセージ)
    を返す。クリア(またはHIT以外)なら None。

    `end_user_account_id`が未指定(取引先と同一)なら取引先のみ判定する
    (§5.2「同一の場合も明示的に送る」はAI_TM送信時の話であり、CRM側の
    ゲート判定は同一エンティティを二重にチェックしても意味が無い)。
    """
    if worst_compliance_outcome(session, tenant_id, account_id) == ComplianceOutcome.HIT:
        account = session.get(Account, account_id)
        name = account.name if account else str(account_id)
        return f"取引先「{name}」が制裁対象(HIT)のため、見積・契約を作成できません"

    if end_user_account_id is not None and end_user_account_id != account_id:
        if worst_compliance_outcome(session, tenant_id, end_user_account_id) == ComplianceOutcome.HIT:
            end_user = session.get(Account, end_user_account_id)
            name = end_user.name if end_user else str(end_user_account_id)
            return f"エンドユーザー「{name}」が制裁対象(HIT)のため、見積・契約を作成できません"

    return None
