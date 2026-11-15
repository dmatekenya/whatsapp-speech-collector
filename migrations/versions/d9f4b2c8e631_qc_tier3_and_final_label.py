"""qc tier 3 and final label

Tier 3 (human review) and the final label (docs/methodology.md 8.4, 8.5):

- qc_human_reviews: append-only, one row per supervisor decision (the supervision
  gateway inserts; nothing updates or deletes, except setting notified_at once).
- recordings.qc_tier3 / qc_tier3_reason: the latest decision, copied by trigger.
- recordings.qc_final: bronze / silver / gold / rejected, from qc_final_label(),
  the only place the rules live, set by a trigger whenever a tier column changes.
  Existing rows are backfilled (their updated_at is left as it was).
- Rights for the gateway's roles, only where those roles exist (the droplet).

Triggers:
- recordings, BEFORE INSERT OR UPDATE OF qc_status, qc_tier2, qc_tier3, assignment_id:
  qc_final from qc_final_label(); updated_at = now() when qc_tier2, qc_tier3 or
  qc_final changed (the gateway's incremental sync reads updated_at).
- qc_human_reviews, AFTER INSERT (SECURITY DEFINER): lock the recording, check the
  gateway's expected tier-3 state (SQLSTATE QC409 on a mismatch), copy the decision
  to recordings. AFTER, not BEFORE: with INSERT ... ON CONFLICT DO NOTHING (the
  gateway's retry-safe insert), BEFORE row triggers run even for a row that is then
  skipped as a duplicate; an AFTER trigger runs only for rows actually inserted.
- qc_human_reviews, BEFORE UPDATE OR DELETE: refused (SQLSTATE QC405), except
  setting notified_at from empty to a time with nothing else changed.

Revision ID: d9f4b2c8e631
Revises: c7d3a9e5f218
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f4b2c8e631'
down_revision: Union[str, Sequence[str], None] = 'c7d3a9e5f218'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REASONS = ('wrong_sentence', 'no_speech', 'noise', 'unclear', 'not_own_voice', 'not_chichewa', 'other')

# The final-label rules: the only place they live. First match wins.
QC_FINAL_LABEL = """
CREATE OR REPLACE FUNCTION qc_final_label(
    p_qc_status text, p_qc_tier2 smallint, p_qc_tier3 text, p_task_type text
) RETURNS text
LANGUAGE sql IMMUTABLE
SET search_path = public, pg_temp
AS $$
    SELECT CASE
        -- 1. The latest human decision wins.
        WHEN p_qc_tier3 = 'accept' THEN 'gold'
        WHEN p_qc_tier3 = 'reject' THEN 'rejected'
        -- 2. Rejected on arrival (tier 1).
        WHEN p_qc_status = 'rejected' THEN 'rejected'
        -- 3. Flagged on arrival, or not checked yet: waits.
        WHEN p_qc_status IN ('needs_review', 'pending') THEN 'bronze'
        -- 6. Unassigned recordings (no assignment): never counted, never exported.
        WHEN p_task_type IS NULL THEN 'bronze'
        -- 4. Sentences: validated by tier 2 at level 3 or 4; otherwise (1, 2, not scored) waits.
        WHEN p_task_type = 'sentence' THEN CASE WHEN p_qc_tier2 IN (3, 4) THEN 'silver' ELSE 'bronze' END
        -- 5. Spontaneous types (tier 2 not applicable): Silver once accepted on arrival
        --    (decision 2026-11-11, until tier 2 has a reliable signal for them).
        WHEN p_task_type IN ('image', 'conversation_starter', 'free_speech') THEN 'silver'
        ELSE 'bronze'
    END
$$;
"""

RECORDINGS_TRIGGER = """
CREATE OR REPLACE FUNCTION recordings_set_qc_final() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_task_type text;
BEGIN
    IF NEW.assignment_id IS NOT NULL THEN
        SELECT prompt_type INTO v_task_type FROM assignments WHERE id = NEW.assignment_id;
    END IF;
    NEW.qc_final := qc_final_label(NEW.qc_status, NEW.qc_tier2, NEW.qc_tier3, v_task_type);
    IF TG_OP = 'UPDATE' AND (
        NEW.qc_tier2 IS DISTINCT FROM OLD.qc_tier2
        OR NEW.qc_tier3 IS DISTINCT FROM OLD.qc_tier3
        OR NEW.qc_final IS DISTINCT FROM OLD.qc_final
    ) THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_recordings_qc_final
