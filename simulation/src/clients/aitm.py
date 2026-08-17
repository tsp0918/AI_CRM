"""AI_TM操作クライアント(docs/BULK_SIMULATION_SPEC.md §3.1)。§4のマスタ投入
(取引先スクリーニング登録・ウォッチリスト・許可証枠)と、P3以降のoverride・
監視トリガで使う。"""

from __future__ import annotations
from urllib.parse import quote

import httpx

from ..config import Endpoints
from .base import SignedSimClient


class AitmClient:
    def __init__(self, cfg: Endpoints):
        self._org_id = cfg.aitm["org_id"]
        self._bearer = cfg.aitm["bearer"]
        self._secret = cfg.aitm["signing_secret"]
        self._screening = SignedSimClient(
            cfg.aitm["screening"], tenant_id=self._org_id, bearer=self._bearer, signing_secret=self._secret,
        )
        self._license = SignedSimClient(
            cfg.aitm["license"], tenant_id=self._org_id, bearer=self._bearer, signing_secret=self._secret,
        )
        self._validation = SignedSimClient(
            cfg.aitm["validation"], tenant_id=self._org_id, bearer=self._bearer, signing_secret=self._secret,
        )

    def create_counterparty(self, payload: dict) -> httpx.Response:
        return self._screening.post("/api/counterparties", payload)

    def find_counterparty_by_name(self, name: str) -> dict | None:
        """`q`は部分一致検索のため、完全一致を取り出してから照合する
        (再実行時の冪等化用)。"""
        resp = self._screening.get(f"/api/counterparties?q={quote(name)}&limit=200")
        if resp.status_code != 200:
            return None
        items = resp.json() if isinstance(resp.json(), list) else resp.json().get("items", [])
        for item in items:
            if item.get("name") == name:
                return item
        return None

    def add_watchlist_entry(self, payload: dict) -> httpx.Response:
        return self._screening.post("/api/watchlist", payload)

    def list_watchlist_entries(self) -> list[dict]:
        resp = self._screening.get("/api/watchlist?limit=500")
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else data.get("items", [])

    def rebuild_watchlist_index(self) -> httpx.Response:
        """`/api/watchlist`へのPOST直後は検索インデックスに反映されない
        (2026-08-16 P3疎通確認で判明 — 再構築するまで`/api/screen`が
        新規追加分をヒットさせない)。ウォッチリスト投入の最後に必ず呼ぶ。"""
        return self._screening.post("/api/rebuild-index", {})

    def screen_counterparty(self, name: str, *, country: str | None = None) -> dict | None:
        resp = self._screening.post("/api/screen", {"company_name": name, "country": country})
        if resp.status_code != 200:
            return None
        return resp.json()

    def register_license_quota(self, payload: dict) -> httpx.Response:
        return self._license.post("/api/licenses/quotas/register", payload)

    def get_review_status(self, case_no: str) -> dict | None:
        resp = self._validation.get(f"/api/crm/review-status/{quote(case_no)}")
        if resp.status_code != 200:
            return None
        return resp.json()

    def find_transaction_id_by_case_no(self, case_no: str) -> int | None:
        """`/api/crm/*-review`はAI_TM内部の`transaction.id`を返さないため、
        `/api/transactions/recent`から`case_no`で逆引きする(直近分のみ、
        limit次第で見つからないことがある点に注意)。"""
        resp = self._validation.get("/api/transactions/recent")
        if resp.status_code != 200:
            return None
        for t in resp.json().get("transactions", []):
            if t.get("case_no") == case_no:
                return t["id"]
        return None

    def run_screening(self, transaction_id: int) -> httpx.Response:
        return self._validation.post(f"/ui/transactions/{transaction_id}/run-screening", {})

    def run_two_lists_decision(self, transaction_id: int) -> httpx.Response:
        """該非判定(HS/ECCNの二法令リスト照合)を実行し、tier(自動承認/要人手)を
        確定させる。`/api/crm/*-review`の時点ではこれが未実行のため、
        `status`は`draft`のまま止まる(2026-08-16 P3疎通確認で判明)。"""
        return self._validation.post(f"/decision/{transaction_id}/run-and-two-lists", {})

    def close(self) -> None:
        self._screening.close()
        self._license.close()
        self._validation.close()
