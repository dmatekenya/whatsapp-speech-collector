"""Database contract for supervision calibration checks."""

from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.storage import db
from tests.conftest import REGISTERED_SENDER, add_participant

MIGRATION = import_module("migrations.versions.2f91b861dc04_calibration_review")
READERS = ("sc_readonly", "masakhane_supervision_ro")
WRITER = "masakhane_qc_writer"
COLUMNS = (
    "recording_id", "participant_id", "enrollment_id", "is_test", "condition_code", "condition_name", "task_type",
    "prompt_id", "attempt", "order_position", "duration_s", "active_seconds", "mean_dbfs", "wav_path", "created_at",
    "as_intended", "reviewer", "checked_at",
)
INSERT = text("""
    INSERT INTO calibration_clip_checks (recording_id, as_intended, note, reviewer, operation_key)
    VALUES (:recording_id, :choice, 'setup check', 'supervisor-1', :operation_key)
    RETURNING id
""")


def existing_acls(conn):
    return conn.execute(text("""
        SELECT c.relname, c.relacl::text, a.attname, a.attacl::text
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
        ORDER BY c.relname, a.attnum
    """)).all()


def execute_as(conn, role, sql, params=None):
    with conn.begin_nested():
        conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        rows = conn.execute(sql, params or {})
        result = rows.fetchall() if rows.returns_rows else None
        conn.exec_driver_sql("RESET ROLE")
        return result


def assert_refused(conn, role, statement, sqlstate, params=None):
    with pytest.raises(DBAPIError) as error:
        execute_as(conn, role, statement, params)
    assert error.value.orig.sqlstate == sqlstate


def test_calibration_review_contract_and_permissions(clean_db):
    add_participant("P0001", REGISTERED_SENDER, name="Private Name")
    engine = db.get_sync_engine()
    with engine.connect() as conn:
        # Role creation, prior grants, and the DDL round trip all roll back together.
        with conn.begin() as transaction:
            with Operations.context(MigrationContext.configure(conn)):
                MIGRATION.downgrade()
                exists = conn.scalar(text("SELECT to_regclass('public.calibration_clip_checks')"))
                assert exists is None
                if not conn.scalar(text("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")):
                    pytest.skip("The scratch database user cannot create fixture roles")
                for role in (*READERS, WRITER):
                    if conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role}):
                        pytest.skip("Run permission tests with a scratch cluster without production roles")
                    conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
                    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
                conn.exec_driver_sql(f"GRANT SELECT (participant_id) ON participants TO {READERS[0]}")
                conn.exec_driver_sql(f"GRANT SELECT, INSERT ON qc_human_reviews TO {WRITER}")
                before = existing_acls(conn)
                MIGRATION.upgrade()
                previous_relations = {row.relname for row in before}
                assert [row for row in existing_acls(conn) if row.relname in previous_relations] == before
                assert conn.scalar(text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :role"), {"role": WRITER}) is False

                conn.execute(text("UPDATE participants SET is_test = true WHERE participant_id = 'P0001'"))
                enrollment = conn.scalar(text("""
                    INSERT INTO enrollments (participant_id, study, round, status)
                    VALUES ('P0001', 'qc_calibration', 'fixture', 'paused') RETURNING id
                """))
                conn.execute(text("""
                    INSERT INTO inbound_messages (message_sid, sender, message_type, raw_payload)
                    VALUES ('study-clip', :phone, 'audio', '{}'), ('main-clip', :phone, 'audio', '{}')
                """), {"phone": REGISTERED_SENDER})
                conn.execute(text("""
                    INSERT INTO recordings (inbound_message_id, participant_id, duration_s,
                                            active_seconds, mean_dbfs, wav_path)
                    SELECT id, 'P0001', 12, 5, -21, 'P0001/fixture.wav' FROM inbound_messages
                """))
                ids = dict(conn.execute(text("""
                    SELECT i.message_sid, r.id FROM recordings r JOIN inbound_messages i ON i.id=r.inbound_message_id
                """)).all())
                rid = ids["study-clip"]
                conn.execute(text("""
                    INSERT INTO calibration_clips
                      (recording_id, enrollment_id, condition_code, task_type, task_position,
                       attempt, suggested_condition, order_position, fast_qc)
                    VALUES (:rid, :enrollment, 'C3', 'sentence', 3, 1, 'C4', 1, '{}')
                """), {"rid": rid, "enrollment": enrollment})

                projection = conn.execute(text("SELECT * FROM calibration_review_clips"))
                assert tuple(projection.keys()) == COLUMNS
                first = projection.mappings().one()
                assert first["as_intended"] is first["reviewer"] is first["checked_at"] is None
                key = uuid4()
                params = {"recording_id": rid, "choice": "unclear", "operation_key": key}
                assert execute_as(conn, WRITER, INSERT, params)
                assert_refused(conn, WRITER, INSERT, "23505", params)
                assert_refused(conn, WRITER, INSERT, "23503", {**params, "recording_id": ids["main-clip"],
                                                              "operation_key": uuid4()})
                assert_refused(conn, WRITER, INSERT, "23514", {**params, "choice": "maybe",
                                                              "operation_key": uuid4()})
                assert execute_as(conn, WRITER, INSERT, {**params, "choice": "yes", "operation_key": uuid4()})
                assert execute_as(conn, WRITER, text("SELECT count(*) FROM calibration_clip_checks")) == [(2,)]
                for role in READERS:
                    (row,) = execute_as(conn, role, text("SELECT * FROM calibration_review_clips"))
                    assert row.recording_id == rid and row.condition_name == "Arm's length"
                    assert row.is_test is True and row.wav_path == "P0001/fixture.wav"
                    assert row.as_intended == "yes" and row.reviewer == "supervisor-1" and row.checked_at
                    assert "Private Name" not in str(row) and REGISTERED_SENDER not in str(row)
                    assert_refused(conn, role, text("SELECT * FROM calibration_clip_checks"), "42501")
                assert_refused(conn, WRITER, text("SELECT * FROM calibration_review_clips"), "42501")
                for table, column in (
                    ("calibration_clip_checks", "note"), ("recordings", "qc_status"),
                    ("qc_human_reviews", "note"), ("participants", "name"),
                ):
                    for sql in (f"UPDATE {table} SET {column}=NULL", f"DELETE FROM {table}"):
                        assert_refused(conn, WRITER, text(sql), "42501")
                for sql in ("UPDATE calibration_clip_checks SET note=NULL", "DELETE FROM calibration_clip_checks"):
                    with pytest.raises(DBAPIError) as error, conn.begin_nested():
                        conn.execute(text(sql))
                    assert error.value.orig.sqlstate == "55000"
            transaction.rollback()
