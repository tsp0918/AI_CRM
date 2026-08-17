"""シミュレータがAI_TM/ERPになりすましてCRMのWebhook受信エンドポイントへ
送信する際の署名鍵(ローカル開発専用のダミー値)。

`crm_mvp/api/webhook_security.py`の`verify_webhook`は対応する環境変数が
CRMサーバ側で未設定だと500 CONFIG_ERRORで拒否する(フェイルクローズ)。
このシミュレーションを動かすには、CRMサーバをこれらの値を環境変数として
エクスポートした状態で起動し直す必要がある(`simulation/scripts/restart_crm_with_webhook_secrets.sh`)。
本番の秘密情報ではないため、リポジトリに直接記述してよい。
"""

from __future__ import annotations

WEBHOOK_SECRETS: dict[str, dict[str, str]] = {
    "aitm_review": {
        "bearer_env": "AITM_REVIEW_WEBHOOK_BEARER", "bearer": "sim-aitm-review-bearer-dev",
        "secret_env": "AITM_REVIEW_WEBHOOK_SECRET", "secret": "sim-aitm-review-secret-dev",
        "path": "/webhooks/aitm/review-result",
    },
    "aitm_party": {
        "bearer_env": "AITM_PARTY_WEBHOOK_BEARER", "bearer": "sim-aitm-party-bearer-dev",
        "secret_env": "AITM_PARTY_WEBHOOK_SECRET", "secret": "sim-aitm-party-secret-dev",
        "path": "/webhooks/aitm/party-event",
    },
    "aitm_rnd": {
        "bearer_env": "AITM_RND_WEBHOOK_BEARER", "bearer": "sim-aitm-rnd-bearer-dev",
        "secret_env": "AITM_RND_WEBHOOK_SECRET", "secret": "sim-aitm-rnd-secret-dev",
        "path": "/webhooks/rnd-opportunity",
    },
    "aitm_deemed_export": {
        "bearer_env": "AITM_DEEMED_EXPORT_WEBHOOK_BEARER", "bearer": "sim-aitm-deemedexport-bearer-dev",
        "secret_env": "AITM_DEEMED_EXPORT_WEBHOOK_SECRET", "secret": "sim-aitm-deemedexport-secret-dev",
        "path": "/webhooks/deemed-export-risk",
    },
    "aitm_monitoring": {
        "bearer_env": "AITM_MONITORING_WEBHOOK_BEARER", "bearer": "sim-aitm-monitoring-bearer-dev",
        "secret_env": "AITM_MONITORING_WEBHOOK_SECRET", "secret": "sim-aitm-monitoring-secret-dev",
        "path": "/webhooks/contract-monitoring",
    },
    "erp": {
        "bearer_env": "ERP_WEBHOOK_BEARER", "bearer": "sim-erp-webhook-bearer-dev",
        "secret_env": "ERP_WEBHOOK_SECRET", "secret": "sim-erp-webhook-secret-dev",
        "path": None,  # crm_mvp/api/erp_webhooks.py内でエンドポイントごとに異なる
    },
}


def as_env_exports() -> str:
    """`export FOO=bar`形式の複数行。CRMサーバ再起動時にそのままevalする。"""
    lines = []
    for cfg in WEBHOOK_SECRETS.values():
        lines.append(f'export {cfg["bearer_env"]}="{cfg["bearer"]}"')
        lines.append(f'export {cfg["secret_env"]}="{cfg["secret"]}"')
    return "\n".join(lines)
