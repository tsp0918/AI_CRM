-- crm_app: アプリケーション/運用スクリプトが接続する非 superuser ロール。
--
-- §7.4: RLS (Row-Level Security) は superuser を無条件に素通りする
-- (FORCE ROW LEVEL SECURITY を付けても superuser には効かない、これは
-- Postgres の仕様で回避不可能)。ローカル開発用の DB ユーザーが
-- superuser だったため、実際にテナント分離を効かせるには非superuser
-- ロールでの接続が必須と判明した。
--
-- Alembic マイグレーション(DDL)は引き続き所有者ロール(superuser)で
-- 実行する。crm_app は DML(SELECT/INSERT/UPDATE/DELETE)のみ。
--
-- 実行: psql -d crm_mvp -f scripts/provision_app_role.sql

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crm_app') THEN
    CREATE ROLE crm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE crm_mvp TO crm_app;
GRANT USAGE ON SCHEMA public TO crm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;

-- 今後のマイグレーションで追加されるテーブルにも自動的に付与する。
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
