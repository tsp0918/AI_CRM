"""Webhook受信の署名検証・冪等性記録(2026-08-15)。

CRM_連携引き継ぎ書.md §4.1(Bearer+HMAC-SHA256署名検証)・§4.3(冪等性)・
§7.1を実装する。**既存の crm_mvp/api/webhooks.py の2エンドポイント
(compliance-judgment / sanctions-list-updated)へは、このフェーズでは
接続しない** — それらはPhase 1でIF-10/IF-12の本実装(revisionベースの
順序解決を含む)と合わせて組み込む。ここでは検証ロジック自体を単体で
完成させ、テストで担保する(CRM_連携_実装計画.md Phase 0スコープ判断)。

エラーレスポンスの外側の`{"error": {...}}`エンベロープ(§4.2)は、実際に
FastAPIルートへ接続する際に例外ハンドラで組み立てる想定 — 本モジュール
単体では`HTTPException(status_code, detail={...})`で必要な情報を返す
だけに留める(過剰な先取り実装を避ける)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import WebhookEventResult
from ..models import WebhookEvent

CLOCK_SKEW_TOLERANCE_SEC = 300


@dataclass(slots=True)
class WebhookContext:
    tenant_id: uuid.UUID
    source: str
    raw: bytes
    payload: dict


def _err(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _active_secrets(secret_env: str) -> list[bytes]:
    """現行鍵 + 旧鍵(ローテーション用)。両方設定されていれば両方受理する
    (引き継ぎ書§4.1: 「無停止ローテーション。切替期間は最低7日」)。"""
    secrets: list[bytes] = []
    current = os.environ.get(secret_env)
    if current:
        secrets.append(current.encode("utf-8"))
    previous = os.environ.get(f"{secret_env}_PREVIOUS")
    if previous:
        secrets.append(previous.encode("utf-8"))
    return secrets


async def verify_webhook(
    request: Request, *, source: str, secret_env: str, bearer_env: str,
) -> WebhookContext:
    """署名・トークン・タイムスタンプを検証し、生ボディをパースして返す。

    生ボディ(`raw`)は署名検証にそのまま使う — パース後のdictを再
    シリアライズして検証しないこと(送信側と同じく、検証対象と受信
    ボディを同一バイト列にするため)。
    """
    raw = await request.body()
    ts = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")

    if not ts.isdigit() or abs(time.time() - int(ts)) > CLOCK_SKEW_TOLERANCE_SEC:
        raise HTTPException(
            401, _err("TIMESTAMP_EXPIRED", "タイムスタンプの有効期限が切れています")
        )

    secrets = _active_secrets(secret_env)
    if not secrets:
        raise HTTPException(500, _err("CONFIG_ERROR", f"{secret_env} が未設定です"))

    prefix = "sha256="
    if not sig.startswith(prefix):
        raise HTTPException(401, _err("INVALID_SIGNATURE", "署名の形式が不正です"))
    provided = sig[len(prefix):]

    verified = any(
        hmac.compare_digest(
            hmac.new(secret, f"{ts}.".encode("utf-8") + raw, hashlib.sha256).hexdigest(),
            provided,
        )
        for secret in secrets
    )
    if not verified:
        raise HTTPException(401, _err("INVALID_SIGNATURE", "署名の検証に失敗しました"))

    bearer = os.environ.get(bearer_env)
    if not bearer:
        raise HTTPException(500, _err("CONFIG_ERROR", f"{bearer_env} が未設定です"))
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, bearer):
        raise HTTPException(401, _err("INVALID_TOKEN", "トークンが不正です"))

    tenant_raw = request.headers.get("X-Tenant-Id", "")
    try:
        tenant_id = uuid.UUID(tenant_raw)
    except ValueError:
        raise HTTPException(403, _err("TENANT_FORBIDDEN", "X-Tenant-Id が不正です"))

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, _err("VALIDATION_ERROR", "JSONの解析に失敗しました"))

    return WebhookContext(tenant_id=tenant_id, source=source, raw=raw, payload=payload)


def record_webhook_event(
    session: Session, tenant_id: uuid.UUID, *, event_id: str, source_system: str,
    event_type: str, payload: dict, actor: str = "system:webhook",
) -> tuple[WebhookEvent, bool]:
    """`(event, is_new)`を返す。既に同一`event_id`が記録済みなら`is_new=False`
    でそのまま返す — 呼び出し側はこれを「200 duplicate、何もしない」の
    判定に使う(引き継ぎ書§4.3: at-least-once配信の重複を正常系として扱う)。
    """
    existing = session.execute(
        select(WebhookEvent).where(
            WebhookEvent.tenant_id == tenant_id, WebhookEvent.event_id == event_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    event = WebhookEvent(
        tenant_id=tenant_id, event_id=event_id, source_system=source_system,
        event_type=event_type, payload=payload, result=WebhookEventResult.PROCESSED,
        written_by=actor,
    )
    session.add(event)
    session.flush()
    return event, True
