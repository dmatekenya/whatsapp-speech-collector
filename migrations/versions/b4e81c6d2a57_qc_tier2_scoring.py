"""qc tier 2 scoring

ASR batch step 2 (src/qc): recordings.snr_db (WADA-SNR, an audio fact computed
once) and recordings.qc_tier2 (level 1-4, a copy of the latest qc_results.band;
NULL = not scored). New table qc_results: one row per scoring run per
recording, never updated by the scorer. A composite foreign key to
transcripts (id, recording_id) means a result can never pair a recording with
another recording's transcript; the unique constraint it needs on transcripts
changes no data. Existing rows stay NULL / empty until the first scoring run.

Revision ID: b4e81c6d2a57
Revises: f3a7c2d9e816
Create Date: 2026-11-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e81c6d2a57'
down_revision: Union[str, Sequence[str], None] = 'f3a7c2d9e816'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('recordings', sa.Column('snr_db', sa.Float(), nullable=True))
    op.add_column('recordings', sa.Column('qc_tier2', sa.SmallInteger(), nullable=True))
    op.create_check_constraint('ck_recordings_qc_tier2', 'recordings', 'qc_tier2 BETWEEN 1 AND 4')
    op.create_unique_constraint('uq_transcripts_id_recording_id', 'transcripts', ['id', 'recording_id'])
    op.create_table(
        'qc_results',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('recording_id', sa.BigInteger(), nullable=False),
        sa.Column('transcript_id', sa.BigInteger(), nullable=False),
        sa.Column('stage', sa.Text(), server_default='asr', nullable=False),
        sa.Column('word_count', sa.Integer(), nullable=True),
        sa.Column('words_per_second', sa.Float(), nullable=True),
        sa.Column('compression_ratio', sa.Float(), nullable=True),
        sa.Column('cer', sa.Float(), nullable=True),
        sa.Column('wer', sa.Float(), nullable=True),
        sa.Column('length_ratio', sa.Float(), nullable=True),
        sa.Column('has_digits', sa.Boolean(), nullable=True),
        sa.Column('band', sa.SmallInteger(), nullable=False),
        sa.Column('band_reason', sa.Text(), nullable=False),
        sa.Column('metrics_version', sa.Text(), nullable=False),
        sa.Column('thresholds_version', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(
            ['recording_id'], ['recordings.id'], name='fk_qc_results_recording_id', ondelete='RESTRICT'
        ),
        sa.ForeignKeyConstraint(
            ['transcript_id', 'recording_id'],
            ['transcripts.id', 'transcripts.recording_id'],
            name='fk_qc_results_transcript_recording',
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint('band BETWEEN 1 AND 4', name='ck_qc_results_band'),
    )
    op.create_index('ix_qc_results_recording_created_id', 'qc_results', ['recording_id', 'created_at', 'id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_qc_results_recording_created_id', table_name='qc_results')
    op.drop_table('qc_results')
    op.drop_constraint('uq_transcripts_id_recording_id', 'transcripts', type_='unique')
    op.drop_constraint('ck_recordings_qc_tier2', 'recordings', type_='check')
    op.drop_column('recordings', 'qc_tier2')
    op.drop_column('recordings', 'snr_db')
