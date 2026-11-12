"""add nube a tracks (copia de la canción en R2)

Revision ID: 7b8c9d0e1f2a
Revises: 6f7a8b9c0d1e
Create Date: 2026-11-12 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b8c9d0e1f2a'
down_revision: Union[str, Sequence[str], None] = '6f7a8b9c0d1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tracks') as batch:
        batch.add_column(sa.Column('nube', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tracks') as batch:
        batch.drop_column('nube')
