"""qc tier 2 band nullable

Tier 2 on CER only (2026-11-11): only sentences get a level. Image, conversation
starter and free speech clips get a qc_results row with every metric but no band
(band NULL, band_reason 'not_applicable'). qc_results.band becomes nullable (NULL
or 1-4), and a check keeps band NULL exactly when band_reason = 'not_applicable'.
Existing rows all have a band and another reason, so they pass the new check.

Revision ID: c7d3a9e5f218
Revises: b4e81c6d2a57
Create Date: 2026-11-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7d3a9e5f218'
down_revision: Union[str, Sequence[str], None] = 'b4e81c6d2a57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_qc_results_band', 'qc_results', type_='check')
    op.alter_column('qc_results', 'band', nullable=True)
    op.create_check_constraint('ck_qc_results_band', 'qc_results', 'band IS NULL OR band BETWEEN 1 AND 4')
    op.create_check_constraint(
        'ck_qc_results_band_not_applicable', 'qc_results', "(band IS NULL) = (band_reason = 'not_applicable')"
    )


def downgrade() -> None:
    """Downgrade schema. Fails if rows without a band exist: delete or re-score them first
    (e.g. DELETE FROM qc_results WHERE band IS NULL; then UPDATE recordings to match)."""
    op.drop_constraint('ck_qc_results_band_not_applicable', 'qc_results', type_='check')
    op.drop_constraint('ck_qc_results_band', 'qc_results', type_='check')
    op.alter_column('qc_results', 'band', nullable=False)
    op.create_check_constraint('ck_qc_results_band', 'qc_results', 'band BETWEEN 1 AND 4')
