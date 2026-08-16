#!/usr/bin/env bash
# ローカル起動スクリプト。
#
#   ./start.sh
#
# PostgreSQL が起動済みで、crm_mvp DB と crm_app ロールが
# 作成済みであることを前提とする(初回のみ scripts/provision_app_role.sql
# の実行が必要)。マイグレーションは所有者ロールで適用し、
# アプリ本体は非 superuser の crm_app ロールで起動する(§7.4 RLS)。

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "エラー: .venv が見つかりません。先に 'python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt' を実行してください。" >&2
  exit 1
fi

if ! pg_isready -q 2>/dev/null; then
  echo "エラー: PostgreSQL に接続できません。'brew services start postgresql@16' 等で起動してください。" >&2
  exit 1
fi

if ! psql -U crm_app -d crm_mvp -c "SELECT 1" >/dev/null 2>&1; then
  echo "エラー: crm_app ロールまたは crm_mvp DB に接続できません。初回セットアップとして:" >&2
  echo "  createdb crm_mvp   # 未作成の場合" >&2
  echo "  psql -d crm_mvp -f scripts/provision_app_role.sql" >&2
  echo "を実行してください。" >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "マイグレーションを適用しています..."
unset DATABASE_URL  # alembic.ini の既定(所有者ロール)を使う
alembic upgrade head

# 8000番台/9000番台はAI_TM/ERP側が使用するため避ける(2026-08-16)。
PORT="${PORT:-7500}"
HOST="127.0.0.1"
DEMO_TENANT="00000000-0000-0000-0000-0000000000aa"

echo ""
echo "=================================================================="
echo "  Compliance-aware Agentic CRM (MVP)"
echo "=================================================================="
echo "  アプリ(ワークスペース切替 → ダッシュボード):"
echo "    http://${HOST}:${PORT}/ui/workspace"
echo "    お試し用 Tenant ID(サンプル案件入り): ${DEMO_TENANT}"
echo ""
echo "  API ドキュメント(Swagger UI):"
echo "    http://${HOST}:${PORT}/docs"
echo ""
echo "  JSON API は X-Tenant-Id ヘッダーが必須です(未認証の暫定仕様)。"
echo "  停止: Ctrl+C"
echo "=================================================================="
echo ""

exec uvicorn crm_mvp.api.app:app --host "${HOST}" --port "${PORT}" --reload
