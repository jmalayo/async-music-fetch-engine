"""add carpetas (nombre, favorita, almacenamiento y sincronización de cada carpeta)

Revision ID: 9d0e1f2a3b4c
Revises: 8c9d0e1f2a3b
Create Date: 2026-11-15 23:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d0e1f2a3b4c'
down_revision: Union[str, Sequence[str], None] = '8c9d0e1f2a3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'carpetas',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('dueno', sa.String(length=80), nullable=False),
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('nombre', sa.String(length=255), nullable=False),
        sa.Column('favorita', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('almacenamiento', sa.String(length=8), server_default='nube', nullable=False),
        sa.Column('sincronizacion', sa.String(length=8), server_default='manual', nullable=False),
        sa.UniqueConstraint('dueno', 'slug', name='uq_carpetas_dueno_slug'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('carpetas')
