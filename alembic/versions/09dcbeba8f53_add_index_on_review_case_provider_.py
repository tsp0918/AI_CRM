"""add index on review_case provider_request_id

Revision ID: 09dcbeba8f53
Revises: bf6015bcc588
Create Date: 2026-10-27 09:04:03.110132

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '09dcbeba8f53'
down_revision: Union[str, Sequence[str], None] = 'bf6015bcc588'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f('ix_review_case_provider_request_id'), 'review_case',
        ['provider_request_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_review_case_provider_request_id'), table_name='review_case')
