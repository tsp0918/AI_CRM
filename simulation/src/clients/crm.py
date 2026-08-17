"""CRM操作クライアント(docs/BULK_SIMULATION_SPEC.md §3.1)。

CRMのAccount/Product等のマスタCRUDは公開JSON APIのスコープ外
(`crm_mvp/api/accounts.py`のdocstring参照)なので、それらは
`simulation/src/db.py`経由でDBに直接投入する(`scripts/seed_demo_*.py`と
同じ確立済みパターン)。このモジュールが担うのは、シミュレータが
AI_TM/ERPになりすましてCRMのWebhook受信エンドポイントへ送る、
署名付きPOSTのみ(§4でのparty-event、P3以降のreview-result等)。
"""

from __future__ import annotations

import uuid

import httpx

from ..config import Endpoints
from ..webhook_secrets import WEBHOOK_SECRETS
from .base import SignedSimClient

_ERP_WEBHOOK_PATHS = {
    "material_updated": "/webhooks/erp/material-updated",
    "business_partner_updated": "/webhooks/erp/business-partner-updated",
    "delivery_posted": "/webhooks/erp/delivery-posted",
    "billing_posted": "/webhooks/erp/billing-posted",
    "return_posted": "/webhooks/erp/return-posted",
}


class CrmWebhookClient:
    """`webhook_secrets.WEBHOOK_SECRETS`のキーごとに署名鍵が異なるため、
    呼び出しのたびに送信先チャネルを指定する。"""

    def __init__(self, cfg: Endpoints):
        self._base_url = cfg.crm["base_url"]
        self._tenant_id = cfg.crm["tenant_id"]
        self._clients: dict[str, SignedSimClient] = {}

    def _client_for(self, channel: str) -> SignedSimClient:
        if channel not in self._clients:
            secret_cfg = WEBHOOK_SECRETS[channel]
            self._clients[channel] = SignedSimClient(
                self._base_url, tenant_id=self._tenant_id,
                bearer=secret_cfg["bearer"], signing_secret=secret_cfg["secret"],
            )
        return self._clients[channel]

    def send_party_linked(
        self, *, crm_account_id: str, aitm_party_id: str, erp_bp_code: str | None = None,
    ) -> httpx.Response:
        payload = {
            "event_id": str(uuid.uuid4()), "event_type": "party.linked",
            "crm_account_id": crm_account_id, "aitm_party_id": aitm_party_id,
        }
        if erp_bp_code:
            payload["erp_bp_code"] = erp_bp_code
        return self._client_for("aitm_party").post(WEBHOOK_SECRETS["aitm_party"]["path"], payload)

    def send_review_result(
        self, *, case_no: str, revision: int, status: str,
        valid_until: str | None = None, detail: dict | None = None,
    ) -> httpx.Response:
        payload = {
            "case_no": case_no, "revision": revision, "status": status,
            "valid_until": valid_until, "detail": detail or {},
        }
        return self._client_for("aitm_review").post(WEBHOOK_SECRETS["aitm_review"]["path"], payload)

    def send_erp_material_updated(self, payload: dict) -> httpx.Response:
        return self._client_for("erp").post(_ERP_WEBHOOK_PATHS["material_updated"], payload)

    def send_erp_business_partner_updated(self, payload: dict) -> httpx.Response:
        return self._client_for("erp").post(_ERP_WEBHOOK_PATHS["business_partner_updated"], payload)

    def send_erp_delivery_posted(self, payload: dict) -> httpx.Response:
        return self._client_for("erp").post(_ERP_WEBHOOK_PATHS["delivery_posted"], payload)

    def send_erp_billing_posted(self, payload: dict) -> httpx.Response:
        return self._client_for("erp").post(_ERP_WEBHOOK_PATHS["billing_posted"], payload)

    def send_erp_return_posted(self, payload: dict) -> httpx.Response:
        return self._client_for("erp").post(_ERP_WEBHOOK_PATHS["return_posted"], payload)

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
