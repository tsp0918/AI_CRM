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

2026-10-27追記(契約書CONTRACT_webhook_payloads.md §4-1 A案): 上記の
「新しい外部連携は不要」はゲート判定自体の話。別の問題として、CRM起点の
取引先が`POST /api/parties/resolve`を一度も呼ばれないため、AI_TM側の
`plat_party`・`MonitoringSubscription`が作られず継続監視に載らない
(`party.linked`をAI_TM側が実装しても対象のpartyが無ければ発火しない)。
これを埋めるため`enqueue_party_resolve()`を追加した — `run_compliance_check`
がSANCTIONSチェックを初めて走らせるタイミングから呼ぶ。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ComplianceCheckType, ComplianceOutcome, OutboxResult
from ..models import Account, ComplianceStatus, OutboxMessage
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher

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


MONITOR_WINDOW_DAYS = 365


def enqueue_party_resolve(
    session: Session, tenant_id: uuid.UUID, account: Account, *, actor: str,
) -> OutboxMessage:
    """AI_TM(platform-core)に`party_id`を解決させ、`Account.aitm_party_id`を
    埋めるためのOutbox起票(2026-10-27、契約書§4-1 A案)。

    `run_compliance_check`がそのAccountに対してSANCTIONSチェックを初めて
    走らせるタイミングから呼ぶ。`enable_watch=true`で送るため、AI_TM側は
    既存の`MonitoringSubscription`を冪等に再利用する(重複購読は増えない、
    platform-core/routers/party.py:102-118)。
    """
    ref = build_party_ref(account)
    monitor_until = (datetime.now(timezone.utc) + timedelta(days=MONITOR_WINDOW_DAYS)).date().isoformat()
    payload = {
        "legal_name": ref["legal_name"],
        "country_code": ref["country"],
        "party_type": "company",
        "source_system": "crm",
        "external_id": str(account.id),
        "tenant_id": str(tenant_id),
        "enable_watch": True,
        "monitor_until": monitor_until,
    }
    return enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.party.resolve",
        payload=payload, ref_type="account", ref_id=str(account.id), actor=actor,
    )


def _dispatch_aitm_party_resolve(session: Session, message: OutboxMessage) -> OutboxResult:
    """`register_aitm_party_dispatchers()`経由でOutbox処理から呼ばれる。
    `review_case.py`の`_dispatch_aitm_review_submit`がレスポンスの
    `case_no`を`provider_request_id`に書き戻すのと同じ形で、
    レスポンスの`party_id`を`Account.aitm_party_id`に書き戻す。
    """
    account = session.execute(
        select(Account).where(
            Account.tenant_id == message.tenant_id, Account.id == uuid.UUID(message.ref_id),
        )
    ).scalar_one_or_none()
    if account is None:
        return OutboxResult.FAILED_NO_RETRY

    client = SignedClient(
        os.environ.get("AITM_PLATFORM_URL"), message.tenant_id,
        bearer_env="AITM_PLATFORM_BEARER", secret_env="AITM_PLATFORM_SECRET",
    )
    try:
        response = client.post("/api/parties/resolve", message.payload, request_id=str(message.id))
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)
    finally:
        client.close()

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        data = response.json()
        party_id = data.get("party_id")
        if party_id:
            account.aitm_party_id = party_id
    return result


def register_aitm_party_dispatchers() -> None:
    """`AITM_PLATFORM_URL`が設定されている場合のみdispatcherを登録する
    (`register_aitm_dispatchers`と同じフェイルクローズパターン。
    platform-core(既定8000)は`AITM_SCREENING_URL`(8005)とは別モジュール
    なので環境変数を分けている)。
    """
    if os.environ.get("AITM_PLATFORM_URL"):
        register_dispatcher("aitm.party.resolve", _dispatch_aitm_party_resolve)
