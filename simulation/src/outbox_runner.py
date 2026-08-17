"""シミュレータ自身のプロセス内でCRMのOutbox送信ディスパッチャを動かす。

`scripts/process_outbox.py`をサブプロセスで呼ぶ代わりに、
`crm_mvp.services.outbox.process_outbox()`を直接呼び出す(同じ関数)。
CRMサーバ本体(uvicorn)はWebhook受信の署名検証にしか環境変数を必要とせず、
Outboxの送信(Outbound)方向はこのシミュレータ自身のプロセス環境変数で
動かす — 実サーバを再起動せずに済む。

送信先3システムとも現状Bearer/署名検証を強制していないことを2026-08-16の
E2E確認で確認済みのため、値はダミーでよい(`SignedClient`は空文字列だと
`SignedClientConfigError`を送出するため、何か値を入れておく必要があるだけ)。
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

_DUMMY = "sim-dummy-outbound-credential"

REQUIRED_ENV = {
    "AITM_REVIEW_URL": "http://localhost:8011",
    "AITM_REVIEW_BEARER": _DUMMY, "AITM_REVIEW_SECRET": _DUMMY,
    # 2026-08-17: port 8888は修正コミットを反映していない古いプロセスと
    # 判明。9000が現行の稼働先(endpoints.yamlと同じ判断)。
    "ERP_BASE_URL": "http://localhost:9000",
    "ERP_COMMERCE_BEARER": _DUMMY, "ERP_COMMERCE_SECRET": _DUMMY,
    # 2026-08-17、ERP側にIF-25専用の`POST /crm/sales-orders`が新設され、
    # `/crm/commerce-check`と同じHMAC署名スキーム(未署名でも通る)に統一
    # された。旧`/sd/sales-orders`のOAuth2パスワードフロー対応は撤去済み。
    "ERP_SALES_ORDER_BEARER": _DUMMY, "ERP_SALES_ORDER_SECRET": _DUMMY,
    "AITM_LICENSE_URL": "http://localhost:8012",
    "AITM_LICENSE_BEARER": _DUMMY, "AITM_LICENSE_SECRET": _DUMMY,
    # AITM_DEEMED_EXPORT_URL: 実エンドポイント未確認(2026-08-16 E2E確認の
    # 「確認できなかった項目」)のため未設定のままにする — dispatcherが
    # 登録されず、該当Outboxメッセージはpendingのまま残る(fail loud)。
}

_initialized = False


def ensure_dispatchers_registered() -> None:
    """env設定 + register_*_dispatchers() をプロセス内で一度だけ行う。"""
    global _initialized
    if _initialized:
        return
    for k, v in REQUIRED_ENV.items():
        os.environ.setdefault(k, v)

    from crm_mvp.services.commerce_check import register_erp_dispatchers
    from crm_mvp.services.deemed_export import register_deemed_export_dispatchers
    from crm_mvp.services.erp_transcription import register_erp_transcription_dispatchers
    from crm_mvp.services.license import register_aitm_license_dispatchers
    from crm_mvp.services.review_case import register_aitm_dispatchers

    register_aitm_dispatchers()
    register_erp_dispatchers()
    register_erp_transcription_dispatchers()
    register_aitm_license_dispatchers()
    register_deemed_export_dispatchers()
    _initialized = True


def drain_outbox(session: Session, tenant_id: uuid.UUID, *, limit: int = 50) -> dict:
    """pending中のOutboxMessageを実際に送信しきるまで繰り返す
    (`process_outbox`は1バッチ限りなので、SENT/RETRYが尽きるまでループする)。"""
    from crm_mvp.services.outbox import process_outbox

    ensure_dispatchers_registered()
    session.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, false)"), {"tid": str(tenant_id)},
    )
    totals = {"sent": 0, "retried": 0, "dlq": 0, "failed_no_retry": 0}
    for _ in range(10):  # リトライバックオフ待ちはせず、その場で拾える分だけ即時再試行
        result = process_outbox(session, tenant_id, limit=limit)
        session.commit()
        for k in totals:
            totals[k] += result.get(k, 0)
        if result.get("sent", 0) == 0 and result.get("retried", 0) == 0:
            break
    return totals
