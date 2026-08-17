"""ERP操作クライアント(docs/BULK_SIMULATION_SPEC.md §3.1)。§4のマスタ投入
(取引先・品目)と、P3以降の出荷・請求・返品操作で使う。

`/mdm/*`はOAuth2パスワードフロー(`POST /auth/token`)で認証する通常の
業務APIで、endpoints.yamlの静的な`bearer`(HMAC署名スキーム用)とは別物。
erp-system自身のREADMEに開発用として明記されているデフォルト認証情報
(admin@example.com)をそのまま使う(実運用の秘密情報ではない)。

発行されたJWTは約1時間で失効することを2026-08-16のP4実行準備で確認した
(60件規模の実行は1時間を超えうる)ため、一定時間ごとに自動再取得する。
"""

from __future__ import annotations

import os
import time

import httpx

from ..config import Endpoints
from .base import SignedSimClient

_DEFAULT_USERNAME = "admin@example.com"
_DEFAULT_PASSWORD = "admin1234"
_TOKEN_REFRESH_INTERVAL_SEC = 15 * 60  # 実測1時間弱の有効期限に対し安全側で15分ごとに更新


def _fetch_oauth_token(base_url: str) -> str:
    username = os.environ.get("ERP_USERNAME", _DEFAULT_USERNAME)
    password = os.environ.get("ERP_PASSWORD", _DEFAULT_PASSWORD)
    resp = httpx.post(
        f"{base_url.rstrip('/')}/auth/token",
        data={"username": username, "password": password},
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


class ErpClient:
    def __init__(self, cfg: Endpoints):
        self._base_url = cfg.erp["base_url"]
        self._tenant_id = cfg.erp["client_id"]
        self._signing_secret = cfg.erp["signing_secret"]
        self._token_fetched_at = 0.0
        self._c = self._make_client()

    def _make_client(self) -> SignedSimClient:
        oauth_token = _fetch_oauth_token(self._base_url)
        self._token_fetched_at = time.time()
        return SignedSimClient(
            self._base_url, tenant_id=self._tenant_id,
            bearer=oauth_token, signing_secret=self._signing_secret,
        )

    def _ensure_fresh_token(self) -> None:
        if time.time() - self._token_fetched_at > _TOKEN_REFRESH_INTERVAL_SEC:
            self._c.close()
            self._c = self._make_client()

    def create_business_partner(self, payload: dict) -> httpx.Response:
        self._ensure_fresh_token()
        return self._c.post("/mdm/business-partners", payload)

    def create_material(self, payload: dict) -> httpx.Response:
        self._ensure_fresh_token()
        return self._c.post("/mdm/materials", payload)

    def find_business_partner_by_code(self, bp_code: str) -> dict | None:
        """`GET /mdm/business-partners`は`bp_code`での絞り込みに対応していない
        ため、ページングしてクライアント側で照合する(再実行時の冪等化用)。"""
        self._ensure_fresh_token()
        resp = self._c.get("/mdm/business-partners?limit=500")
        if resp.status_code != 200:
            return None
        for item in resp.json().get("items", []):
            if item.get("bp_code") == bp_code:
                return item
        return None

    def find_material_by_code(self, material_code: str) -> dict | None:
        self._ensure_fresh_token()
        resp = self._c.get("/mdm/materials?limit=500")
        if resp.status_code != 200:
            return None
        for item in resp.json().get("items", []):
            if item.get("material_code") == material_code:
                return item
        return None

    def find_sales_order_by_document_number(self, document_number: str, *, customer_code: str | None = None) -> dict | None:
        """CRMは`Contract.external_id`に`document_number`(例: "0010000213")
        しか保持しない。出荷/請求を起こすには内部数値`id`が必要なため、
        ここで逆引きする(`customer_code`を渡すとページング対象を絞れる)。"""
        self._ensure_fresh_token()
        path = "/sd/sales-orders?limit=500"
        if customer_code:
            path += f"&customer_code={customer_code}"
        resp = self._c.get(path)
        if resp.status_code != 200:
            return None
        for item in resp.json().get("items", []):
            if item.get("document_number") == document_number:
                return item
        return None

    def create_delivery(self, payload: dict) -> httpx.Response:
        self._ensure_fresh_token()
        return self._c.post("/sd/deliveries", payload)

    def create_billing(self, payload: dict) -> httpx.Response:
        self._ensure_fresh_token()
        return self._c.post("/sd/billing", payload)

    def close(self) -> None:
        self._c.close()
