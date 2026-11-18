"""Store QC calibration progress, clip metadata and interactive replies.

Revision ID: e81c4d29b702
Revises: 6ca8042a93f1
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e81c4d29b702"
down_revision = "6ca8042a93f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calibration_state",
        sa.Column("enrollment_id", sa.BigInteger(), sa.ForeignKey("enrollments.id"), primary_key=True),
        sa.Column("context", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_table(
        "calibration_clips",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("recording_id", sa.BigInteger(), sa.ForeignKey("recordings.id"), nullable=False, unique=True),
        sa.Column("enrollment_id", sa.BigInteger(), sa.ForeignKey("enrollments.id"), nullable=False),
        sa.Column("condition_code", sa.Text(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("task_position", sa.Integer(), nullable=False),
        sa.Column("prompt_id", sa.Text(), sa.ForeignKey("prompts.prompt_id"), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("suggested_condition", sa.Text(), nullable=False),
        sa.Column("order_position", sa.Integer(), nullable=False),
        sa.Column("fast_qc", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("enrollment_id", "condition_code", "task_position", "attempt",
                            name="uq_calibration_clips_attempt"),
        sa.CheckConstraint("task_position >= 1 AND attempt >= 1 AND order_position BETWEEN 1 AND 10",
                           name="ck_calibration_clips_positions"),
    )
    op.add_column("outbound_messages", sa.Column("interactive", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbound_messages", "interactive")
    op.drop_table("calibration_clips")
    op.drop_table("calibration_state")
