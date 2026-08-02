"""rename track duration_s to elapsed_s

Revision ID: a1b2c3d4e5f6
Revises: e7de9014d28f
Create Date: 2026-07-31 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e7de9014d28f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.alter_column('duration_s', new_column_name='elapsed_s', existing_type=sa.Float(), existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.alter_column('elapsed_s', new_column_name='duration_s', existing_type=sa.Float(), existing_nullable=False)
