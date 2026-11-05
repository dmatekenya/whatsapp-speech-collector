"""free_speech task type and audio origin

assignments.prompt_id becomes nullable: unprompted task types (free_speech)
have assignments with no prompt. recordings.audio_origin (recorded,
forwarded, uploaded, unknown), backfilled from is_voice_note and forwarded.

Revision ID: d8e2b5c1f9a4
Revises: c4f1a8d27e53
Create Date: 2026-11-05 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e2b5c1f9a4'
down_revision: Union[str, Sequence[str], None] = 'c4f1a8d27e53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('assignments', 'prompt_id', existing_type=sa.Text(), nullable=True)

    op.add_column(
        'recordings', sa.Column('audio_origin', sa.Text(), server_default='unknown', nullable=False)
    )
    op.create_check_constraint(
        'ck_recordings_audio_origin',
        'recordings',
        "audio_origin IN ('recorded', 'forwarded', 'uploaded', 'unknown')",
    )
    # Same rule as the worker: forwarded wins; then Meta's voice flag; else unknown (Twilio).
    op.execute(
        """
        UPDATE recordings SET audio_origin = CASE
            WHEN forwarded IN ('forwarded', 'frequently_forwarded') THEN 'forwarded'
            WHEN is_voice_note IS TRUE THEN 'recorded'
            WHEN is_voice_note IS FALSE THEN 'uploaded'
            ELSE 'unknown'
        END
        """
    )


def downgrade() -> None:
    """Downgrade schema. Fails if free_speech assignments (no prompt) exist."""
    op.drop_constraint('ck_recordings_audio_origin', 'recordings', type_='check')
    op.drop_column('recordings', 'audio_origin')
    op.alter_column('assignments', 'prompt_id', existing_type=sa.Text(), nullable=False)
