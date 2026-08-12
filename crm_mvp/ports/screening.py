"""外部スクリーニング(反社・与信・輸出管理)ポート。

AI_TM の実 API 契約は CRM_INTEGRATION_HANDOVER.md §6 参照。
ここでは Protocol と、テスト・ローカル開発用の Mock 実装を提供する。
AITMScreeningAdapter は実 API への骨格実装であり、環境変数が無ければ
明示的に RuntimeError を送出する（未接続を暗黙に CLEAR 扱いにしない）。

§3.7: 証跡は参照のみ保持する。記事本文・レポート本体は一切コピーしない。
ScreeningResult.detail に外部 API のレスポンスをそのまま入れているのは
一時的な受け渡し用であり、DB へ永続化する際は evidence_uri / evidence_hash
だけを ComplianceStatus に落とすこと（本文を JSONB に丸ごと保存しない）。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..enums import ComplianceCheckType, ComplianceOutcome


@dataclass(slots=True)
class ScreeningResult:
    outcome: ComplianceOutcome
    provider: str
    provider_request_id: str | None = None
    detail: dict = field(default_factory=dict)


class ScreeningPort(Protocol):
    def screen(
        self, account_name: str, check_type: ComplianceCheckType
    ) -> ScreeningResult: ...


class MockScreeningAdapter:
    """常に CLEAR を返す。ローカル開発・テスト専用。"""

    def screen(
        self, account_name: str, check_type: ComplianceCheckType
    ) -> ScreeningResult:
        return ScreeningResult(
            outcome=ComplianceOutcome.CLEAR,
            provider="mock",
            provider_request_id=f"MOCK-{uuid.uuid4().hex[:8]}",
            detail={"account_name": account_name, "check_type": check_type.value},
        )


class AITMScreeningAdapter:
    """AI_TM screening API (CRM_INTEGRATION_HANDOVER.md §6) への実アダプタ。

    AITM_SCREENING_URL が未設定なら RuntimeError を送出する。
    """

    _OUTCOME_MAP = {
        "match": ComplianceOutcome.HIT,
        "possible_match": ComplianceOutcome.NEEDS_REVIEW,
        "clear": ComplianceOutcome.CLEAR,
    }

    def __init__(
        self, base_url: str | None = None, bearer: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url or os.environ.get("AITM_SCREENING_URL")
        self.bearer = bearer or os.environ.get("AITM_BEARER")
        self.timeout = timeout
        if not self.base_url:
            raise RuntimeError(
                "AITM_SCREENING_URL is not configured; AITMScreeningAdapter "
                "cannot connect. Use MockScreeningAdapter for local development."
            )

    def screen(
        self, account_name: str, check_type: ComplianceCheckType
    ) -> ScreeningResult:
        headers = {"Authorization": f"Bearer {self.bearer}"} if self.bearer else {}
        response = httpx.post(
            f"{self.base_url}/api/screen",
            json={"company_name": account_name, "threshold": 0.75},
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        matches = data.get("matches") or []
        outcome = self._OUTCOME_MAP.get(
            data.get("result_status", "clear"), ComplianceOutcome.UNKNOWN
        )
        return ScreeningResult(
            outcome=outcome,
            provider="ai_tm",
            provider_request_id=matches[0].get("source_id") if matches else None,
            detail=data,
        )
