"""Tier 3 (qc_human_reviews) and the final label (recordings.qc_final): the database
functions and triggers of migration d9f4b2c8e631, as the supervision gateway will use them."""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.storage import db
from src.storage.models import (
    Assignment,
    InboundMessage,
    QcHumanReview,
    Recording,
    TaskSession,
)
from tests.conftest import add_participant

T0 = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
CAS_CONFLICT = "QC409"  # the documented SQLSTATE for a tier-3 state mismatch
APPEND_ONLY = "QC405"


# =======
# HELPERS
# =======
@pytest.fixture
def person(clean_db: None) -> str:
    add_participant("P0001", "+265991000001")
    return "P0001"


def recording(task_type: str | None = "sentence", qc_status: str = "accepted", qc_tier2: int | None = None) -> int:
    """A stored recording; task_type None: unassigned."""
    with db.get_sync_sessionmaker()() as session:
        n = session.query(InboundMessage).count() + 1
        inbound = InboundMessage(message_sid=f"wamid.H{n}", sender="+265991000001", message_type="audio",
                                 raw_payload={}, status="processed")  # fmt: skip
        session.add(inbound)
        session.flush()
        assignment_id = None
        if task_type is not None:
            task_session = TaskSession(participant_id="P0001", task_type=task_type, status="completed", size=10,
                                       started_at=T0, ended_at=T0)  # fmt: skip
            session.add(task_session)
            session.flush()
            assignment = Assignment(participant_id="P0001", prompt_type=task_type, session_id=task_session.id,
                                    position=1, status="recorded", expires_at=T0 + timedelta(days=1))  # fmt: skip
            session.add(assignment)
            session.flush()
            assignment_id = assignment.id
        row = Recording(inbound_message_id=inbound.id, participant_id="P0001", assignment_id=assignment_id,
                        status="stored", qc_status=qc_status, qc_tier2=qc_tier2, duration_s=6.0)  # fmt: skip
        session.add(row)
        session.commit()
        return row.id


def state(recording_id: int) -> Recording:
    with db.get_sync_sessionmaker()() as session:
        return session.get(Recording, recording_id)


def review(
    recording_id: int,
    decision: str = "accept",
    reason_code: str | None = None,
    note: str | None = None,
    expected: str | None = None,
    key: uuid.UUID | None = None,
    connection=None,
) -> None:
    """Insert one decision as the gateway does: optional expected state, then the INSERT."""
    values = {"rid": recording_id, "decision": decision, "reason": reason_code, "note": note,
              "key": key or uuid.uuid4()}  # fmt: skip

    def run(conn) -> None:
        if expected is not None:
            conn.execute(text("SELECT set_config('masakhane.expected_qc_tier3', :v, true)"), {"v": expected})
        conn.execute(
            text(
                "INSERT INTO qc_human_reviews (recording_id, decision, reason_code, note, reviewer_id, reviewer_name,"
                " operation_key) VALUES (:rid, :decision, :reason, :note, '4', 'Supervisor Gloria', :key)"
            ),
            values,
        )

    if connection is not None:
        run(connection)
        return
    with db.get_sync_engine().begin() as conn:
        run(conn)


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def reviews(recording_id: int) -> list[QcHumanReview]:
    with db.get_sync_sessionmaker()() as session:
        return list(
            session.scalars(
                select(QcHumanReview)
                .where(QcHumanReview.recording_id == recording_id)
                .order_by(QcHumanReview.created_at, QcHumanReview.id)
            )
        )


# ===========
# FINAL LABEL
# ===========
@pytest.mark.parametrize(
    ("qc_status", "qc_tier2", "qc_tier3", "task_type", "label"),
    [
        # 1. The human decision wins, over everything.
        ("accepted", 4, "accept", "sentence", "gold"),
        ("rejected", None, "accept", "sentence", "gold"),  # overrides tier 1
        ("needs_review", 1, "accept", "image", "gold"),
        ("accepted", 4, "reject", "sentence", "rejected"),
        ("accepted", None, "reject", "free_speech", "rejected"),
        ("accepted", None, "accept", None, "gold"),
        # 2. Rejected on arrival.
        ("rejected", 4, None, "sentence", "rejected"),
        ("rejected", None, None, "image", "rejected"),
        # 3. Flagged on arrival or not checked yet: Bronze, even at tier-2 level 4.
        ("needs_review", 4, None, "sentence", "bronze"),
        ("needs_review", None, None, "free_speech", "bronze"),
        ("pending", 4, None, "sentence", "bronze"),
        # 4. Sentences: tier-2 level 3 or 4 -> Silver; 1, 2 or not scored -> Bronze.
        ("accepted", 4, None, "sentence", "silver"),
        ("accepted", 3, None, "sentence", "silver"),
        ("accepted", 2, None, "sentence", "bronze"),
        ("accepted", 1, None, "sentence", "bronze"),
        ("accepted", None, None, "sentence", "bronze"),
        # 5. Spontaneous types (tier 2 not applicable): Silver.
        ("accepted", None, None, "image", "silver"),
        ("accepted", None, None, "conversation_starter", "silver"),
        ("accepted", None, None, "free_speech", "silver"),
        # 6. Unassigned: Bronze.
        ("accepted", None, None, None, "bronze"),
        ("accepted", 4, None, None, "bronze"),
    ],
)
def test_final_label_rules(clean_db: None, qc_status: str, qc_tier2: int | None, qc_tier3: str | None,
                           task_type: str | None, label: str) -> None:  # fmt: skip
    with db.get_sync_engine().connect() as conn:
        result = conn.execute(
            text("SELECT qc_final_label(:s, CAST(:t2 AS smallint), :t3, :task)"),
            {"s": qc_status, "t2": qc_tier2, "t3": qc_tier3, "task": task_type},
        ).scalar()
    assert result == label


