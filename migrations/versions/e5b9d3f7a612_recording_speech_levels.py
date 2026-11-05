"""recording speech levels

recordings.active_seconds and recordings.mean_dbfs, measured by the worker on
the 16 kHz WAV (config clip_checks). Existing rows stay NULL here; fill them
with scripts/backfill_speech_levels.py.

Revision ID: e5b9d3f7a612
Revises: d8e2b5c1f9a4
Create Date: 2026-11-05 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b9d3f7a612'
down_revision: Union[str, Sequence[str], None] = 'd8e2b5c1f9a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('recordings', sa.Column('active_seconds', sa.Float(), nullable=True))
    op.add_column('recordings', sa.Column('mean_dbfs', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('recordings', 'mean_dbfs')
    op.drop_column('recordings', 'active_seconds')
