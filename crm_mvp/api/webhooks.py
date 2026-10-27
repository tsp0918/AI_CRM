"""外部システムからの Webhook 受信(HANDOVER.md §5 Phase5, item 20,21)。

全エンドポイントが`verify_webhook`(署名・Bearer検証)に接続済み
(2026-10-25、`/compliance-judgment`・`/sanctions-list-updated`の2つが
X-Tenant-Idのみで送信元を検証していない状態だったのを最後に接続した)。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..enums import (
    ComplianceCheckType, ComplianceOutcome, ReviewCaseStatus, Stage,
    WebhookEventResult,
)
from ..models import Account, ComplianceStatus, Contract, Engagement, ReviewCase, WebhookEvent
from ..services.action_items import create_manual_action_item
from .deps import get_session, get_tenant_id, get_tenant_scoped_session
from .webhook_security import record_webhook_event, verify_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _mark_event_result(event, result: WebhookEventResult, *, error: str | None = None) -> None:
    """ERROR/STALEは受信側で黙って握ると送信元からは成功に見えてしまう
    (`/webhooks/*`は常に200/204/201を返すため)。2026-10-26クロスリポジトリ
    レビューで、これが原因でIF-10の取り落としに誰も気づけない状態だったと
    判明したため、ログに残す(A-3、監視で拾えるようにする)。"""
    event.result = result
    if error:
        event.error = error
    if result in (WebhookEventResult.STALE, WebhookEventResult.ERROR):
        logger.warning(
            "webhook %s/%s -> %s (event_id=%s%s)",
            event.source_system, event.event_type, result.value, event.event_id,
            f", error={error}" if error else "",
        )


def _parse_valid_until(raw: str) -> datetime:
    """`ReviewCase.valid_until`は`DateTime(timezone=True)`列。AI_TMの
    overrideは`YYYY-MM-DD`の日付文字列(tzinfo無し)で送ってくることがあり、
    `datetime.fromisoformat`がnaive datetimeを返すとセッションのタイム
    ゾーンに依存して解釈され実質ずれる(2026-10-26クロスリポジトリ
    レビューで指摘)。naiveな場合はUTCとみなしてtzinfoを補う。
    """
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ComplianceJudgmentPayload(BaseModel):
    account_id: uuid.UUID
    check_type: ComplianceCheckType
    outcome: ComplianceOutcome
    provider: str
    provider_request_id: str | None = None
    evidence_uri: str | None = None
    evidence_hash: str | None = None
    valid_until: datetime | None = None


@router.post("/compliance-judgment", status_code=204)
async def receive_compliance_judgment(request: Request, session: Session = Depends(get_session)) -> None:
    """2026-10-25: 署名検証(`verify_webhook`)に接続した(クロスリポジトリ
    レビューで、本エンドポイントが`X-Tenant-Id`のみで送信元の真正性を
    検証していない点が指摘されたための対応)。パースは引き続き
    `ComplianceJudgmentPayload`で行うが、生ボディを先に署名検証してから
    パースする(他7エンドポイントと同じ順序)。

    2026-10-27 非推奨(契約書§3): AI_TM側にこのエンドポイントへの送信
    実装が存在せず、今後も作られない(誤実装の受信口をAI_TM側が撤去した
    際に送信側も作られなかった)。アカウント単位の判定は`screening.alert`
    (`/webhooks/aitm/party-event`経由)でカバーする。AI_TM側の撤去完了を
    確認してからルート自体を削除する — `WebhookEvent`履歴と撤去タイミング
    のずれを吸収するため、今回はdocstringのみの変更にとどめる。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_COMPLIANCE_WEBHOOK_SECRET",
        bearer_env="AITM_COMPLIANCE_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )
    body = ComplianceJudgmentPayload.model_validate(ctx.payload)

    # `event_id`はDB上VARCHAR(64)。account_id(UUID36字)+check_type+
    # provider_request_idを素朴に連結すると送信元のIDが長い場合に
    # 64字を超えてDataErrorになる(2026-10-26、実際にBEARER検証確認中に
    # 発生)ため、フォールバック時はハッシュ化して長さを固定する。
    if ctx.payload.get("event_id"):
        event_id = str(ctx.payload["event_id"])[:64]
    else:
        raw_key = f"{body.account_id}:{body.check_type.value}:{body.provider_request_id}"
        event_id = f"cj:{hashlib.sha256(raw_key.encode()).hexdigest()[:48]}"
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id, source_system="aitm",
        event_type="compliance.judgment", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return

    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == ctx.tenant_id,
            ComplianceStatus.account_id == body.account_id,
            ComplianceStatus.check_type == body.check_type,
        )
    ).scalar_one_or_none()
    if status is None:
        status = ComplianceStatus(
            tenant_id=ctx.tenant_id, account_id=body.account_id,
            check_type=body.check_type,
        )
        session.add(status)

    status.outcome = body.outcome
    status.provider = body.provider
    status.provider_request_id = body.provider_request_id
    status.evidence_uri = body.evidence_uri
    status.evidence_hash = body.evidence_hash
    status.checked_at = datetime.now(timezone.utc)
    status.valid_until = body.valid_until
    session.commit()


