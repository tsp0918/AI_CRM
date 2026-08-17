"""3システム共通の署名付きHTTPクライアント(docs/BULK_SIMULATION_SPEC.md §2.2)。

`crm_mvp/services/integration_client.py` の `SignedClient` と同じ署名方式
(署名対象 = "{timestamp}.{raw_body}", HMAC-SHA256)を流用する。ただし
2026-08-16 のE2E疎通確認で3システムとも現状Bearer/署名検証を強制していない
ことを確認済みのため、`bearer`/`signing_secret` が空でもエラーにはせず、
その場合は `Authorization`/`X-Signature` ヘッダーを付けずに送信する
(相手側が将来検証を有効化した場合は endpoints.yaml に値を設定すれば
そのまま効くようにしておく)。`X-Timestamp`/`X-Request-Id`/`X-Tenant-Id` は
ERPがタイムスタンプの存在を必須にしていることが分かっているため常に付与する。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx


class SignedSimClient:
    def __init__(
        self, base_url: str, *, tenant_id: str, bearer: str = "",
        signing_secret: str = "", client: httpx.Client | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self._bearer = bearer
        self._secret = signing_secret.encode("utf-8") if signing_secret else b""
        self._client = client or httpx.Client()
        self._timeout = timeout or httpx.Timeout(30.0, connect=5.0)

    def _headers(self, ts: str, body: bytes, request_id: str | None) -> dict:
        headers = {
            "X-Timestamp": ts,
            "X-Request-Id": request_id or str(uuid.uuid4()),
            "X-Tenant-Id": self.tenant_id,
            "Content-Type": "application/json; charset=utf-8",
        }
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if self._secret:
            signature = hmac.new(
                self._secret, f"{ts}.".encode("utf-8") + body, hashlib.sha256,
            ).hexdigest()
            headers["X-Signature"] = f"sha256={signature}"
        return headers

    def post(self, path: str, payload: dict, *, request_id: str | None = None) -> httpx.Response:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        return self._client.post(
            f"{self.base_url}{path}", content=body,
            headers=self._headers(ts, body, request_id), timeout=self._timeout,
        )

    def get(self, path: str, *, request_id: str | None = None) -> httpx.Response:
        ts = str(int(time.time()))
        return self._client.get(
            f"{self.base_url}{path}",
            headers=self._headers(ts, b"", request_id), timeout=self._timeout,
        )

    def delete(self, path: str, *, request_id: str | None = None) -> httpx.Response:
        ts = str(int(time.time()))
        return self._client.delete(
            f"{self.base_url}{path}",
            headers=self._headers(ts, b"", request_id), timeout=self._timeout,
        )

    def close(self) -> None:
        self._client.close()