@pytest.mark.parametrize(
    ("task_type", "qc_status", "qc_tier2", "label"),
    [("sentence", "accepted", None, "bronze"), ("sentence", "accepted", 4, "silver"), ("image", "accepted", None, "silver"),
     ("free_speech", "needs_review", None, "bronze"), ("sentence", "rejected", None, "rejected"), (None, "accepted", None, "bronze")],
)  # fmt: skip
def test_new_recordings_get_a_final_label(person: str, task_type: str | None, qc_status: str, qc_tier2: int | None, label: str) -> None:
    assert state(recording(task_type, qc_status, qc_tier2)).qc_final == label


def test_tier1_and_tier2_changes_recompute_the_label(person: str) -> None:
    rid = recording("sentence", "pending")
    first = state(rid)
    assert first.qc_final == "bronze"
    with db.get_sync_sessionmaker()() as session:
        # Tier-2 scoring's targeted update (updated_at kept as it was): the trigger bumps it.
        session.execute(update(Recording).where(Recording.id == rid).values(qc_status="accepted", updated_at=Recording.updated_at))
        session.commit()
    assert state(rid).qc_final == "bronze"
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Recording).where(Recording.id == rid).values(qc_tier2=4, updated_at=Recording.updated_at))
        session.commit()
    after = state(rid)
    assert after.qc_final == "silver" and after.updated_at > first.updated_at
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Recording).where(Recording.id == rid).values(qc_status="needs_review", updated_at=Recording.updated_at))
        session.commit()
    assert state(rid).qc_final == "bronze" and state(rid).updated_at > after.updated_at  # qc_final changed


def test_linking_an_assignment_recomputes_the_label(person: str) -> None:
    unassigned = recording(None)
    other = recording("image")
    assert state(unassigned).qc_final == "bronze"
    with db.get_sync_sessionmaker()() as session:
        assignment_id = session.get(Recording, other).assignment_id
        session.execute(update(Recording).where(Recording.id == unassigned).values(assignment_id=assignment_id))
        session.commit()
    assert state(unassigned).qc_final == "silver"


# =========
# DECISIONS
# =========
def test_decisions_reversals_and_tier1_untouched(person: str) -> None:
    rid = recording("sentence", "accepted", qc_tier2=2)
    before = state(rid)
    assert (before.qc_tier3, before.qc_final) == (None, "bronze")

    review(rid, "accept", expected="__NULL__")
    accepted = state(rid)
    assert (accepted.qc_tier3, accepted.qc_tier3_reason, accepted.qc_final) == ("accept", None, "gold")
    assert accepted.updated_at > before.updated_at

    review(rid, "reject", "no_speech", expected="accept")
    rejected = state(rid)
    assert (rejected.qc_tier3, rejected.qc_tier3_reason, rejected.qc_final) == ("reject", "no_speech", "rejected")

    review(rid, "accept", expected="reject")  # reversal
    final = state(rid)
    assert (final.qc_tier3, final.qc_tier3_reason, final.qc_final) == ("accept", None, "gold")

    assert [r.decision for r in reviews(rid)] == ["accept", "reject", "accept"]  # history kept
    assert (final.qc_status, final.qc_reason, final.qc_tier2) == (before.qc_status, before.qc_reason, 2)