BEFORE INSERT OR UPDATE OF qc_status, qc_tier2, qc_tier3, assignment_id ON recordings
FOR EACH ROW EXECUTE FUNCTION recordings_set_qc_final();
"""

# Runs as the function's owner (the app's owner role), so the gateway's writer role, which has
# no rights on recordings, can still have its decision copied there. Reachable only through
# the trigger (a trigger function can't be called directly; EXECUTE is revoked from PUBLIC).
APPLY_REVIEW_TRIGGER = """
CREATE OR REPLACE FUNCTION qc_human_reviews_apply() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_current text;
    v_expected text;
BEGIN
    -- Serialise decisions on this recording: a concurrent one waits here, then sees the new state.
    SELECT qc_tier3 INTO v_current FROM recordings WHERE id = NEW.recording_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'recording % does not exist', NEW.recording_id USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- Compare-and-set (the supervision gateway): masakhane.expected_qc_tier3, set for the
    -- transaction with set_config(..., true), must equal the current qc_tier3 ('__NULL__' = none).
    -- Not set (NULL, or '' once the setting has been used earlier in the session): no check.
    v_expected := current_setting('masakhane.expected_qc_tier3', true);
    IF v_expected IS NOT NULL AND v_expected <> '' THEN
        IF v_expected NOT IN ('accept', 'reject', '__NULL__') THEN
            RAISE EXCEPTION 'masakhane.expected_qc_tier3 must be accept, reject or __NULL__, not %', v_expected
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_expected = '__NULL__' THEN
            v_expected := NULL;
        END IF;
        IF v_expected IS DISTINCT FROM v_current THEN
            RAISE EXCEPTION 'tier-3 state of recording % changed: expected %, now %',
                NEW.recording_id, coalesce(v_expected, 'none'), coalesce(v_current, 'none')
                USING ERRCODE = 'QC409',
                      HINT = 'Refresh the recording and submit again (new operation key).';
        END IF;
    END IF;

    -- The latest decision is the current verdict; the recordings trigger sets qc_final and updated_at.
    UPDATE recordings SET qc_tier3 = NEW.decision, qc_tier3_reason = NEW.reason_code
    WHERE id = NEW.recording_id;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION qc_human_reviews_apply() FROM PUBLIC;

CREATE TRIGGER trg_qc_human_reviews_apply
AFTER INSERT ON qc_human_reviews
FOR EACH ROW EXECUTE FUNCTION qc_human_reviews_apply();
"""

APPEND_ONLY_TRIGGER = """
CREATE OR REPLACE FUNCTION qc_human_reviews_append_only() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    -- The one allowed change: recording when the contributor was told (notified_at, once).
    IF TG_OP = 'UPDATE'
       AND OLD.notified_at IS NULL AND NEW.notified_at IS NOT NULL
       AND (NEW.id, NEW.recording_id, NEW.decision, NEW.reason_code, NEW.note, NEW.reviewer_id,
            NEW.reviewer_name, NEW.operation_key, NEW.created_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.recording_id, OLD.decision, OLD.reason_code, OLD.note, OLD.reviewer_id,
            OLD.reviewer_name, OLD.operation_key, OLD.created_at)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'qc_human_reviews is append-only: % refused', TG_OP
        USING ERRCODE = 'QC405',
              HINT = 'To correct a decision, insert a new one; the latest row is the current verdict.';
END
$$;