class SanctionsHit(BaseModel):
    account_id: uuid.UUID
    matched_list: str
    matched_entity_name: str


class SanctionsListUpdatedPayload(BaseModel):
    hits: list[SanctionsHit]


class WebhookReceiptOut(BaseModel):
    """受理のみを返す。業務データ(対象アカウント・商談等)は含めない —
    2026-08-15: CRM_連携引き継ぎ書.md §7.2「Webhookのレスポンスに業務
    データを載せないこと」に合わせた変更。以前はaffected_engagements
    をレスポンスで返していたが、リトライ時に副作用が読めなくなる
    (呼び出し側がレスポンスを再利用できない)ため、通知はActionItem
    としてCRM側に永続化する形に変更した(下記SANCTIONS_ACTION_ASSIGNEE
    参照)。"""

    status: str = "processed"
    hits_processed: int


NON_TERMINAL_STAGES = [
    Stage.LEAD, Stage.PROSPECT, Stage.QUALIFIED, Stage.PROPOSAL,
    Stage.NEGOTIATION,
]

SANCTIONS_ACTION_ASSIGNEE = "輸出管理チーム"


@router.post("/sanctions-list-updated", response_model=WebhookReceiptOut)
async def receive_sanctions_list_update(request: Request, session: Session = Depends(get_session)) -> WebhookReceiptOut:
    """制裁リスト更新時の遡及再評価(HANDOVER.md §5 item21)。

    ヒットしたアカウントの ComplianceStatus を HIT に更新し、進行中
    (CLOSED_* でない)の Engagement ごとに ActionItem を起票する。
    レスポンスには業務データを含めない(WebhookReceiptOut参照) — 「誰が
    見ても分かる形」の通知はActionItemとして画面上に残す。

    2026-10-25: 署名検証(`verify_webhook`)に接続した。複数取引先を一括
    更新する経路のため優先的に対応(クロスリポジトリレビューで指摘)。
    併せて`record_webhook_event`によるイベント単位の冪等性も追加した
    (署名検証が無かった間はリトライ時に重複ActionItemが積まれていた)。

    2026-10-27 非推奨(契約書§3): 個別`screening.alert`
    (`/webhooks/aitm/party-event`経由)に一本化する方針に決定した。
    AI_TM側に`hits[]`を束ねて送る実装が存在せず、今後も作られない。
    AI_TM側の撤去完了を確認してからルート自体を削除する — 今回は
    docstringのみの変更にとどめる。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_SANCTIONS_WEBHOOK_SECRET",
        bearer_env="AITM_SANCTIONS_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )
    body = SanctionsListUpdatedPayload.model_validate(ctx.payload)
    tenant_id = ctx.tenant_id

    # `hash()`は文字列/タプルに対してプロセスごとにソルトされる
    # (PYTHONHASHSEED既定はrandom)ため、CRM再起動後に同じ通知が再送される
    # と別のevent_idになり冪等性が効かない(2026-10-26クロスリポジトリ
    # レビューで指摘)。compliance-judgment側と同じhashlib.sha256に揃える。
    if ctx.payload.get("event_id"):
        event_id = str(ctx.payload["event_id"])[:64]
    else:
        raw_key = ":".join(sorted(str(h.account_id) for h in body.hits))
        event_id = f"sanctions:{hashlib.sha256(raw_key.encode()).hexdigest()[:48]}"
    event, is_new = record_webhook_event(
        session, tenant_id, event_id=event_id, source_system="aitm",
        event_type="sanctions.list_updated", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return WebhookReceiptOut(hits_processed=0)

    now = datetime.now(timezone.utc)

    for hit in body.hits:
        account = session.get(Account, hit.account_id)
        if account is None or account.tenant_id != tenant_id:
            continue

        status = session.execute(
            select(ComplianceStatus).where(
                ComplianceStatus.tenant_id == tenant_id,
                ComplianceStatus.account_id == account.id,
                ComplianceStatus.check_type == ComplianceCheckType.SANCTIONS,
            )
        ).scalar_one_or_none()
        if status is None:
            status = ComplianceStatus(
                tenant_id=tenant_id, account_id=account.id,
                check_type=ComplianceCheckType.SANCTIONS,
            )
            session.add(status)
        status.outcome = ComplianceOutcome.HIT
        status.provider = "sanctions-list-monitor"
        status.detail = {
            "matched_list": hit.matched_list,
            "matched_entity_name": hit.matched_entity_name,
        }
        status.checked_at = now
        status.valid_until = now

        engagements = session.execute(
            select(Engagement).where(
                Engagement.tenant_id == tenant_id,
                Engagement.account_id == account.id,
                Engagement.stage.in_(NON_TERMINAL_STAGES),
            )
        ).scalars().all()
        for engagement in engagements:
            create_manual_action_item(
                session, tenant_id, engagement.id,
                assigned_to=SANCTIONS_ACTION_ASSIGNEE,
                task=(
                    f"制裁リスト更新: {hit.matched_entity_name}"
                    f"({hit.matched_list})への該当が検知されました。"
                    "取引先・商談の取り扱いを確認してください。"
                ),
                assigned_by="system:sanctions-webhook",
            )

    session.commit()
    return WebhookReceiptOut(hits_processed=len(body.hits))


@router.post("/sanctions-sync-completed", status_code=204)
async def receive_sanctions_sync_completed(request: Request, session: Session = Depends(get_session)) -> None:
    """制裁リストの定期同期が完了した通知(2026-10-27、契約書§2-1、B-2)。

    業務影響・ActionItemは無し — `WebhookEvent`に記録するだけのログ目的
    (`/ui/integration-status`・`GET /webhooks/webhook-events`から確認できれば
    目的を満たす)。既存の`/sanctions-list-updated`と同じチャネル
    (`AITM_SANCTIONS_WEBHOOK_{SECRET,BEARER}`)を流用する。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_SANCTIONS_WEBHOOK_SECRET",
        bearer_env="AITM_SANCTIONS_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )
    source = str(ctx.payload.get("source", ""))
    synced_at = str(ctx.payload.get("synced_at", ""))
    event_id = str(ctx.payload.get("event_id") or f"sanctions-sync:{source}:{synced_at}")[:64]

    _event, _is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id, source_system="aitm",
        event_type="sanctions.sync_completed", payload=ctx.payload,
    )
    session.commit()