@pytest.mark.parametrize(
    ("decision", "reason_code", "note"),
    [
        ("reject", None, None),  # rejection without a reason
        ("reject", "other", None),  # "other" without a note
        ("reject", "other", "   "),
        ("reject", "too_quiet", None),  # unknown reason
        ("approve", None, None),  # unknown decision
        ("accepted", None, None),
        ("accept", "noise", None),  # a reason on an acceptance
        ("reject", "noise", "x" * 501),  # note too long
    ],
)
def test_invalid_decisions_are_refused(person: str, decision: str, reason_code: str | None, note: str | None) -> None:
    rid = recording()
    with pytest.raises(IntegrityError):
        review(rid, decision, reason_code, note)
    assert reviews(rid) == [] and state(rid).qc_tier3 is None


def test_every_reason_and_an_explained_other_are_accepted(person: str) -> None:
    rid = recording()
    for reason in ("wrong_sentence", "no_speech", "noise", "unclear", "not_own_voice", "not_chichewa"):
        review(rid, "reject", reason)
    review(rid, "reject", "other", "Two people reading together")
    assert state(rid).qc_tier3_reason == "other" and len(reviews(rid)) == 7


def test_the_same_operation_key_twice_is_refused(person: str) -> None:
    rid = recording()
    key = uuid.uuid4()
    review(rid, "accept", key=key)
    with pytest.raises(IntegrityError, match="uq_qc_human_reviews_operation_key"):
        review(rid, "reject", "noise", key=key)
    assert state(rid).qc_tier3 == "accept"


def test_a_replay_with_on_conflict_do_nothing_changes_nothing(person: str) -> None:
    """The gateway's retry-safe insert: the duplicate row is skipped, and so is the trigger."""
    rid = recording()
    key = uuid.uuid4()
    review(rid, "accept", expected="__NULL__", key=key)
    stamp = state(rid).updated_at
    with db.get_sync_engine().begin() as conn:
        conn.execute(text("SELECT set_config('masakhane.expected_qc_tier3', 'accept', true)"))
        inserted = conn.execute(
            text(
                "INSERT INTO qc_human_reviews (recording_id, decision, reason_code, reviewer_id, reviewer_name,"
                " operation_key) VALUES (:rid, 'reject', 'noise', '4', 'Supervisor Gloria', :key)"
                " ON CONFLICT (operation_key) DO NOTHING RETURNING id"
            ),
            {"rid": rid, "key": key},
        ).first()
    assert inserted is None
    assert (state(rid).qc_tier3, state(rid).updated_at) == ("accept", stamp) and len(reviews(rid)) == 1


def test_a_decision_for_a_missing_recording_is_refused(person: str) -> None:
    with pytest.raises(IntegrityError):
        review(999_999)


# ===============
# COMPARE-AND-SET
# ===============
def test_compare_and_set(person: str) -> None:
    rid = recording()
    review(rid, "accept", expected="__NULL__")  # no decision yet: matches
    with pytest.raises(DBAPIError) as stale:
        review(rid, "reject", "noise", expected="__NULL__")  # it isn't empty any more
    assert sqlstate(stale.value) == CAS_CONFLICT and "changed" in str(stale.value.orig)
    with pytest.raises(DBAPIError) as wrong:
        review(rid, "reject", "noise", expected="reject")
    assert sqlstate(wrong.value) == CAS_CONFLICT
    review(rid, "reject", "noise", expected="accept")  # matches
    assert state(rid).qc_tier3 == "reject" and len(reviews(rid)) == 2  # the refused ones weren't saved
    review(rid, "accept")  # no expected value (e.g. an admin insert): no check
    assert state(rid).qc_tier3 == "accept"


def test_an_invalid_expected_value_is_refused(person: str) -> None:
    rid = recording()
    with pytest.raises(DBAPIError) as caught:
        review(rid, "accept", expected="NULL")
    assert sqlstate(caught.value) == "22023"  # invalid_parameter_value


def test_the_expected_value_only_lasts_for_its_transaction(person: str) -> None:
    rid = recording()
    with db.get_sync_engine().connect() as conn:
        with conn.begin():
            review(rid, "accept", expected="__NULL__", connection=conn)
        with conn.begin():  # same session, new transaction: the setting reads '' -> no check
            review(rid, "reject", "noise", connection=conn)
    assert state(rid).qc_tier3 == "reject"


def test_two_concurrent_decisions_on_one_recording_exactly_one_wins(person: str) -> None:
    rid = recording()
    outcome: dict[str, object] = {}

    def second() -> None:
        try:
            with db.get_sync_engine().connect() as conn, conn.begin():
                review(rid, "reject", "noise", expected="__NULL__", connection=conn)  # waits for the first
            outcome["second"] = "committed"
        except DBAPIError as exc:
            outcome["second"] = sqlstate(exc)

    with db.get_sync_engine().connect() as first:
        with first.begin():
            review(rid, "accept", expected="__NULL__", connection=first)  # holds the recording's lock
            thread = threading.Thread(target=second)
            thread.start()
            time.sleep(0.5)
            assert thread.is_alive()  # blocked on the lock
        thread.join(timeout=10)
    assert outcome == {"second": CAS_CONFLICT}
    assert [r.decision for r in reviews(rid)] == ["accept"] and state(rid).qc_tier3 == "accept"


