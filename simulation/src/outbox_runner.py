"""シミュレータ自身のプロセス内でCRMのOutbox送信ディスパッチャを動かす。

`scripts/process_outbox.py`をサブプロセスで呼ぶ代わりに、
`crm_mvp.services.outbox.process_outbox()`を直接呼び出す(同じ関数)。
CRMサーバ本体(uvicorn)はWebhook受信の署名検証にしか環境変数を必要とせず、
Outboxの送信(Outbound)方向はこのシミュレータ自身のプロセス環境変数で
動かす — 実サーバを再起動せずに済む。

送信先3システムとも現状Bearer/署名検証を強制していないことを2026-08-16の
E2E確認で確認済みのため、値はダミーでよい(`SignedClient`は空文字列だと
`SignedClientConfigError`を送出するため、何か値を入れておく必要があるだけ)。

2026-10-27追記: 上記は一部古い。AI_TMの`ai_validation`モジュールは
`CRM_SIGNING_SECRET`でHMAC署名を検証している(未設定時の既定値は
`dev-crm-signing-secret-000`)。CRM側がダミー値のまま署名すると
`401 Invalid HMAC signature`になる(第3弾クロスリポジトリレビューで
判明)。Bearerの方は`/api/crm/*`・`/api/deemed-export/events`・
`/api/parties/resolve`のいずれも未検証のため、ダミーのままで良い
(AI_TM側に`webhook_endpoint.bearer`列が入るのはインバウンド3チャネル
向けの別の話 — `webhook_secrets.py`参照)。
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

_DUMMY = "sim-dummy-outbound-credential"
# AI_TMの`ai_validation`モジュール(provisional/formal-review、
# deemed-export)がHMAC検証に使う共有鍵。AI_TM側`CRM_SIGNING_SECRET`の
# 既定値と一致させる必要がある(2026-10-27、401 Invalid HMAC signatureの
# 直接原因)。本番ではAI_TM側の値と同時にローテーションすること
# (CRM受信側の`webhook_security._active_secrets()`のような無停止
# ローテーションはAI_TM側に実装されていない)。
_AITM_HMAC = "dev-crm-signing-secret-000"

REQUIRED_ENV = {
    "AITM_REVIEW_URL": "http://localhost:8011",
    # Bearerは/api/crm/*で未検証のためダミーのままで良い(検証有効化時に
    # 実値へ差し替えること)。
    "AITM_REVIEW_BEARER": _DUMMY, "AITM_REVIEW_SECRET": _AITM_HMAC,
    # 2026-08-17: port 8888は修正コミットを反映していない古いプロセスと
    # 判明。9000が現行の稼働先(endpoints.yamlと同じ判断)。
    "ERP_BASE_URL": "http://localhost:9000",
    # ERPは現時点で署名検証していないためダミーのままで良い(検証有効化
    # 時に実値へ差し替えること)。
    "ERP_COMMERCE_BEARER": _DUMMY, "ERP_COMMERCE_SECRET": _DUMMY,
    # 2026-08-17、ERP側にIF-25専用の`POST /crm/sales-orders`が新設され、
    # `/crm/commerce-check`と同じHMAC署名スキーム(未署名でも通る)に統一
    # された。旧`/sd/sales-orders`のOAuth2パスワードフロー対応は撤去済み。
    "ERP_SALES_ORDER_BEARER": _DUMMY, "ERP_SALES_ORDER_SECRET": _DUMMY,
    # AITM_LICENSE_*は今回の第3弾の対象外。現状維持(ダミーのまま)。
    "AITM_LICENSE_URL": "http://localhost:8012",
    "AITM_LICENSE_BEARER": _DUMMY, "AITM_LICENSE_SECRET": _DUMMY,
    # 2026-10-25、AI_TM側に`POST /api/deemed-export/events`(ai_validation
    # モジュール、8011)が実装されたことを確認(2026-08-16 E2E確認時点では
    # 「確認できなかった項目」だった)。ai_validationモジュール共通の
    # CRM_SIGNING_SECRETで検証される(Bearerは未検証のためダミーのまま)。
    "AITM_DEEMED_EXPORT_URL": "http://localhost:8011",
    "AITM_DEEMED_EXPORT_BEARER": _DUMMY, "AITM_DEEMED_EXPORT_SECRET": _AITM_HMAC,
    # 2026-10-27、契約書§4-1 A案(aitm.party.resolve)。platform-coreは
    # AITM_SCREENING_URL(8005)とは別モジュールなので既定8000で分ける。
    # /api/parties/resolve(platform-core/routers/party.py:78)は署名検証
    # なしのためBearer/Secretともダミーのままで良い(検証有効化時に
    # 実値へ差し替えること)。
    "AITM_PLATFORM_URL": "http://localhost:8000",
    "AITM_PLATFORM_BEARER": _DUMMY, "AITM_PLATFORM_SECRET": _DUMMY,
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
    from crm_mvp.services.party_compliance import register_aitm_party_dispatchers
    from crm_mvp.services.review_case import register_aitm_dispatchers

    register_aitm_dispatchers()
    register_erp_dispatchers()
    register_erp_transcription_dispatchers()
    register_aitm_license_dispatchers()
    register_deemed_export_dispatchers()
    register_aitm_party_dispatchers()
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