REVIEW_ACTION_ASSIGNEE = "輸出管理チーム"
_UNRESOLVED_STATUSES = (
    ReviewCaseStatus.HIT, ReviewCaseStatus.BLOCKED, ReviewCaseStatus.NEEDS_REVIEW,
)


@router.post("/aitm/review-result")
async def receive_aitm_review_result(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """AI_TMからの取引審査ケース判定結果(2026-08-15, IF-10相当・§7.4)。

    `/webhooks/compliance-judgment`はアカウント単位のComplianceStatus更新用、
    こちらはReviewCase(見積・契約という取引の1版)単位の判定結果通知用で
    別エンドポイントにしている(CRM_連携_実装計画.md Phase 1a)。

    Phase 0で単体実装済みの`verify_webhook`/`record_webhook_event`をここで
    初めて実ルートに接続する。テナント文脈は署名検証済みの`ctx.tenant_id`を
    正とする(X-Tenant-Idヘッダの二重パースを避けるため、既存2エンドポイント
    と異なりDepends(get_tenant_id)は使わない)。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_REVIEW_WEBHOOK_SECRET",
        bearer_env="AITM_REVIEW_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    case_no = str(ctx.payload.get("case_no", ""))
    revision = int(ctx.payload.get("revision", 0))
    event_id = str(ctx.payload.get("event_id") or f"{case_no}:{revision}")

    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id, source_system="aitm",
        event_type="review.judged", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    # 2026-10-27: AI_TMが送るcase_noはAI_TM採番値(provider_request_idに
    # 保存済み、_dispatch_aitm_review_submit参照)。CRM採番のcase_no列で
    # 引くと常にヒットせず、IF-10が100%ERRORになっていた(クロスリポジトリ
    # レビューで発覚)。CRM採番へのfallbackは入れない — 「どちらの採番値で
    # 引いているか」を曖昧にすると同じ事故を再発させる。
    review_case = session.execute(
        select(ReviewCase).where(
            ReviewCase.tenant_id == ctx.tenant_id, ReviewCase.provider_request_id == case_no,
        )
    ).scalar_one_or_none()
    if review_case is None:
        _mark_event_result(
            event, WebhookEventResult.ERROR,
            error=f"該当する ReviewCase が見つかりません (AI_TM case_no={case_no})",
        )
        session.commit()
        return {"status": "processed"}

    if revision <= review_case.revision:
        _mark_event_result(event, WebhookEventResult.STALE)
        session.commit()
        return {"status": "processed"}

    try:
        status = ReviewCaseStatus(ctx.payload.get("status", ""))
    except ValueError:
        status = ReviewCaseStatus.NEEDS_REVIEW

    review_case.status = status
    review_case.revision = revision
    review_case.detail = ctx.payload.get("detail") or {}
    review_case.decided_at = datetime.now(timezone.utc)
    valid_until_raw = ctx.payload.get("valid_until")
    if valid_until_raw:
        review_case.valid_until = _parse_valid_until(valid_until_raw)

    if status in _UNRESOLVED_STATUSES:
        create_manual_action_item(
            session, ctx.tenant_id, review_case.engagement_id,
            assigned_to=REVIEW_ACTION_ASSIGNEE,
            task=f"取引審査ケース {case_no} の判定結果: {status.value}。内容を確認してください。",
            assigned_by="system:aitm-review-webhook",
        )

    session.commit()
    return {"status": "processed"}


@router.post("/override-applied")
async def receive_override_applied(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """AI_TMからのコンプライアンス・オーバーライド適用通知(2026-10-25、IF-15)。

    `/webhooks/aitm/review-result`とは別チャネル — オーバーライドは
    revisionベースの判定更新ではなく、責任者承認による例外的な通過なので、
    「誰が・どの権限で・いつまで」を`ReviewCase.detail`に残し、監査証跡
    として`engagement_detail.html`から確認できるようにする(通常のAI判定
    結果と見分けがつかなくなることを避ける — 2026-10-25のクロスリポジトリ
    レビューで「CRM側がoverride経由の通過を一切記録・表示していない」と
    判明したための対応)。

    このwebhookが適用した時点でケースはCLEARとして扱う(オーバーライドの
    目的自体が「rejected/needs_reviewだった取引を責任者承認で通す」ため)。
    正式審査へのoverride継承を防ぐかどうかはAI_TM側の判断領域であり、
    ここでは関与しない(CRMは`parent_case_no`経由でAI_TM採番のcase_noを
    引き渡すだけ)。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_OVERRIDE_WEBHOOK_SECRET",
        bearer_env="AITM_OVERRIDE_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    case_no = str(ctx.payload.get("case_no", ""))
    override_id = ctx.payload.get("override_id")
    event_id = str(ctx.payload.get("event_id") or f"{case_no}:override:{override_id}")

    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id, source_system="aitm",
        event_type="override.applied", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    # 2026-10-27: receive_aitm_review_resultと同じ理由でprovider_request_id
    # で引く(AI_TM採番値)。CRM採番のcase_no列へのfallbackは入れない。
    review_case = session.execute(
        select(ReviewCase).where(
            ReviewCase.tenant_id == ctx.tenant_id, ReviewCase.provider_request_id == case_no,
        )
    ).scalar_one_or_none()
    if review_case is None:
        _mark_event_result(
            event, WebhookEventResult.ERROR,
            error=f"該当する ReviewCase が見つかりません (AI_TM case_no={case_no})",
        )
        session.commit()
        return {"status": "processed"}

    override_detail = {
        "override_id": override_id,
        "scope": ctx.payload.get("scope"),
        "overridden_by": ctx.payload.get("overridden_by"),
        "approver_name": ctx.payload.get("approver_name"),
        "valid_until": ctx.payload.get("valid_until"),
        "department_head_approval": ctx.payload.get("department_head_approval"),
        "transaction_id": ctx.payload.get("transaction_id"),
    }
    review_case.status = ReviewCaseStatus.CLEAR
    review_case.detail = {**(review_case.detail or {}), "override": override_detail}
    review_case.decided_at = datetime.now(timezone.utc)
    valid_until_raw = ctx.payload.get("valid_until")
    if valid_until_raw:
        review_case.valid_until = _parse_valid_until(valid_until_raw)

    create_manual_action_item(
        session, ctx.tenant_id, review_case.engagement_id,
        assigned_to=REVIEW_ACTION_ASSIGNEE,
        task=(
            f"取引審査ケース {case_no} は責任者承認(オーバーライド)により通過しました。"
            f"承認者: {override_detail['approver_name']}、"
            f"適用範囲: {override_detail['scope']}、"
            f"有効期限: {override_detail['valid_until']}。"
            "通常のAI判定ではなく例外承認である点に留意してください。"
        ),
        assigned_by="system:aitm-override-webhook",
    )

    session.commit()
    return {"status": "processed"}


def _resolve_party_account(
    session: Session, tenant_id: uuid.UUID, payload: dict,
) -> Account | None:
    """`crm_account_id`(優先)または`aitm_party_id`からAccountを解決する。"""
    crm_account_id = payload.get("crm_account_id")
    if crm_account_id:
        try:
            account = session.get(Account, uuid.UUID(crm_account_id))
        except ValueError:
            account = None
        if account is not None and account.tenant_id == tenant_id:
            return account

    aitm_party_id = payload.get("aitm_party_id")
    if aitm_party_id:
        return session.execute(
            select(Account).where(
                Account.tenant_id == tenant_id, Account.aitm_party_id == aitm_party_id,
            )
        ).scalar_one_or_none()
    return None


@router.post("/aitm/party-event")
async def receive_aitm_party_event(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """AI_TMからの取引先イベント受信(2026-08-15, IF-11相当・§7.4)。

    `event_type`で2種類を多重化する(CRM_連携_実装計画.md Phase 2追補):
    - `party.linked`: CRM発生の取引先がERPへ後日登録され、AI_TM側の名寄せで
      同一partyとマージされた通知(§5.1)。`external_system`/`external_id`と
      `aitm_party_id`をCRM側に反映する。
    - `screening.alert`: 継続監視中の取引先で新たな懸念が検出された通知。
      該当Accountの`ComplianceStatus`を更新し、進行中の商談にActionItemを
      起票する(`/webhooks/sanctions-list-updated`と同じ設計原則4)。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_PARTY_WEBHOOK_SECRET",
        bearer_env="AITM_PARTY_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    event_id = str(ctx.payload.get("event_id") or "")
    event_type = str(ctx.payload.get("event_type") or "")
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id or f"{event_type}:{datetime.now(timezone.utc).isoformat()}",
        source_system="aitm", event_type=event_type or "party_event", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    account = _resolve_party_account(session, ctx.tenant_id, ctx.payload)
    if account is None:
        _mark_event_result(event, WebhookEventResult.ERROR, error="該当するAccountが見つかりません")
        session.commit()
        return {"status": "processed"}

    if event_type == "party.linked":
        erp_bp_code = ctx.payload.get("erp_bp_code")
        if erp_bp_code:
            account.external_system = "erp"
            account.external_id = erp_bp_code
        aitm_party_id = ctx.payload.get("aitm_party_id")
        if aitm_party_id:
            account.aitm_party_id = aitm_party_id

    elif event_type == "screening.alert":
        try:
            check_type = ComplianceCheckType(ctx.payload.get("check_type", ""))
        except ValueError:
            check_type = ComplianceCheckType.SANCTIONS
        try:
            outcome = ComplianceOutcome(ctx.payload.get("outcome", ""))
        except ValueError:
            outcome = ComplianceOutcome.NEEDS_REVIEW

        status = session.execute(
            select(ComplianceStatus).where(
                ComplianceStatus.tenant_id == ctx.tenant_id,
                ComplianceStatus.account_id == account.id,
                ComplianceStatus.check_type == check_type,
            )
        ).scalar_one_or_none()
        if status is None:
            status = ComplianceStatus(
                tenant_id=ctx.tenant_id, account_id=account.id, check_type=check_type,
            )
            session.add(status)
        now = datetime.now(timezone.utc)
        status.outcome = outcome
        status.provider = "aitm"
        status.detail = ctx.payload.get("detail") or {}
        status.checked_at = now
        status.valid_until = now

        if outcome in (ComplianceOutcome.HIT, ComplianceOutcome.NEEDS_REVIEW):
            engagements = session.execute(
                select(Engagement).where(
                    Engagement.tenant_id == ctx.tenant_id,
                    Engagement.account_id == account.id,
                    Engagement.stage.in_(NON_TERMINAL_STAGES),
                )
            ).scalars().all()
            for engagement in engagements:
                create_manual_action_item(
                    session, ctx.tenant_id, engagement.id,
                    assigned_to=SANCTIONS_ACTION_ASSIGNEE,
                    task=(
                        f"継続監視アラート: 取引先「{account.name}」で"
                        f"{check_type.value}の懸念が検出されました({outcome.value})。"
                        "取引先・商談の取り扱いを確認してください。"
                    ),
                    assigned_by="system:aitm-party-webhook",
                )

    session.commit()
    return {"status": "processed"}


@router.post("/rnd-opportunity")
async def receive_rnd_opportunity(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-14: R&D案件からの商談自動作成(§7.5)。専用ステージ
    `Stage.RND_INCUBATION`に隔離し、パイプライン予実集計から除外する。
    """
    from ..services.rnd_opportunity import process_rnd_opportunity

    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_RND_WEBHOOK_SECRET",
        bearer_env="AITM_RND_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    rnd_case_id = str(ctx.payload.get("rnd_case_id") or "")
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=rnd_case_id or f"rnd:{datetime.now(timezone.utc).isoformat()}",
        source_system="aitm", event_type="rnd.opportunity", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    process_rnd_opportunity(session, ctx.tenant_id, ctx.payload)
    session.commit()
    return {"status": "processed"}


@router.post("/deemed-export-risk")
async def receive_deemed_export_risk(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-13: みなし輸出リスクの判定結果(§6.7・§7.3)。取引先に注意フラグを
    立て、輸出管理部門への確認ActionItemを起票する。既存の`ComplianceStatus`
    (`check_type=export_control`)を「みなし輸出注意」フラグの置き場として
    再利用する — 新規のAccount列は追加しない。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_DEEMED_EXPORT_WEBHOOK_SECRET",
        bearer_env="AITM_DEEMED_EXPORT_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    event_id = str(ctx.payload.get("event_id") or "")
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id or f"deemed-export:{datetime.now(timezone.utc).isoformat()}",
        source_system="aitm", event_type="deemed_export.risk", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    account = _resolve_party_account(session, ctx.tenant_id, ctx.payload)
    if account is None:
        _mark_event_result(event, WebhookEventResult.ERROR, error="該当するAccountが見つかりません")
        session.commit()
        return {"status": "processed"}

    now = datetime.now(timezone.utc)
    status = session.execute(
        select(ComplianceStatus).where(
            ComplianceStatus.tenant_id == ctx.tenant_id,
            ComplianceStatus.account_id == account.id,
            ComplianceStatus.check_type == ComplianceCheckType.EXPORT_CONTROL,
        )
    ).scalar_one_or_none()
    if status is None:
        status = ComplianceStatus(
            tenant_id=ctx.tenant_id, account_id=account.id,
            check_type=ComplianceCheckType.EXPORT_CONTROL,
        )
        session.add(status)
    status.outcome = ComplianceOutcome.NEEDS_REVIEW
    status.provider = "aitm"
    status.detail = ctx.payload.get("detail") or {}
    status.checked_at = now
    status.valid_until = now

    engagements = session.execute(
        select(Engagement).where(
            Engagement.tenant_id == ctx.tenant_id, Engagement.account_id == account.id,
            Engagement.stage.in_(NON_TERMINAL_STAGES),
        )
    ).scalars().all()
    for engagement in engagements:
        create_manual_action_item(
            session, ctx.tenant_id, engagement.id, assigned_to=SANCTIONS_ACTION_ASSIGNEE,
            task=(
                f"みなし輸出リスク: 取引先「{account.name}」との技術情報授受に"
                f"注意が必要です({ctx.payload.get('reason', '詳細はAI_TM側を確認')})。"
            ),
            assigned_by="system:aitm-deemed-export-webhook",
        )

    session.commit()
    return {"status": "processed"}


@router.post("/contract-monitoring")
async def receive_contract_monitoring(
    request: Request, session: Session = Depends(get_session),
) -> dict:
    """IF-16: 契約の継続監視アラート(§7.3)。`Contract.monitoring_alert`を
    立てる — `create_child_engagement`(継続/Upsell/Cross-sell)がこれを見て
    renewal商談の起票をブロックする。
    """
    ctx = await verify_webhook(
        request, source="aitm", secret_env="AITM_MONITORING_WEBHOOK_SECRET",
        bearer_env="AITM_MONITORING_WEBHOOK_BEARER",
    )
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(ctx.tenant_id)},
    )

    event_id = str(ctx.payload.get("event_id") or "")
    event, is_new = record_webhook_event(
        session, ctx.tenant_id, event_id=event_id or f"monitoring:{datetime.now(timezone.utc).isoformat()}",
        source_system="aitm", event_type="contract.monitoring_alert", payload=ctx.payload,
    )
    if not is_new:
        session.commit()
        return {"status": "duplicate"}

    contract_id = ctx.payload.get("crm_contract_id")
    contract = session.get(Contract, uuid.UUID(contract_id)) if contract_id else None
    if contract is None or contract.tenant_id != ctx.tenant_id:
        _mark_event_result(event, WebhookEventResult.ERROR, error="該当するContractが見つかりません")
        session.commit()
        return {"status": "processed"}

    contract.monitoring_alert = True
    contract.monitoring_alert_detail = ctx.payload.get("detail") or {}

    create_manual_action_item(
        session, ctx.tenant_id, contract.engagement_id, assigned_to=SANCTIONS_ACTION_ASSIGNEE,
        task=(
            f"契約 {contract.contract_number} に継続監視アラートが発生しました。"
            "この契約からの更新商談の起票がブロックされます。内容を確認してください。"
        ),
        assigned_by="system:aitm-monitoring-webhook",
    )

    session.commit()
    return {"status": "processed"}


class WebhookEventOut(BaseModel):
    # ORMオブジェクト(WebhookEvent)をそのまま返すため必須。FastAPI 0.141系は
    # response_model検証時にfrom_attributes=Trueを内部的に付与するため実害は
    # 出ていなかったが、将来のFastAPIバージョン変更や単体でのmodel_validate()
    # 呼び出しに対しても安全にするため明示する。
    model_config = {"from_attributes": True}

    id: uuid.UUID
    event_id: str
    source_system: str
    event_type: str
    result: str
    error: str | None
    received_at: datetime
    processed_at: datetime | None


@router.get("/webhook-events", response_model=list[WebhookEventOut])
def list_webhook_events(
    result: str | None = None,
    source_system: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_tenant_scoped_session),
) -> list[WebhookEvent]:
    """受信Webhookの処理結果をJSONで参照する(2026-10-26、A-3)。

    `/ui/integration-status`はHTMLのみでテスト/自動検証から読めないため、
    「IF-10がSTALEで握られていないこと」等をPostmanのテストスクリプトから
    直接アサートできるようにする(クロスリポジトリレビューで指摘)。
    """
    query = select(WebhookEvent).where(WebhookEvent.tenant_id == tenant_id)
    if result:
        query = query.where(WebhookEvent.result == result)
    if source_system:
        query = query.where(WebhookEvent.source_system == source_system)
    if event_type:
        query = query.where(WebhookEvent.event_type == event_type)
    query = query.order_by(WebhookEvent.received_at.desc()).limit(min(limit, 200))
    return session.execute(query).scalars().all()
