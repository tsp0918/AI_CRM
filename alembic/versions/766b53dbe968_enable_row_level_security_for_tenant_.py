"""enable row level security for tenant isolation

HANDOVER.md §7.4: ユーザー決定によりテナント分離方式を Row-Level
Security に確定(2026-08-13)。全16テーブルの tenant_id 列に対して
RLS ポリシーを設定する。

FORCE ROW LEVEL SECURITY を使うことで、テーブル所有者(このアプリの
接続ロール)自身にも RLS を適用させる。これにより別ロールを新設せずに
単一の DB ユーザーのままテナント分離を機能させられる — 別ロール運用は
ローカル開発環境の pg_hba.conf を変更する必要があり、他プロジェクトが
同居する共有 Postgres インスタンスへの影響が大きいため avoid した。

current_setting('app.current_tenant_id', true) は未設定なら NULL を返す
(第2引数 true = missing_ok)。NULL の場合は比較が偽になり、
「テナント文脈を明示的に SET しない限り 0 件」という安全側の既定になる。
アプリ側は crm_mvp/api/deps.py の get_tenant_scoped_session が
リクエストごとに SET LOCAL する。管理スクリプト(seed_policies.py /
daily_snapshot.py)も同様に自分で SET してから操作する。

Revision ID: 766b53dbe968
Revises: 3cec7569c151
Create Date: 2026-08-13 09:34:11.824571

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '766b53dbe968'
down_revision: Union[str, Sequence[str], None] = '3cec7569c151'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_SCOPED_TABLES: list[str] = [
    "account", "contact", "compliance_status",
    "engagement", "stage_transition", "pipeline_snapshot",
    "qualification_slot",
    "graph_node", "graph_edge", "engagement_role",
    "gate_policy", "gate_evaluation", "waiver",
    "ingestion_source", "extraction_proposal", "field_autonomy_policy",
]

_POLICY_EXPR = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_POLICY_EXPR}) WITH CHECK ({_POLICY_EXPR})"
        )


def downgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
