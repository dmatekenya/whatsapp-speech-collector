"""length flag and long form

recordings.length_flag: long_sentence or long_clip, for clips longer than the task
type's flag_over (config clip_limits). Information only (qc_status is unchanged);
such clips are split before training. Existing rows stay NULL: nothing stored so
far is longer than the old 29 s maximum.
transcripts.long_form: whether the ASR used Whisper long-form generation (clips
over 30 s). Existing rows stay NULL.

Revision ID: f3a7c2d9e816
Revises: 9698f26aa068
Create Date: 2026-11-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a7c2d9e816'
down_revision: Union[str, Sequence[str], None] = '9698f26aa068'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('recordings', sa.Column('length_flag', sa.Text(), nullable=True))
    op.create_check_constraint(
        'ck_recordings_length_flag', 'recordings', "length_flag IN ('long_sentence', 'long_clip')"
    )
    op.add_column('transcripts', sa.Column('long_form', sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transcripts', 'long_form')
    op.drop_constraint('ck_recordings_length_flag', 'recordings', type_='check')
    op.drop_column('recordings', 'length_flag')
