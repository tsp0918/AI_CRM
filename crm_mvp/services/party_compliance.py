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

from ..enums import ComplianceCheckType, ComplianceOutcome
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


_COMMERCE_CHECK_TYPES = (ComplianceCheckType.CREDIT, ComplianceCheckType.ANTI_SOCIAL)


def check_commerce_clearance(session: Session, tenant_id: uuid.UUID, *, account_id: uuid.UUID) -> str | None:
    """ERP商流ゲート(IF-32、§6.8/§8.1)の`ng`(=`ComplianceOutcome.BLOCKED`)を
    見積送付・契約締結のハード遮断として扱う。`check_party_clearance`が見る
    HIT(制裁)とは意味が異なる別種の懸念のため、独立した関数にする。
    """
    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == tenant_id,
            ComplianceStatus.account_id == account_id,
            ComplianceStatus.check_type.in_(_COMMERCE_CHECK_TYPES),
            ComplianceStatus.outcome == ComplianceOutcome.BLOCKED,
        )
    ).scalars().first()
    if status is None:
        return None
    account = session.get(Account, account_id)
    name = account.name if account else str(account_id)
    return f"取引先「{name}」の商流ゲート(与信・反社)がNGのため、見積送付・契約締結できません"


def build_party_ref(account: Account) -> dict:
    """AI_TM送出用の`party_ref`構造体(CRM_連携引き継ぎ書.md §5.1)を組み立てる。

    社名文字列を照合キーにせず、`aitm_party_id`(既知なら)・`erp_bp_code`
    (`external_system=="erp"`の場合の`external_id`)・CRM側Account IDを
    併せて送ることで、AI_TM側の名寄せ処理をスキップ/高速化できる。
    """
    erp_bp_code = account.external_id if account.external_system == "erp" else None
    return {
        "source_system": "crm",
        "crm_account_id": str(account.id),
        "erp_bp_code": erp_bp_code,
        "aitm_party_id": account.aitm_party_id,
        "legal_name": account.name,
        "legal_name_local": account.name,
        "country": account.country,
        "address": None,
        "aliases": [],
    }
