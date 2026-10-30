"""conversation starters

Revision ID: e15062b700c3
Revises: 6562ce7cf086
Create Date: 2026-10-18 12:54:51.785254

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e15062b700c3'
down_revision: Union[str, Sequence[str], None] = '6562ce7cf086'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # IDs C0001, ... for conversation starters (formatted in code).
    op.execute("CREATE SEQUENCE prompt_conversation_starter_id_seq START 1")
    # Domain is optional for conversation starters.
    op.alter_column("prompts", "domain", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # Fails if any prompt has no domain; set one first.
    op.alter_column("prompts", "domain", existing_type=sa.Text(), nullable=False)
    op.execute("DROP SEQUENCE prompt_conversation_starter_id_seq")