CREATE TRIGGER trg_qc_human_reviews_append_only
BEFORE UPDATE OR DELETE ON qc_human_reviews
FOR EACH ROW EXECUTE FUNCTION qc_human_reviews_append_only();
"""

# Only where the roles exist (on the droplet; not on laptops or the test database).
GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'masakhane_qc_writer') THEN
        GRANT INSERT, SELECT ON qc_human_reviews TO masakhane_qc_writer;
        -- No rights on recordings at all (the gateway refuses to write otherwise); decisions reach
        -- recordings only through the SECURITY DEFINER trigger. Also revokes column-level grants.
        REVOKE ALL ON recordings FROM masakhane_qc_writer;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'masakhane_supervision_ro') THEN
        GRANT SELECT ON qc_human_reviews, qc_results TO masakhane_supervision_ro;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sc_readonly') THEN
        GRANT SELECT ON qc_human_reviews, qc_results TO sc_readonly;
    END IF;
END
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'qc_human_reviews',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('recording_id', sa.BigInteger(), nullable=False),
        sa.Column('decision', sa.Text(), nullable=False),
        sa.Column('reason_code', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('reviewer_id', sa.Text(), nullable=False),
        sa.Column('reviewer_name', sa.Text(), nullable=False),
        sa.Column('operation_key', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['recording_id'], ['recordings.id'], name='fk_qc_human_reviews_recording_id', ondelete='RESTRICT'
        ),
        sa.UniqueConstraint('operation_key', name='uq_qc_human_reviews_operation_key'),
        sa.CheckConstraint("decision IN ('accept', 'reject')", name='ck_qc_human_reviews_decision'),
        sa.CheckConstraint(
            'reason_code IN (' + ', '.join(f"'{r}'" for r in REASONS) + ')', name='ck_qc_human_reviews_reason_code'
        ),
        sa.CheckConstraint(
            "(decision = 'reject') = (reason_code IS NOT NULL)", name='ck_qc_human_reviews_reason_when_rejected'
        ),
        sa.CheckConstraint(
            "reason_code IS DISTINCT FROM 'other' OR btrim(coalesce(note, '')) <> ''",
            name='ck_qc_human_reviews_note_for_other',
        ),
        sa.CheckConstraint('note IS NULL OR char_length(note) <= 500', name='ck_qc_human_reviews_note_length'),
        sa.CheckConstraint(
            "btrim(reviewer_id) <> '' AND btrim(reviewer_name) <> ''", name='ck_qc_human_reviews_reviewer'
        ),
    )
    op.create_index(
        'ix_qc_human_reviews_recording_created_id', 'qc_human_reviews', ['recording_id', 'created_at', 'id']
    )

    op.add_column('recordings', sa.Column('qc_tier3', sa.Text(), nullable=True))
    op.add_column('recordings', sa.Column('qc_tier3_reason', sa.Text(), nullable=True))
    op.add_column('recordings', sa.Column('qc_final', sa.Text(), nullable=True))
    op.create_check_constraint('ck_recordings_qc_tier3', 'recordings', "qc_tier3 IN ('accept', 'reject')")
    op.create_check_constraint(
        'ck_recordings_qc_final', 'recordings', "qc_final IN ('bronze', 'silver', 'gold', 'rejected')"
    )

    op.execute(QC_FINAL_LABEL)
    # Backfill every row (before the trigger exists, so updated_at stays as it was).
    op.execute(
        """
        UPDATE recordings r
        SET qc_final = qc_final_label(
            r.qc_status, r.qc_tier2, r.qc_tier3,
            (SELECT a.prompt_type FROM assignments a WHERE a.id = r.assignment_id)
        )
        """
    )
    op.alter_column('recordings', 'qc_final', nullable=False)

    op.execute(RECORDINGS_TRIGGER)
    op.execute(APPLY_REVIEW_TRIGGER)
    op.execute(APPEND_ONLY_TRIGGER)
    op.execute(GRANTS)


def downgrade() -> None:
    """Downgrade schema: removes exactly what upgrade added (decisions in qc_human_reviews are lost)."""
    op.execute('DROP TRIGGER IF EXISTS trg_qc_human_reviews_append_only ON qc_human_reviews')
    op.execute('DROP TRIGGER IF EXISTS trg_qc_human_reviews_apply ON qc_human_reviews')
    op.execute('DROP TRIGGER IF EXISTS trg_recordings_qc_final ON recordings')
    op.execute('DROP FUNCTION IF EXISTS qc_human_reviews_append_only()')
    op.execute('DROP FUNCTION IF EXISTS qc_human_reviews_apply()')
    op.execute('DROP FUNCTION IF EXISTS recordings_set_qc_final()')
    op.drop_index('ix_qc_human_reviews_recording_created_id', table_name='qc_human_reviews')
    op.drop_table('qc_human_reviews')
    op.drop_constraint('ck_recordings_qc_final', 'recordings', type_='check')
    op.drop_constraint('ck_recordings_qc_tier3', 'recordings', type_='check')
    op.drop_column('recordings', 'qc_final')
    op.drop_column('recordings', 'qc_tier3_reason')
    op.drop_column('recordings', 'qc_tier3')
    op.execute('DROP FUNCTION IF EXISTS qc_final_label(text, smallint, text, text)')
