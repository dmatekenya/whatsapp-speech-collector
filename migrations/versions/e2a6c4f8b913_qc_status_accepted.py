"""qc status accepted

Tier 1 (recordings.qc_status) now ends every turn in accepted, needs_review or rejected
(decision 2026-11-11, docs/methodology.md 8.2). Until now a clip that passed the arrival
checks kept the default 'pending', and a duplicate was never rejected; 'pending' now means
only "stored, the conversation engine hasn't finished its turn yet".

Existing clips whose inbound message the engine finished (status 'processed'):
- pending duplicates -> rejected, qc_reason 'duplicate';
- other pending clips -> accepted (they passed: a failure would have rejected them).
A clip whose message isn't processed (failed or still queued) stays pending. The recordings
trigger recomputes qc_final and bumps updated_at where qc_final changes.

Revision ID: e2a6c4f8b913
Revises: d9f4b2c8e631
Create Date: 2026-11-11 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e2a6c4f8b913'
down_revision: Union[str, Sequence[str], None] = 'd9f4b2c8e631'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FINISHED = """
    r.status = 'stored' AND r.qc_status = 'pending'
    AND EXISTS (SELECT 1 FROM inbound_messages m WHERE m.id = r.inbound_message_id AND m.status = 'processed')
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        f"UPDATE recordings r SET qc_status = 'rejected', qc_reason = 'duplicate'"
        f" WHERE {FINISHED} AND r.duplicate_of_recording_id IS NOT NULL"
    )
    op.execute(f"UPDATE recordings r SET qc_status = 'accepted' WHERE {FINISHED}")


def downgrade() -> None:
    """Downgrade schema: back to the old convention (passing clips pending, duplicates not
    rejected). Also reverts clips accepted or rejected as duplicates by the new code."""
    op.execute("UPDATE recordings SET qc_status = 'pending', qc_reason = NULL WHERE qc_status = 'accepted'")
    op.execute(
        "UPDATE recordings SET qc_status = 'pending', qc_reason = NULL"
        " WHERE qc_status = 'rejected' AND qc_reason = 'duplicate'"
    )
