"""外部システム(ERP/AI_TM)への送信保証キュー(2026-08-15)。

CRM_連携引き継ぎ書.md §3.3-2「見積・契約の成立を外部システムの可用性に
依存させない」・§4.4(リトライ規約)・§6.2(Outbox)を実装する。

業務処理側(Phase 1以降の見積・契約サービス)は `enqueue_outbox()` を
呼ぶだけで完結させ、実際のHTTP送信は `process_outbox()` が別プロセス
(scripts/process_outbox.py)から定期的に処理する。どの `kind` にどの
送信関数を対応させるかは `register_dispatcher()` で後から登録する —
本モジュール自体は特定の外部システムのペイロード形状を一切知らない。

冪等性キー(`case_no` 等)の重複は送信側APIが 409 を返す形で表現される
想定のため、409 は失敗ではなく `OutboxResult.SENT` として扱う
(§4.3「409は正常系。既存のtransaction_idを採用」)。
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import OutboxResult, OutboxStatus
from ..models import OutboxMessage

# 引き継ぎ書§4.4: 初回 + 5回、指数バックオフ(10秒→60秒→5分→30分→2時間)。
RETRY_SCHEDULE_SEC: list[int] = [10, 60, 300, 1800, 7200]
MAX_ATTEMPTS = len(RETRY_SCHEDULE_SEC) + 1
_JITTER_RATIO = 0.20

DispatchFn = Callable[[Session, OutboxMessage], OutboxResult]

_DISPATCHERS: dict[str, DispatchFn] = {}


def register_dispatcher(kind: str, fn: DispatchFn) -> None:
    """`OutboxMessage.kind` ごとの実送信関数を登録する。

    Phase 1以降、`register_dispatcher("aitm.transaction.create", ...)` の
    ように実装が積み上がっていく前提。未登録の`kind`は`process_outbox`が
    即座に`FAILED_NO_RETRY`として扱う(無限リトライしない)。
    """
    _DISPATCHERS[kind] = fn


def classify_http_response(
    response: httpx.Response | None, exc: Exception | None = None,
) -> OutboxResult:
    """引き継ぎ書§4.4のリトライ可否表をコード化する。

    dispatcher実装(Phase 1以降)が`SignedClient.post()`の結果をこの関数に
    通すだけで、リトライ方針の判断ロジックを重複させずに済む。
    """
    if exc is not None:
        return OutboxResult.RETRY  # タイムアウト・接続エラー

    assert response is not None
    status = response.status_code
    if status == 409:
        return OutboxResult.SENT  # 冪等性キーの重複 = 正常系(既存リソースを採用)
    if status == 429 or status >= 500:
        return OutboxResult.RETRY
    if status in (200, 201):
        return OutboxResult.SENT
    return OutboxResult.FAILED_NO_RETRY  # 400/401/403/404/422等


def enqueue_outbox(
    session: Session, tenant_id: uuid.UUID, *, target_system: str, kind: str,
    payload: dict, ref_type: str | None = None, ref_id: str | None = None,
    actor: str = "system:outbox",
) -> OutboxMessage:
    message = OutboxMessage(
        tenant_id=tenant_id, target_system=target_system, kind=kind,
        payload=payload, status=OutboxStatus.PENDING,
        ref_type=ref_type, ref_id=ref_id, written_by=actor,
    )
    session.add(message)
    session.flush()
    return message


def _with_jitter(seconds: int) -> float:
    delta = seconds * _JITTER_RATIO
    return seconds + random.uniform(-delta, delta)


def _next_attempt_delay_sec(attempt_count: int) -> float:
    """`attempt_count`(このメッセージの試行回数、1回目の失敗後は1)に対応する
    バックオフ秒数。スケジュール範囲を超えたら最後の間隔を使う。"""
    idx = min(attempt_count - 1, len(RETRY_SCHEDULE_SEC) - 1)
    return _with_jitter(RETRY_SCHEDULE_SEC[idx])


def process_outbox(session: Session, tenant_id: uuid.UUID, *, limit: int = 50) -> dict:
    """保留中(`PENDING`かつ`next_attempt_at`が過去またはNULL)のメッセージを
    処理する。1テナント分のみを対象とする(RLSでも強制されるが、複数
    テナントを1プロセスで回すことは想定していない — Context参照)。

    行ロック(`FOR UPDATE SKIP LOCKED`)は本コードベース初導入のパターン。
    同一メッセージを複数ワーカーが同時に処理しないためのもの。
    """
    now = datetime.now(timezone.utc)
    messages = session.execute(
        select(OutboxMessage).where(
            OutboxMessage.tenant_id == tenant_id,
            OutboxMessage.status == OutboxStatus.PENDING,
            (OutboxMessage.next_attempt_at.is_(None))
            | (OutboxMessage.next_attempt_at <= now),
        ).order_by(OutboxMessage.created_at).limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    summary = {"sent": 0, "retried": 0, "dlq": 0, "failed_no_retry": 0}

    for message in messages:
        dispatcher = _DISPATCHERS.get(message.kind)
        if dispatcher is None:
            message.status = OutboxStatus.FAILED
            message.last_error = f"未登録のkindです: {message.kind}"
            summary["failed_no_retry"] += 1
            session.flush()
            continue

        try:
            result = dispatcher(session, message)
        except Exception as exc:  # noqa: BLE001 — dispatcher実装側の想定外例外もリトライ対象にする
            result = OutboxResult.RETRY
            message.last_error = str(exc)

        message.attempt_count += 1

        if result == OutboxResult.SENT:
            message.status = OutboxStatus.SENT
            message.sent_at = now
            summary["sent"] += 1
        elif result == OutboxResult.FAILED_NO_RETRY:
            message.status = OutboxStatus.FAILED
            summary["failed_no_retry"] += 1
        elif message.attempt_count >= MAX_ATTEMPTS:
            message.status = OutboxStatus.DLQ
            summary["dlq"] += 1
        else:
            message.next_attempt_at = now + timedelta(
                seconds=_next_attempt_delay_sec(message.attempt_count)
            )
            summary["retried"] += 1

        session.flush()

    return summary


def requeue_outbox_message(message: OutboxMessage) -> None:
    """`failed`/`dlq`状態のメッセージを手動で再送待ちに戻す(連携ステータス
    画面の「再送する」ボタンから呼ぶ)。試行回数はリセットしない —
    バックオフの土台となる履歴として残す。"""
    message.status = OutboxStatus.PENDING
    message.next_attempt_at = None
    message.last_error = None
