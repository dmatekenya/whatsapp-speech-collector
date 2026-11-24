"""Expose calibration clips and append-only study checks to supervision.

Revision ID: 2f91b861dc04
Revises: e81c4d29b702
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "2f91b861dc04"
down_revision = "e81c4d29b702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calibration_clip_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("recording_id", sa.BigInteger(), sa.ForeignKey("calibration_clips.recording_id"), nullable=False),
        sa.Column("as_intended", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("operation_key", postgresql.UUID(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("as_intended IN ('yes', 'no', 'unclear')", name="ck_calibration_checks_as_intended"),
    )
    op.execute("""
        CREATE INDEX ix_calibration_checks_latest
        ON public.calibration_clip_checks (recording_id, created_at DESC, id DESC)
    """)
    op.execute("""
        CREATE FUNCTION public.reject_calibration_check_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'calibration_clip_checks is append-only'
                USING ERRCODE = '55000';
        END
        $$;
        CREATE TRIGGER calibration_checks_append_only
            BEFORE UPDATE OR DELETE ON public.calibration_clip_checks
            FOR EACH STATEMENT EXECUTE FUNCTION public.reject_calibration_check_mutation();
    """)
    op.execute("""
        CREATE VIEW public.calibration_review_clips AS
        SELECT c.recording_id, e.participant_id, c.enrollment_id, p.is_test, c.condition_code,
               CASE c.condition_code
                   WHEN 'C1' THEN 'Good'
                   WHEN 'C2' THEN 'Too quiet'
                   WHEN 'C3' THEN 'Arm''s length'
                   WHEN 'C4' THEN 'Background voices'
                   WHEN 'C5' THEN 'Background music'
                   WHEN 'C6' THEN 'Noisy surroundings'
                   WHEN 'C7' THEN 'Echo'
                   WHEN 'C8' THEN 'Muffled microphone'
                   WHEN 'C9' THEN 'Silence in a quiet room'
                   WHEN 'C10' THEN 'Silence with speech'
               END AS condition_name,
               c.task_type, c.prompt_id, c.attempt, c.order_position,
               r.duration_s, r.active_seconds, r.mean_dbfs, r.wav_path, c.created_at,
               latest.as_intended, latest.reviewer, latest.created_at AS checked_at
        FROM public.calibration_clips c
        JOIN public.recordings r ON r.id = c.recording_id
        JOIN public.enrollments e ON e.id = c.enrollment_id
        JOIN public.participants p ON p.participant_id = e.participant_id
        LEFT JOIN LATERAL (
            SELECT as_intended, reviewer, created_at
            FROM public.calibration_clip_checks
            WHERE recording_id = c.recording_id
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        ) latest ON true
    """)
    op.execute("""
        REVOKE ALL ON public.calibration_review_clips, public.calibration_clip_checks FROM PUBLIC;
        DO $$
        DECLARE reader text;
        BEGIN
            FOREACH reader IN ARRAY ARRAY['sc_readonly', 'masakhane_supervision_ro'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader) THEN
                    EXECUTE format('REVOKE ALL ON public.calibration_review_clips, public.calibration_clip_checks FROM %I', reader);
                    EXECUTE format('GRANT SELECT ON public.calibration_review_clips TO %I', reader);
                END IF;
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'masakhane_qc_writer') THEN
                REVOKE ALL ON public.calibration_review_clips, public.calibration_clip_checks FROM masakhane_qc_writer;
                GRANT INSERT, SELECT ON public.calibration_clip_checks TO masakhane_qc_writer;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP VIEW public.calibration_review_clips")
    op.drop_table("calibration_clip_checks")
    op.execute("DROP FUNCTION public.reject_calibration_check_mutation()")