# ===========
# APPEND-ONLY
# ===========
def test_reviews_cannot_be_updated_or_deleted(person: str) -> None:
    rid = recording()
    review(rid, "reject", "noise")
    for statement in (
        "UPDATE qc_human_reviews SET decision = 'accept', reason_code = NULL",
        "UPDATE qc_human_reviews SET note = 'edited'",
        "DELETE FROM qc_human_reviews",
    ):
        with pytest.raises(DBAPIError) as caught, db.get_sync_engine().begin() as conn:
            conn.execute(text(statement))
        assert sqlstate(caught.value) == APPEND_ONLY and "append-only" in str(caught.value.orig)
    assert [r.decision for r in reviews(rid)] == ["reject"] and state(rid).qc_tier3 == "reject"


def test_notified_at_can_be_set_once(person: str) -> None:
    rid = recording()
    review(rid, "reject", "unclear")
    with db.get_sync_engine().begin() as conn:
        conn.execute(text("UPDATE qc_human_reviews SET notified_at = now()"))
    for statement in ("UPDATE qc_human_reviews SET notified_at = now() + interval '1 day'",
                      "UPDATE qc_human_reviews SET notified_at = NULL",
                      "UPDATE qc_human_reviews SET notified_at = now(), note = 'x'"):  # fmt: skip
        with pytest.raises(DBAPIError) as caught, db.get_sync_engine().begin() as conn:
            conn.execute(text(statement))
        assert sqlstate(caught.value) == APPEND_ONLY
    assert reviews(rid)[0].notified_at is not None


# ======
# RIGHTS
# ======
WRITER = "qc_test_writer"


@pytest.fixture
def writer_role(person: str):
    """A role like the gateway's writer: INSERT and SELECT on qc_human_reviews, nothing on recordings."""
    engine = db.get_sync_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {WRITER}")) if conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": WRITER}
            ).first() else None
            conn.execute(text(f"DROP ROLE IF EXISTS {WRITER}"))
            conn.execute(text(f"CREATE ROLE {WRITER} NOLOGIN"))
            conn.execute(text(f"GRANT INSERT, SELECT ON qc_human_reviews TO {WRITER}"))
    except DBAPIError as exc:
        pytest.skip(f"can't create a test role here ({type(exc.orig).__name__})")
    yield WRITER
    with engine.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {WRITER}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {WRITER}"))


def test_a_writer_with_only_insert_rights_updates_recordings_through_the_trigger(writer_role: str) -> None:
    rid = recording("sentence", qc_tier2=1)
    with db.get_sync_engine().connect() as conn:
        with conn.begin():
            conn.execute(text(f"SET LOCAL ROLE {writer_role}"))
            # The gateway's own checks pass: it can read and insert, and can't touch recordings.
            rights = conn.execute(
                text(
                    "SELECT has_table_privilege(current_user, 'qc_human_reviews', 'INSERT'),"
                    " has_table_privilege(current_user, 'qc_human_reviews', 'UPDATE'),"
                    " has_any_column_privilege(current_user, 'recordings', 'SELECT'),"
                    " has_any_column_privilege(current_user, 'recordings', 'UPDATE')"
                )
            ).one()
            assert tuple(rights) == (True, False, False, False)
            review(rid, "accept", expected="__NULL__", connection=conn)
        with pytest.raises(DBAPIError) as refused, conn.begin():
            conn.execute(text(f"SET LOCAL ROLE {writer_role}"))
            conn.execute(text("UPDATE recordings SET qc_status = 'rejected' WHERE id = :rid"), {"rid": rid})
        assert sqlstate(refused.value) == "42501"  # insufficient_privilege
        with pytest.raises(DBAPIError), conn.begin():
            conn.execute(text(f"SET LOCAL ROLE {writer_role}"))
            conn.execute(text("UPDATE qc_human_reviews SET note = 'x'"))
    final = state(rid)
    assert (final.qc_tier3, final.qc_final, final.qc_status) == ("accept", "gold", "accepted")


def test_the_trigger_function_cannot_be_called_directly(writer_role: str) -> None:
    with pytest.raises(DBAPIError), db.get_sync_engine().begin() as conn:
        conn.execute(text(f"SET LOCAL ROLE {writer_role}"))
        conn.execute(text("SELECT qc_human_reviews_apply()"))
