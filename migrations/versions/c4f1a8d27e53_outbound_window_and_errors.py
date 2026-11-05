"""outbound: skipped_window status, provider error details, state snapshot

Revision ID: c4f1a8d27e53
Revises: b7c3e9a41d20
Create Date: 2026-11-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c4f1a8d27e53'
down_revision: Union[str, Sequence[str], None] = 'b7c3e9a41d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUSES = "status IN ('pending', 'sent', 'failed', 'delivered', 'read')"
NEW_STATUSES = "status IN ('pending', 'sent', 'failed', 'delivered', 'read', 'skipped_window')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_outbound_messages_status', 'outbound_messages', type_='check')
    op.create_check_constraint('ck_outbound_messages_status', 'outbound_messages', NEW_STATUSES)
    op.add_column('outbound_messages', sa.Column('error_details', postgresql.JSONB(), nullable=True))
    op.add_column('outbound_messages', sa.Column('state_snapshot', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    """Downgrade schema. skipped_window rows become failed."""
    op.drop_column('outbound_messages', 'state_snapshot')
    op.drop_column('outbound_messages', 'error_details')
    op.drop_constraint('ck_outbound_messages_status', 'outbound_messages', type_='check')
    op.execute("UPDATE outbound_messages SET status = 'failed' WHERE status = 'skipped_window'")
    op.create_check_constraint('ck_outbound_messages_status', 'outbound_messages', OLD_STATUSES)
