"""Add study enrollments, receipt-time activation history and main_recordings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "6ca8042a93f1"
down_revision = "e2a6c4f8b913"
branch_labels = None
depends_on = None


# =======
# UPGRADE
# =======
def upgrade() -> None:
    op.create_table(
        "enrollments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("participant_id", sa.Text(), sa.ForeignKey("participants.participant_id"), nullable=False),
        sa.Column("study", sa.Text(), nullable=False),
        sa.Column("round", sa.Text(), nullable=False),
        sa.Column("target_minutes", sa.Integer()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attributes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("starts_on", sa.Date()),
        sa.Column("ends_on", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("participant_id", "study", "round", name="uq_enrollments_study_round"),
        sa.CheckConstraint("study IN ('main', 'qc_calibration')", name="ck_enrollments_study"),
        sa.CheckConstraint("status IN ('active', 'paused', 'completed', 'withdrawn')", name="ck_enrollments_status"),
        sa.CheckConstraint("target_minutes >= 0", name="ck_enrollments_target_minutes"),
    )
    op.create_index("uq_enrollments_active", "enrollments", ["participant_id"],
                    unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table(
        "enrollment_activations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("enrollment_id", sa.BigInteger(), sa.ForeignKey("enrollments.id"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_enrollment_activations_enrollment_id", "enrollment_activations", ["enrollment_id"])
    op.add_column("participants", sa.Column("lifetime_cap_minutes", sa.Integer()))
    for name in ("sessions", "recordings"):
        op.add_column(name, sa.Column("enrollment_id", sa.BigInteger()))
        op.create_foreign_key(f"fk_{name}_enrollment_id", name, "enrollments", ["enrollment_id"], ["id"])
        op.create_index(f"ix_{name}_enrollment_id", name, ["enrollment_id"])

    op.execute("""
        INSERT INTO enrollments (participant_id, study, round, status, starts_on)
        SELECT participant_id, 'main', 'pilot',
               CASE WHEN status = 'inactive' THEN 'paused' ELSE status END, enrolled_on
        FROM participants
    """)
    op.execute("""
        INSERT INTO enrollment_activations (enrollment_id, activated_at)
        SELECT id, 'epoch'::timestamptz FROM enrollments WHERE status = 'active'
    """)
    for name in ("sessions", "recordings"):
        op.execute(f"""
            UPDATE {name} AS item SET enrollment_id = e.id
            FROM enrollments AS e WHERE item.participant_id = e.participant_id
        """)
    op.execute("UPDATE participants SET lifetime_cap_minutes = 80")
    op.execute("""
        CREATE FUNCTION track_enrollment_activation() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE changed_at timestamptz := COALESCE(
            NULLIF(current_setting('collector.enrollment_transition_at', true), '')::timestamptz,
            clock_timestamp()
        );
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.status = OLD.status THEN
                    RETURN NEW;
                END IF;
                IF OLD.status = 'active' THEN
                    UPDATE enrollment_activations SET deactivated_at = changed_at
                    WHERE enrollment_id = OLD.id AND deactivated_at IS NULL;
                END IF;
            END IF;
            IF NEW.status = 'active' THEN
                INSERT INTO enrollment_activations (enrollment_id, activated_at)
                VALUES (NEW.id, changed_at);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER enrollment_activation_changed
        AFTER INSERT OR UPDATE OF status ON enrollments
        FOR EACH ROW EXECUTE FUNCTION track_enrollment_activation();
    """)
    op.execute("""
        CREATE VIEW main_recordings AS
        SELECT r.* FROM recordings r LEFT JOIN enrollments e ON e.id = r.enrollment_id
        WHERE r.enrollment_id IS NULL OR e.study = 'main'
    """)

    # Keep production readers on the filtered view if they already had recording access.
    op.execute("""
        DO $$
        DECLARE reader text;
        BEGIN
            FOR reader IN SELECT rolname FROM pg_roles
                WHERE rolname IN ('masakhane_supervision_ro', 'sc_readonly')
                  AND has_table_privilege(oid, 'recordings', 'SELECT')
            LOOP
                EXECUTE format('GRANT SELECT ON main_recordings TO %I', reader);
            END LOOP;
        END $$;
    """)


# =========
# DOWNGRADE
# =========
def downgrade() -> None:
    op.execute("DROP VIEW main_recordings")
    op.execute("DROP TRIGGER enrollment_activation_changed ON enrollments")
    op.execute("DROP FUNCTION track_enrollment_activation()")
    for name in ("recordings", "sessions"):
        op.drop_index(f"ix_{name}_enrollment_id", table_name=name)
        op.drop_constraint(f"fk_{name}_enrollment_id", name, type_="foreignkey")
        op.drop_column(name, "enrollment_id")
    op.drop_column("participants", "lifetime_cap_minutes")
    op.drop_table("enrollment_activations")
    op.drop_table("enrollments")
