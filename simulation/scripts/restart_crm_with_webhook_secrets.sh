#!/usr/bin/env bash
# シミュレータがAI_TM/ERPになりすましてCRMのWebhook受信エンドポイントに
# 送信できるよう、署名検証用の環境変数(simulation/src/webhook_secrets.py)を
# エクスポートした状態でCRMサーバを再起動する。
#
# これらは verify_webhook() がフェイルクローズで要求する設定であり
# (未設定だと500 CONFIG_ERROR)、通常の ./start.sh には含めていない
# (シミュレーション専用のダミー鍵のため)。

set -euo pipefail
cd "$(dirname "$0")/../.."

echo "既存のCRMサーバ(port 7500)を停止します..."
pkill -f "uvicorn crm_mvp.api.app:app.*--port 7500" 2>/dev/null && sleep 1 || echo "  (起動していませんでした)"

eval "$(PYTHONPATH=. .venv/bin/python -c 'from simulation.src.webhook_secrets import as_env_exports; print(as_env_exports())')"

source .venv/bin/activate
nohup env PORT=7500 ./start.sh > /tmp/crm_server_sim.log 2>&1 &
disown
echo "CRMサーバを再起動しました(ログ: /tmp/crm_server_sim.log)。起動待ち..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:7500/health >/dev/null 2>&1; then
    echo "OK: CRMサーバが起動しました。"
    exit 0
  fi
  sleep 1
done
echo "エラー: 30秒待っても起動確認できませんでした。/tmp/crm_server_sim.log を確認してください。" >&2
exit 1
