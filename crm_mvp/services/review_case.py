"""AI_TMへの取引審査(仮審査・正式審査)の起票(2026-08-15)。

CRM_連携引き継ぎ書.md §5(2段階審査の設計)・§6(送信側実装)を実装する。
送信自体はOutbox(crm_mvp/services/outbox.py)経由で非同期に行う —
見積・契約の成立をAI_TMの可用性に依存させないため(§3.3-2)。

品目マッピング(Product.erp_material_id)が未設定の商品を含む見積・契約は
審査を起票せず、ActionItemだけを起票する(§5.4)。これは異常系ではなく
仕様であり、見積・契約の作成自体は妨げない。

金額バケット化について: 引き継ぎ書はUSD換算後にバケット化する設計だが、
本コードベースは通貨換算機能を持たない。ここでは通貨コードをハッシュ
キーに含めることで代替する(同一通貨内の重複検知は機能するが、異なる
通貨間での実質同額の検知はできない — 既知の簡略化。将来FX対応時に
再検討する)。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import ArtifactType, OutboxResult, ReviewCaseStatus, ReviewType
from ..models import (
    Account, Contract, ContractLineItem, Engagement, ErpMaterial, OutboxMessage,
    Product, Quote, QuoteLineItem, ReviewCase,
)
from .action_items import create_manual_action_item
from .integration_client import SignedClient
from .outbox import classify_http_response, enqueue_outbox, register_dispatcher
from .quoting import list_contract_line_items, list_quote_line_items

REVIEW_ACTION_ASSIGNEE = "輸出管理チーム"


def _resolve_material_codes(
    session: Session, tenant_id: uuid.UUID,
    line_items: Sequence[QuoteLineItem | ContractLineItem],
) -> list[tuple[str, float]] | None:
    """明細の`product_id`から`ErpMaterial.material_code`を解決する。

    1件でも`product_id`未設定、または`Product.erp_material_id`未設定の
    品目マッピング欠落があれば None を返す(§5.4: 審査を起票しない判定)。
    """
    product_ids = {li.product_id for li in line_items if li.product_id}
    products = {
        p.id: p for p in session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id, Product.id.in_(product_ids),
            )
        ).scalars()
    } if product_ids else {}

    material_ids = {
        p.erp_material_id for p in products.values() if p.erp_material_id
    }
    materials = {
        m.id: m for m in session.execute(
            select(ErpMaterial).where(
                ErpMaterial.tenant_id == tenant_id, ErpMaterial.id.in_(material_ids),
            )
        ).scalars()
    } if material_ids else {}

    codes: list[tuple[str, float]] = []
    for li in line_items:
        product = products.get(li.product_id) if li.product_id else None
        material = (
            materials.get(product.erp_material_id)
            if product is not None and product.erp_material_id else None
        )
        if material is None:
            return None
        codes.append((material.material_code, float(li.quantity)))
    return codes


def build_review_key_hash(
    *, line_item_codes: list[tuple[str, float]], destination_country: str | None,
    end_user_account_id: uuid.UUID | None, end_use: str | None,
    total_amount: Decimal, currency: str,
) -> str:
    bucket = Decimal(os.environ.get("REVIEW_KEY_VALUE_BUCKET", "100000"))
    parts = [
        json.dumps(sorted(line_item_codes), ensure_ascii=False),
        destination_country or "",
        str(end_user_account_id) if end_user_account_id else "",
        (end_use or "").strip(),
        currency,
        str(int(total_amount // bucket)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _build_payload(
    *, title: str, parent_case_no: str | None,
    line_item_codes: list[tuple[str, float]], destination_country: str | None,
    end_use: str | None, total_amount: Decimal,
    engagement_id: uuid.UUID, quote_id: uuid.UUID | None, contract_id: uuid.UUID | None,
    counterparty: Account | None, end_user: Account | None,
) -> dict:
    """AI_TM `ai_validation`モジュールの実API契約に合わせたペイロード
    (2026-08-16、実サービスへのE2E疎通確認で判明した実際のスキーマ)。

    引き継ぎ書の設計(`counterparty_ref`構造体・CRM側でのcase_no採番)とは
    異なり、実際のAI_TMは`counterparty_name`(文字列)のみを求め、
    **case_noはAI_TM側が採番して返す**(CRM側が送るのではない)。
    真のFX換算が無いため`total_value_usd`は円建て等でも生の金額をそのまま
    渡す(既知の簡略化、モジュールdocstring参照)。
    """
    return {
        "title": title,
        "counterparty_name": counterparty.name if counterparty else None,
        "destination_country": destination_country or "",
        "end_user_party_id": end_user.aitm_party_id if end_user else None,
        "end_use": end_use or "",
        "total_value_usd": float(total_amount),
        "products": [
            {"product_code": code, "quantity": qty} for code, qty in line_item_codes
        ],
        "revision": 1,
        "crm_quote_id": str(quote_id) if quote_id else None,
        "crm_engagement_id": str(engagement_id),
        **({"parent_case_no": parent_case_no} if parent_case_no else {}),
        **({"crm_contract_id": str(contract_id)} if contract_id else {}),
    }


def submit_provisional_review(
    session: Session, tenant_id: uuid.UUID, quote: Quote, engagement: Engagement, *,
    actor: str,
) -> ReviewCase | None:
    """見積作成フック(§5 仮審査)。未マッピング品目があれば None を返し、
    ActionItemのみ起票する(見積作成自体は既に成立している前提で呼ばれる)。
    """
    line_items = list_quote_line_items(session, tenant_id, quote.id)
    codes = _resolve_material_codes(session, tenant_id, line_items)
    if codes is None:
        create_manual_action_item(
            session, tenant_id, engagement.id, assigned_to=REVIEW_ACTION_ASSIGNEE,
            task=(
                f"見積 {quote.quote_number} に品目マッピング(ERP品目コード)未設定の商品が"
                "含まれるため、輸出審査を起票できませんでした。商品マスタでERP品目を"
                "紐付けてください。"
            ),
            assigned_by="system:review-case",
        )
        return None

    review_key_hash = build_review_key_hash(
        line_item_codes=codes, destination_country=quote.destination_country,
        end_user_account_id=quote.end_user_account_id, end_use=quote.end_use,
        total_amount=quote.total_amount, currency=quote.currency,
    )
    case_no = f"CRM-{quote.quote_number}"

    review_case = ReviewCase(
        tenant_id=tenant_id, case_no=case_no, review_type=ReviewType.PROVISIONAL,
        artifact_type=ArtifactType.QUOTE, quote_id=quote.id,
        engagement_id=engagement.id, review_key_hash=review_key_hash,
        status=ReviewCaseStatus.PENDING, written_by=actor,
    )
    session.add(review_case)
    session.flush()

    counterparty = session.get(Account, engagement.account_id)
    end_user = (
        session.get(Account, quote.end_user_account_id)
        if quote.end_user_account_id else None
    )
    payload = _build_payload(
        title=f"{engagement.name} - {quote.quote_number}", parent_case_no=None,
        line_item_codes=codes, destination_country=quote.destination_country,
        end_use=quote.end_use, total_amount=quote.total_amount,
        engagement_id=engagement.id, quote_id=quote.id, contract_id=None,
        counterparty=counterparty, end_user=end_user,
    )
    enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.review.submit.provisional",
        payload=payload, ref_type="review_case", ref_id=str(review_case.id), actor=actor,
    )
    return review_case


def submit_formal_review(
    session: Session, tenant_id: uuid.UUID, contract: Contract, engagement: Engagement, *,
    actor: str,
) -> ReviewCase | None:
    """契約発行フック(§5 正式審査)。仮審査とハッシュが同一かつ有効期限内なら
    `parent_case_no`に引き継ぐ(§5.5: AI_TM側が判定を引き継いで即座に完了)。
    """
    line_items = list_contract_line_items(session, tenant_id, contract.id)
    codes = _resolve_material_codes(session, tenant_id, line_items)
    if codes is None:
        create_manual_action_item(
            session, tenant_id, engagement.id, assigned_to=REVIEW_ACTION_ASSIGNEE,
            task=(
                f"契約 {contract.contract_number} に品目マッピング(ERP品目コード)未設定の"
                "商品が含まれるため、輸出審査を起票できませんでした。商品マスタでERP品目を"
                "紐付けてください。"
            ),
            assigned_by="system:review-case",
        )
        return None

    review_key_hash = build_review_key_hash(
        line_item_codes=codes, destination_country=contract.destination_country,
        end_user_account_id=contract.end_user_account_id, end_use=contract.end_use,
        total_amount=contract.total_amount, currency=contract.currency,
    )

    parent_case_no: str | None = None       # 自社(CRM)内部の記録用
    aitm_parent_case_no: str | None = None  # AI_TM側へ送るのはこちら(AI_TM採番のcase_no)
    if contract.quote_id is not None:
        candidate = session.execute(
            select(ReviewCase).where(
                ReviewCase.tenant_id == tenant_id,
                ReviewCase.quote_id == contract.quote_id,
                ReviewCase.review_type == ReviewType.PROVISIONAL,
            ).order_by(ReviewCase.created_at.desc())
        ).scalars().first()
        now = datetime.now(timezone.utc)
        if (
            candidate is not None
            and candidate.review_key_hash == review_key_hash
            and candidate.valid_until is not None
            and candidate.valid_until > now
        ):
            parent_case_no = candidate.case_no
            aitm_parent_case_no = candidate.provider_request_id

    case_no = f"CRM-{contract.contract_number}"
    review_case = ReviewCase(
        tenant_id=tenant_id, case_no=case_no, parent_case_no=parent_case_no,
        review_type=ReviewType.FORMAL, artifact_type=ArtifactType.CONTRACT,
        contract_id=contract.id, engagement_id=engagement.id,
        review_key_hash=review_key_hash, status=ReviewCaseStatus.PENDING, written_by=actor,
    )
    session.add(review_case)
    session.flush()

    counterparty = session.get(Account, engagement.account_id)
    end_user = (
        session.get(Account, contract.end_user_account_id)
        if contract.end_user_account_id else None
    )
    payload = _build_payload(
        title=f"{engagement.name} - {contract.contract_number}",
        parent_case_no=aitm_parent_case_no,
        line_item_codes=codes, destination_country=contract.destination_country,
        end_use=contract.end_use, total_amount=contract.total_amount,
        engagement_id=engagement.id, quote_id=contract.quote_id, contract_id=contract.id,
        counterparty=counterparty, end_user=end_user,
    )
    enqueue_outbox(
        session, tenant_id, target_system="aitm", kind="aitm.review.submit.formal",
        payload=payload, ref_type="review_case", ref_id=str(review_case.id), actor=actor,
    )
    return review_case


# 2026-08-16 E2E疎通確認で判明した実際のパス(ai_validationモジュール、
# CRM_連携_実装計画.md参照)。仮審査/正式審査でエンドポイントが分かれている。
_REVIEW_ENDPOINTS = {
    "aitm.review.submit.provisional": "/api/crm/provisional-review",
    "aitm.review.submit.formal": "/api/crm/formal-review",
}


def _dispatch_aitm_review_submit(session: Session, message: OutboxMessage) -> OutboxResult:
    """`register_aitm_dispatchers()`経由でOutbox処理から呼ばれる送信関数。

    実際のAI_TM(`ai_validation`モジュール)はcase_noを自ら採番して返す
    (CRM側が送るのではない) — レスポンスの`case_no`を`provider_request_id`
    に保存する。`ReviewCase.case_no`(CRM内部の識別子)はそのまま維持し、
    `parent_case_no`引き継ぎ等の内部ロジックはそちらを使い続ける。
    """
    review_case = session.execute(
        select(ReviewCase).where(
            ReviewCase.tenant_id == message.tenant_id,
            ReviewCase.id == uuid.UUID(message.ref_id),
        )
    ).scalar_one_or_none()
    if review_case is None:
        return OutboxResult.FAILED_NO_RETRY

    path = _REVIEW_ENDPOINTS.get(message.kind)
    if path is None:
        return OutboxResult.FAILED_NO_RETRY

    client = SignedClient(
        os.environ.get("AITM_REVIEW_URL"), message.tenant_id,
        bearer_env="AITM_REVIEW_BEARER", secret_env="AITM_REVIEW_SECRET",
    )
    try:
        response = client.post(path, message.payload, request_id=str(message.id))
    except httpx.HTTPError as exc:
        return classify_http_response(None, exc=exc)
    finally:
        client.close()

    result = classify_http_response(response)
    if result == OutboxResult.SENT and response.content:
        data = response.json()
        review_case.provider_request_id = data.get("case_no")
        valid_until_raw = data.get("valid_until")
        if valid_until_raw:
            review_case.valid_until = datetime.fromisoformat(valid_until_raw)
    return result


def register_aitm_dispatchers() -> None:
    """`AITM_REVIEW_URL`が設定されている場合のみdispatcherを登録する。

    未設定の開発環境では意図的に何もしない — 未登録kindはPhase 0の
    `process_outbox`が`failed`として扱い、`/ui/integration-status`で
    目視できる(コンプライアンスゲートを黙って偽クリアにしないため)。
    """
    if os.environ.get("AITM_REVIEW_URL"):
        for kind in _REVIEW_ENDPOINTS:
            register_dispatcher(kind, _dispatch_aitm_review_submit)


def check_review_clearance(
    session: Session, tenant_id: uuid.UUID, *,
    quote_id: uuid.UUID | None = None, contract_id: uuid.UUID | None = None,
) -> str | None:
    """見積送付・契約締結の前に呼ぶハード遮断チェック(Phase 1b)。

    クリア済みなら None、未クリアならブロック理由(表示用メッセージ)を返す。
    `ReviewCase`が存在しない場合(§5.4: 品目マッピング未設定で審査未起票)も
    「未クリア」として扱う — 無審査のまま送付・締結させない安全側の判断。
    `quote_id`/`contract_id`のどちらか一方を指定する。
    """
    stmt = select(ReviewCase).where(ReviewCase.tenant_id == tenant_id)
    if quote_id is not None:
        stmt = stmt.where(ReviewCase.quote_id == quote_id)
    else:
        stmt = stmt.where(ReviewCase.contract_id == contract_id)
    review_case = session.execute(
        stmt.order_by(ReviewCase.created_at.desc())
    ).scalars().first()

    if review_case is None:
        return (
            "輸出管理審査が未起票です"
            "(品目マッピング未設定の可能性があります。ActionItemを確認してください)"
        )
    if review_case.status != ReviewCaseStatus.CLEAR:
        return f"輸出管理審査が未クリアです(現在の状態: {review_case.status})"
    if review_case.valid_until is not None and review_case.valid_until <= datetime.now(timezone.utc):
        return "輸出管理審査の有効期限が切れています(再審査が必要です)"
    return None
