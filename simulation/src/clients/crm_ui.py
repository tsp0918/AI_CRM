"""CRMのWeb UI(Jinja2 SSR)を実際にPOSTで操作するクライアント。

このMVPには認証が無く、`crm_tenant_id`/`crm_actor_id`という無署名Cookieだけで
セッションが成立する(`crm_mvp/api/web/session.py`)。JSON API
(`crm_mvp/api/engagements.py`)とは別物で、Engagement/Quote/Contractの作成は
Web UIのフォームPOSTでしかできない(公開JSON APIにはPOSTが無い)。

成功レスポンスは常に303リダイレクト(`?flash=...&flash_type=...`)で、
新規作成した行のUUIDはリダイレクト先に含まれない(flashメッセージに人間可読の
採番(quote_number等)が入るのみ)。実UUIDの取得は`simulation/src/db.py`経由の
読み取り専用クエリで行う(Account/Product同様、書き込みはUI経由・生成物の
参照だけDB直読み、という本シミュレーションの一貫した方針)。
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx


class UiFlowError(RuntimeError):
    """flash_type=errorが返ってきた(業務ゲート等でブロックされた)場合。"""


_ENGAGEMENT_ID_RE = re.compile(r"/ui/engagements/([0-9a-f-]{36})")


class CrmUiClient:
    def __init__(self, base_url: str, *, tenant_id: uuid.UUID, actor_id: uuid.UUID):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            cookies={"crm_tenant_id": str(tenant_id), "crm_actor_id": str(actor_id)},
            timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True,
        )

    def _post(self, path: str, data: dict, *, allow_error: bool = False) -> httpx.Response:
        resp = self._client.post(f"{self.base_url}{path}", data=data)
        # flash_type=errorはアプリが正常に検知したビジネスゲート違反。
        # 500等は未処理の例外(サーバ側バグ)で、redirectすら起きず
        # flash_typeも付かないため、これも別に検出する(2026-08-16 P4実行で
        # 発覚 — VARCHAR(2)超過のDataErrorが黙って呑み込まれていた)。
        if resp.status_code >= 400 and not allow_error:
            raise UiFlowError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        qs = parse_qs(urlparse(str(resp.url)).query)
        if qs.get("flash_type", [""])[0] == "error" and not allow_error:
            raise UiFlowError(qs.get("flash", ["(詳細不明)"])[0])
        return resp

    def create_engagement_new_account(
        self, *, account_name: str, engagement_name: str, sales_group_id: str = "",
        allow_error: bool = False,
    ) -> uuid.UUID:
        """常に新規Accountを作る(`engagement_new_submit`の仕様)。リダイレクト
        パスに新規engagement UUIDが含まれる(quote/contractと異なりここだけ
        は取得できる)。"""
        resp = self._post(
            "/ui/engagements/new",
            {"account_name": account_name, "engagement_name": engagement_name, "sales_group_id": sales_group_id},
            allow_error=allow_error,
        )
        m = _ENGAGEMENT_ID_RE.search(str(resp.url))
        if not m:
            raise UiFlowError(f"engagement IDをリダイレクト先から取得できません: {resp.url}")
        return uuid.UUID(m.group(1))

    def add_line_item(self, engagement_id: uuid.UUID, *, product_id: uuid.UUID, quantity: float) -> httpx.Response:
        # `add_line_item_ui`はFormの`quantity`を`int(quantity)`でパースする
        # (整数のみ許容、2026-08-16 P4実行で判明)。
        return self._post(
            f"/ui/engagements/{engagement_id}/line-items",
            {"product_id": str(product_id), "quantity": str(int(quantity)), "discount_rate": "0"},
        )

    def create_quote(
        self, engagement_id: uuid.UUID, *, destination_country: str = "",
        end_user_account_id: str = "", end_use: str = "", valid_until: str = "",
        allow_error: bool = False,
    ) -> httpx.Response:
        return self._post(
            f"/ui/engagements/{engagement_id}/quotes",
            {
                "destination_country": destination_country, "end_user_account_id": end_user_account_id,
                "end_use": end_use, "valid_until": valid_until,
            },
            allow_error=allow_error,
        )

    def update_quote_status(
        self, engagement_id: uuid.UUID, quote_id: uuid.UUID, *, status: str, allow_error: bool = False,
    ) -> httpx.Response:
        return self._post(
            f"/ui/engagements/{engagement_id}/quotes/{quote_id}/status",
            {"status": status}, allow_error=allow_error,
        )

    def create_contract(
        self, engagement_id: uuid.UUID, *, quote_id: str = "", start_date: str = "",
        end_date: str = "", destination_country: str = "", end_user_account_id: str = "",
        end_use: str = "", allow_error: bool = False,
    ) -> httpx.Response:
        return self._post(
            f"/ui/engagements/{engagement_id}/contracts",
            {
                "quote_id": quote_id, "start_date": start_date, "end_date": end_date,
                "destination_country": destination_country, "end_user_account_id": end_user_account_id,
                "end_use": end_use,
            },
            allow_error=allow_error,
        )

    def update_contract_status(
        self, engagement_id: uuid.UUID, contract_id: uuid.UUID, *, status: str, allow_error: bool = False,
    ) -> httpx.Response:
        return self._post(
            f"/ui/engagements/{engagement_id}/contracts/{contract_id}/status",
            {"status": status}, allow_error=allow_error,
        )

    def close(self) -> None:
        self._client.close()
