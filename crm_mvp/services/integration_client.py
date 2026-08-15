"""外部システム(ERP/AI_TM)への署名付きHTTPクライアント(2026-08-15)。

CRM_連携引き継ぎ書.md §4.1(Bearer + HMAC-SHA256の二重認証)・§6.1を実装する。
`ports/screening.py`の`AITMScreeningAdapter`と同じ思想 — 接続先が未設定なら
黙って進めず、明示的に`RuntimeError`を送出する。

署名対象文字列は "{timestamp}.{raw_body}" で、raw_body は送信するHTTP
ボディの生バイト列そのもの。httpx の `json=` 引数は内部で再シリアライズ
されるため使わない(署名対象と送信ボディが食い違う典型的な不具合を
構造的に防ぐため、`SignedClient`はシリアライズを自分で1回だけ行う)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid

import httpx


class SignedClientConfigError(RuntimeError):
    """接続先URL・Bearerトークン・署名シークレットのいずれかが未設定。"""


class SignedClient:
    """`target_system`(aitm/erp)向けの署名付きPOSTクライアント。

    `bearer_env`/`secret_env` は環境変数名そのもの(値ではない)を渡す —
    どの接続先の設定が欠けているかをエラーメッセージで特定しやすくする
    ため。`client` を注入すると `httpx.MockTransport` 等でテスト可能。
    """

    def __init__(
        self, base_url: str | None, tenant_id: uuid.UUID, *,
        bearer_env: str, secret_env: str, client: httpx.Client | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        if not base_url:
            raise SignedClientConfigError(
                f"接続先URLが未設定です。呼び出し元で base_url を指定してください"
                f"(環境変数の例: {bearer_env.replace('_BEARER', '_URL')})"
            )
        bearer = os.environ.get(bearer_env)
        if not bearer:
            raise SignedClientConfigError(f"{bearer_env} が未設定です")
        secret = os.environ.get(secret_env)
        if not secret:
            raise SignedClientConfigError(f"{secret_env} が未設定です")

        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self._bearer = bearer
        self._secret = secret.encode("utf-8")
        self._client = client or httpx.Client()
        self._timeout = timeout or httpx.Timeout(30.0, connect=5.0)

    def post(
        self, path: str, payload: dict, *, request_id: str | None = None,
    ) -> httpx.Response:
        # ★ シリアライズは一度だけ。署名対象と送信ボディは同一バイト列。
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        ts = str(int(time.time()))
        signature = hmac.new(
            self._secret, f"{ts}.".encode("utf-8") + body, hashlib.sha256,
        ).hexdigest()
        headers = {
            "Authorization": f"Bearer {self._bearer}",
            "X-Signature": f"sha256={signature}",
            "X-Timestamp": ts,
            "X-Request-Id": request_id or str(uuid.uuid4()),
            "X-Tenant-Id": str(self.tenant_id),
            "Content-Type": "application/json; charset=utf-8",
        }
        return self._client.post(
            f"{self.base_url}{path}", content=body, headers=headers,
            timeout=self._timeout,
        )

    def close(self) -> None:
        self._client.close()
