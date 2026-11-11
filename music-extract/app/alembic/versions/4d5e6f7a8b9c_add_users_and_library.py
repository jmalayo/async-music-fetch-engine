"""usuarios y biblioteca: users, jobs.user_id y archivo/tamaño/descargas/recuperada en tracks

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-11-10 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4d5e6f7a8b9c'
down_revision: Union[str, Sequence[str], None] = '3c4d5e6f7a8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('username', sa.String(length=64), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('quota_bytes', sa.Integer(), nullable=False),
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.String(), nullable=False),
    )

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_jobs_user_id', 'users', ['user_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archivo', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column('bytes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('descargas', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('recuperada', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('recuperada')
        batch_op.drop_column('descargas')
        batch_op.drop_column('bytes')
        batch_op.drop_column('archivo')

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_jobs_user_id', type_='foreignkey')
        batch_op.drop_column('user_id')

    op.drop_table('users')
