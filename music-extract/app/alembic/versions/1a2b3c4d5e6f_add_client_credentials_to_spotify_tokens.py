"""add client credentials to spotify tokens

Revision ID: 1a2b3c4d5e6f
Revises: 066870430e23
Create Date: 2026-08-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, Sequence[str], None] = '066870430e23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('spotify_tokens', sa.Column('client_id', sa.String(length=64), nullable=True))
    op.add_column('spotify_tokens', sa.Column('client_secret', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('spotify_tokens', 'client_secret')
    op.drop_column('spotify_tokens', 'client_id')
