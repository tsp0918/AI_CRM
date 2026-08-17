"""AI_TM取引審査(仮審査/正式審査)の判定完了待ちとCRMへのwebhook橋渡し。

2026-08-16のP3疎通確認時点では、`/api/crm/provisional-review`等は審査
ケースを起票するだけでAI判定は自動実行されず、①スクリーニング実行
②該非二法令リスト照合、の2段階を明示的に呼ぶ必要があった。
**2026-08-17、AI_TM側の対応完了後の確認dry-runで、この2段階は不要になり
判定が数秒で自動完了することを確認済み**(`tx_id`もレスポンスに追加され、
`/api/transactions/recent`を線形探索する必要も無くなった)。そのためポーリング
のみで完了を待つ。

一方、AI_TM→CRMへの判定完了webhook(callback)は同dry-runで**依然未接続**
であることを確認済み — CRM側は今も判定完了を能動的に知る手段が無いため、
シミュレータが判定結果の取得とCRMへのwebhook橋渡しを引き続き担う。
"""

from __future__ import annotations

import time

from crm_mvp.enums import ReviewCaseStatus

from ..clients.aitm import AitmClient
from ..clients.crm import CrmWebhookClient

# `/api/crm/*-review`が返す`status`(draft/approved/rejected/...)をCRMの
# ReviewCaseStatusへ写像する。未知の値・タイムアウトは安全側でNEEDS_REVIEWに
# する(§6.2「UNKNOWNがCLEARに化けてはいけない」原則、A-07相当のフェイルクローズ)。
_AITM_STATUS_TO_REVIEW_STATUS = {
    "approved": ReviewCaseStatus.CLEAR, "clear": ReviewCaseStatus.CLEAR,
    "rejected": ReviewCaseStatus.BLOCKED, "denied": ReviewCaseStatus.BLOCKED,
    "needs_review": ReviewCaseStatus.NEEDS_REVIEW,
}
_POLL_INTERVAL_SEC = 3
_POLL_TIMEOUT_SEC = 45


def drive_and_push_review_result(
    aitm: AitmClient, crm_wh: CrmWebhookClient, case_no: str, aitm_case_no: str, *, revision: int = 1,
) -> dict:
    deadline = time.time() + _POLL_TIMEOUT_SEC
    data: dict = {}
    while time.time() < deadline:
        data = aitm.get_review_status(aitm_case_no) or {}
        if str(data.get("status") or "").lower() not in ("", "draft", "pending", "in_review"):
            break
        time.sleep(_POLL_INTERVAL_SEC)

    final_status = str(data.get("status") or "").lower()
    mapped = _AITM_STATUS_TO_REVIEW_STATUS.get(final_status, ReviewCaseStatus.NEEDS_REVIEW)
    crm_wh.send_review_result(
        case_no=case_no, revision=revision, status=mapped.value,
        valid_until=data.get("valid_until"), detail={"aitm_raw_status": final_status},
    )
    return data
