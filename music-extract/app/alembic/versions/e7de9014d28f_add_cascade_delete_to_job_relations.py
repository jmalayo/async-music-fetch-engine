"""add cascade delete to job relations

Revision ID: e7de9014d28f
Revises: effd14b5f4f3
Create Date: 2026-07-31 11:13:14.303958

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7de9014d28f'
down_revision: Union[str, Sequence[str], None] = 'effd14b5f4f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


naming_convention = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('attempts', schema=None, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_attempts_track_id_tracks'), type_='foreignkey')
        batch_op.create_foreign_key(batch_op.f('fk_attempts_track_id_tracks'), 'tracks', ['track_id'], ['track_id'], ondelete='CASCADE')

    with op.batch_alter_table('retries', schema=None, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_retries_job_id_jobs'), type_='foreignkey')
        batch_op.create_foreign_key(batch_op.f('fk_retries_job_id_jobs'), 'jobs', ['job_id'], ['job_id'], ondelete='CASCADE')

    with op.batch_alter_table('tracks', schema=None, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_tracks_job_id_jobs'), type_='foreignkey')
        batch_op.create_foreign_key(batch_op.f('fk_tracks_job_id_jobs'), 'jobs', ['job_id'], ['job_id'], ondelete='CASCADE')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tracks', schema=None, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_tracks_job_id_jobs'), type_='foreignkey')
        batch_op.create_foreign_key(batch_op.f('fk_tracks_job_id_jobs'), 'jobs', ['job_id'], ['job_id'])

    with op.batch_alter_table('retries', schema=None, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_retries_job_id_jobs'), type_='foreignkey')
        batch_op.create_foreign_key(batch_op.f('fk_retries_job_id_jobs'), 'jobs', ['job_id'], ['job_id'])

    with op.batch_alter_table('attempts', schema=None, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_attempts_track_id_tracks'), type_='foreignkey')
        batch_op.create_foreign_key(batch_op.f('fk_attempts_track_id_tracks'), 'tracks', ['track_id'], ['track_id'])
