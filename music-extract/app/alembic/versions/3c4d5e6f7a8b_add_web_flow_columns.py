"""columnas del flujo de la web: vista previa, intentos con reinicio, tracklist

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-11-09 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3c4d5e6f7a8b'
down_revision: Union[str, Sequence[str], None] = '2b3c4d5e6f7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origen', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('carpeta', sa.String(length=255), nullable=True))

    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rondas_usadas', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('duration_s', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('ya_descargada', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('motivo', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('preview', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('tracklist', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('tracklist')
        batch_op.drop_column('preview')
        batch_op.drop_column('motivo')
        batch_op.drop_column('ya_descargada')
        batch_op.drop_column('duration_s')
        batch_op.drop_column('rondas_usadas')

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('carpeta')
        batch_op.drop_column('origen')
