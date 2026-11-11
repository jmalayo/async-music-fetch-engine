"""add eliminada to tracks

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e6f7a8b9c0d'
down_revision: Union[str, Sequence[str], None] = '4d5e6f7a8b9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('eliminada', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('eliminada')
