import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.storage import db
from src.storage.models import Assignment, InboundMessage, Participant, Prompt, Recording
from src.tasks.bank import (
    ActiveSessionError,
    RetakeLimitError,
    assign_next_prompt,
    choose_next_prompt,
    choose_task_type,
    create_assignment,
    end_session,
    expire_stale_assignments,
    find_open_assignment,
    get_active_session,
    handle_response,
    mark_sent,
    minutes_recorded,
    prompt_coverage,
    record_response,
    session_type_target_reached,
    set_assignment_outcome,
    start_session,
)
from src.tasks.config import CollectionConfig, get_collection_config
from tests.conftest import add_participant

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def config(**overrides: object) -> CollectionConfig:
    values = {
        "speakers_per_prompt": {"sentence": 2, "image": 2, "conversation_starter": 2},
        "session_size": 10,
        "assignment_expiry_hours": 24,
        "max_retakes_per_prompt": 2,
        "targets_minutes": {"total": 10, "sentence": 4, "image": 3, "conversation_starter": 3},
        "clip_seconds": {"min": 5, "max": 30},
    }
    values.update(overrides)
    return CollectionConfig.model_validate(values)


# =======
# HELPERS
# =======
@pytest.fixture
def dbs(clean_db: None) -> Session:
    with db.get_sync_sessionmaker()() as session:
        yield session


def people(*ids: str) -> list[str]:
    for n, participant_id in enumerate(ids, start=1):
        add_participant(participant_id, f"+26588800{n:04d}")
    return list(ids)


def add_prompts(session: Session, prompt_type: str, count: int, flags: dict | None = None, start: int = 1) -> list[str]:
    prefix = {"sentence": "S", "image": "I", "conversation_starter": "C"}[prompt_type]
    ids = []
    for n in range(start, start + count):
        prompt_id = f"{prefix}{n:05d}"
        text = None if prompt_type == "image" else f"Prompt text number {n} for testing here."
        session.add(
            Prompt(
                prompt_id=prompt_id,
                prompt_type=prompt_type,
                text=text,
                normalized_text=text.lower() if text else None,
                flags=flags or {},
                source_file="test.csv",
            )
        )
        ids.append(prompt_id)
    session.commit()
    return ids


def assign(session: Session, participant_id: str, task_type: str, cfg: CollectionConfig, now: datetime = T0) -> Assignment | None:
    from src.tasks.bank import get_active_session

    task_session = get_active_session(session, participant_id) or start_session(
        session, participant_id, task_type, now, cfg
    )
    assignment = assign_next_prompt(session, task_session, now, cfg)
    if assignment is not None:
        mark_sent(session, assignment.id, f"SM{assignment.id:032d}", now, cfg)
    session.commit()
    return assignment


def recording(session: Session, participant_id: str, seconds: float, duplicate_of: int | None = None) -> int:
    count = session.scalar(select(func.count(InboundMessage.id)))
    message = InboundMessage(
        message_sid=f"MMREC{count:027d}",
        sender="+265888000000",
        message_type="audio",
        raw_payload={},
        status="processed",
        participant_id=participant_id,
    )
    session.add(message)
    session.flush()
    rec = Recording(
        inbound_message_id=message.id,
        participant_id=participant_id,
        status="stored",
        duration_s=seconds,
        duplicate_of_recording_id=duplicate_of,
    )
    session.add(rec)
    session.commit()
    return rec.id


def answer(session: Session, assignment: Assignment, seconds: float = 30.0, now: datetime = T0) -> int:
    rec_id = recording(session, assignment.participant_id, seconds)
    record_response(session, assignment.id, rec_id, now)
    session.commit()
    return rec_id


# ================
# CHOOSING PROMPTS
# ================
def test_no_prompt_is_ever_repeated_for_a_participant(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 3)
    cfg = config(speakers_per_prompt={"sentence": 5, "image": 1, "conversation_starter": 1})

    got = [assign(dbs, p1, "sentence", cfg) for _ in range(3)]
    assert sorted(a.prompt_id for a in got) == ["S00001", "S00002", "S00003"]
    assert assign(dbs, p1, "sentence", cfg) is None

    # Even after their assignments expire or are rejected, they never get one again.
    answer(dbs, got[0])
    set_assignment_outcome(dbs, got[0].id, accepted=False)
    dbs.commit()
    assert choose_next_prompt(dbs, p1, "sentence", T0 + timedelta(days=5), cfg) is None


def test_coverage_stops_at_speakers_per_prompt(dbs: Session) -> None:
    p1, p2, p3 = people("P0001", "P0002", "P0003")
    add_prompts(dbs, "sentence", 1)
    cfg = config()  # 2 speakers per sentence

    assert assign(dbs, p1, "sentence", cfg).prompt_id == "S00001"
    assert assign(dbs, p2, "sentence", cfg).prompt_id == "S00001"
    assert assign(dbs, p3, "sentence", cfg) is None
    assert prompt_coverage(dbs, "S00001", T0) == 2


def test_pending_sent_assignments_count_toward_coverage(dbs: Session) -> None:
    p1, p2 = people("P0001", "P0002")
    add_prompts(dbs, "sentence", 1)
    cfg = config(speakers_per_prompt={"sentence": 1, "image": 1, "conversation_starter": 1})

    pending = assign(dbs, p1, "sentence", cfg)  # sent, not answered

    assert pending.status == "sent"
    assert choose_next_prompt(dbs, p2, "sentence", T0 + timedelta(hours=1), cfg) is None


def test_expiry_frees_the_slot(dbs: Session) -> None:
    p1, p2 = people("P0001", "P0002")
    add_prompts(dbs, "sentence", 1)
    cfg = config(speakers_per_prompt={"sentence": 1, "image": 1, "conversation_starter": 1})
    first = assign(dbs, p1, "sentence", cfg)

    assert choose_next_prompt(dbs, p2, "sentence", T0 + timedelta(hours=23), cfg) is None
    dbs.rollback()
    later = T0 + timedelta(hours=24, minutes=1)
    prompt = choose_next_prompt(dbs, p2, "sentence", later, cfg)

    assert prompt is not None and prompt.prompt_id == "S00001"
    dbs.refresh(first)
    assert first.status == "expired"
    dbs.commit()
    assert expire_stale_assignments(dbs, later) == 0  # already done


def test_mark_sent_restarts_expiry_clock(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 1)
    cfg = config()
    task_session = start_session(dbs, p1, "sentence", T0, cfg)
    assignment = assign_next_prompt(dbs, task_session, T0, cfg)

    mark_sent(dbs, assignment.id, "SM_late", T0 + timedelta(hours=2), cfg)

    assert assignment.expires_at == T0 + timedelta(hours=26)
    assert assignment.sent_at == T0 + timedelta(hours=2)


def test_rejection_frees_the_slot_acceptance_does_not(dbs: Session) -> None:
    p1, p2, p3 = people("P0001", "P0002", "P0003")
    add_prompts(dbs, "sentence", 1)
    cfg = config(speakers_per_prompt={"sentence": 1, "image": 1, "conversation_starter": 1})
    first = assign(dbs, p1, "sentence", cfg)
    answer(dbs, first)

    set_assignment_outcome(dbs, first.id, accepted=False)
    dbs.commit()
    second = assign(dbs, p2, "sentence", cfg)
    assert second is not None and second.prompt_id == "S00001"

    answer(dbs, second)
    set_assignment_outcome(dbs, second.id, accepted=True)
    dbs.commit()
    assert assign(dbs, p3, "sentence", cfg) is None


def test_length_flagged_prompts_chosen_last_but_coverage_first(dbs: Session) -> None:
    p1, p2, p3 = people("P0001", "P0002", "P0003")
    add_prompts(dbs, "sentence", 3, flags={"length": "short"}, start=1)
    add_prompts(dbs, "sentence", 1, flags={"joined_lines": True}, start=4)  # not a length flag
    cfg = config(speakers_per_prompt={"sentence": 3, "image": 1, "conversation_starter": 1})

    # Equal coverage: the prompt without a length flag comes first.
    assert assign(dbs, p1, "sentence", cfg).prompt_id == "S00004"
    # S00004 now has coverage 1, so the uncovered flagged prompts come before it.
    second = assign(dbs, p2, "sentence", cfg)
    third = assign(dbs, p3, "sentence", cfg)
    flagged = {"S00001", "S00002", "S00003"}
    assert second.prompt_id in flagged and third.prompt_id in flagged - {second.prompt_id}


def test_assignment_snapshots_text_and_images_have_none(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 1)
    add_prompts(dbs, "image", 1)
    cfg = config()

    sentence = assign(dbs, p1, "sentence", cfg)
    end_session(dbs, sentence.session_id, "completed", T0)
    dbs.commit()
    image = assign(dbs, p1, "image", cfg)

    assert sentence.prompt_text_snapshot == "Prompt text number 1 for testing here."
    assert image.prompt_text_snapshot is None
    assert (sentence.position, image.position) == (1, 1)


def test_one_active_session_and_positions(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 5)
    cfg = config(session_size=2)
    task_session = start_session(dbs, p1, "sentence", T0, cfg)

    with pytest.raises(ActiveSessionError):
        start_session(dbs, p1, "image", T0, cfg)
    first = assign_next_prompt(dbs, task_session, T0, cfg)
    second = assign_next_prompt(dbs, task_session, T0, cfg)
    assert (first.position, second.position) == (1, 2)
    assert assign_next_prompt(dbs, task_session, T0, cfg) is None  # session full


# =======
# ANSWERS
# =======
def test_reply_to_binding_picks_the_right_assignment(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 3)
    cfg = config()
    task_session = start_session(dbs, p1, "sentence", T0, cfg)
    assignments = []
    for minute in range(3):
        a = assign_next_prompt(dbs, task_session, T0, cfg)
        mark_sent(dbs, a.id, f"SM_prompt_{minute}", T0 + timedelta(minutes=minute), cfg)
        assignments.append(a)
    dbs.commit()
    first, second, third = assignments

    assert find_open_assignment(dbs, p1, "SM_prompt_0").id == first.id
    assert find_open_assignment(dbs, p1).id == third.id  # latest sent
    assert find_open_assignment(dbs, p1, "SM_unknown").id == third.id

    answer(dbs, third)
    assert find_open_assignment(dbs, p1).id == second.id
    # A reply to an old prompt binds even after it expired.
    expire_stale_assignments(dbs, T0 + timedelta(days=2))
    dbs.commit()
    old = find_open_assignment(dbs, p1, "SM_prompt_0")
    assert old.id == first.id and old.status == "expired"
    assert find_open_assignment(dbs, p1) is None  # nothing open any more

    end_session(dbs, task_session.id, "completed", T0)
    dbs.commit()
    assert find_open_assignment(dbs, p1) is None
    assert find_open_assignment(dbs, p1, "SM_prompt_1").id == second.id


def test_record_response_links_both_ways_and_retakes(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 1)
    cfg = config(max_retakes_per_prompt=1)
    a = assign(dbs, p1, "sentence", cfg)

    first_rec = answer(dbs, a, seconds=10)
    assert a.status == "recorded" and a.recording_id == first_rec and a.answered_at == T0
    assert dbs.get(Recording, first_rec).assignment_id == a.id

    retake = recording(dbs, p1, 20)
    record_response(dbs, a.id, retake, T0, cfg)
    dbs.commit()
    assert a.retake_count == 1 and a.recording_id == retake
    with pytest.raises(RetakeLimitError):
        record_response(dbs, a.id, recording(dbs, p1, 25), T0, cfg)


def test_minutes_recorded_excludes_duplicates_rejected_and_old_takes(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 3)
    add_prompts(dbs, "image", 1)
    cfg = config()
    kept = assign(dbs, p1, "sentence", cfg)
    answer(dbs, kept, seconds=60)
    retaken = assign(dbs, p1, "sentence", cfg)
    answer(dbs, retaken, seconds=30)
    record_response(dbs, retaken.id, recording(dbs, p1, 90), T0, cfg)  # newest counts
    rejected = assign(dbs, p1, "sentence", cfg)
    answer(dbs, rejected, seconds=600)
    set_assignment_outcome(dbs, rejected.id, accepted=False)
    end_session(dbs, kept.session_id, "completed", T0)
    dbs.commit()
    image = assign(dbs, p1, "image", cfg)
    duplicate = recording(dbs, p1, 120, duplicate_of=1)
    record_response(dbs, image.id, duplicate, T0, cfg)
    dbs.commit()
    recording(dbs, p1, 300)  # free speech: no assignment

    assert minutes_recorded(dbs, p1) == {"sentence": 2.5, "image": 0.0, "conversation_starter": 0.0}


# ====================
# CHOOSING A TASK TYPE
# ====================
def _record_minutes(session: Session, participant_id: str, task_type: str, minutes: float, cfg: CollectionConfig) -> None:
    from src.tasks.bank import get_active_session

    a = assign(session, participant_id, task_type, cfg)
    answer(session, a, seconds=minutes * 60)
    end_session(session, get_active_session(session, participant_id).id, "completed", T0)
    session.commit()


def test_choose_task_type_follows_remaining_share(dbs: Session) -> None:
    (p1,) = people("P0001")
    for task_type in ("sentence", "image", "conversation_starter"):
        add_prompts(dbs, task_type, 5)
    cfg = config(speakers_per_prompt={"sentence": 5, "image": 5, "conversation_starter": 5})

    assert choose_task_type(dbs, p1, T0, cfg) == "sentence"  # all at 100%: config order
    _record_minutes(dbs, p1, "sentence", 3, cfg)  # sentence 25% left
    _record_minutes(dbs, p1, "image", 1, cfg)  # image 67% left; conversation 100%
    assert choose_task_type(dbs, p1, T0, cfg) == "conversation_starter"
    _record_minutes(dbs, p1, "conversation_starter", 2.5, cfg)  # 17% left
    assert choose_task_type(dbs, p1, T0, cfg) == "image"


def test_choose_task_type_skips_exhausted_types_and_stops_at_targets(dbs: Session) -> None:
    p1, p2 = people("P0001", "P0002")
    add_prompts(dbs, "sentence", 3)
    add_prompts(dbs, "image", 1)  # no conversation starters at all
    cfg = config(speakers_per_prompt={"sentence": 5, "image": 1, "conversation_starter": 5})
    assign(dbs, p2, "image", cfg)  # p2 holds the only image slot

    assert choose_task_type(dbs, p1, T0, cfg) == "sentence"  # image, conversation exhausted
    _record_minutes(dbs, p1, "sentence", 4, cfg)
    assert choose_task_type(dbs, p1, T0, cfg) is None  # nothing left that's available


def test_total_target_is_not_a_stopping_rule(dbs: Session) -> None:
    (p1,) = people("P0001")
    for task_type in ("sentence", "image", "conversation_starter"):
        add_prompts(dbs, task_type, 5)
    cfg = config(
        speakers_per_prompt={"sentence": 5, "image": 5, "conversation_starter": 5},
        targets_minutes={"total": 2, "sentence": 4, "image": 1, "conversation_starter": 1},
    )
    _record_minutes(dbs, p1, "sentence", 3, cfg)
    # 3 of 2 total minutes, but every type is still short: keep going.
    assert choose_task_type(dbs, p1, T0, cfg) == "image"
    _record_minutes(dbs, p1, "image", 1, cfg)
    _record_minutes(dbs, p1, "conversation_starter", 1, cfg)
    assert choose_task_type(dbs, p1, T0, cfg) == "sentence"
    _record_minutes(dbs, p1, "sentence", 1.5, cfg)
    # Every type met (total 6.5 of 2): now it stops.
    assert choose_task_type(dbs, p1, T0, cfg) is None
    assert sum(minutes_recorded(dbs, p1).values()) == 6.5


def test_overrides_and_inactive_participants(dbs: Session) -> None:
    p1, p2 = people("P0001", "P0002")
    for task_type in ("sentence", "image", "conversation_starter"):
        add_prompts(dbs, task_type, 5)
    cfg = config(speakers_per_prompt={"sentence": 5, "image": 5, "conversation_starter": 5})

    participant = dbs.scalar(select(Participant).where(Participant.participant_id == p1))
    participant.attributes = {"targets_minutes": {"sentence": 0, "image": 0}}
    dbs.commit()
    assert choose_task_type(dbs, p1, T0, cfg) == "conversation_starter"

    participant = dbs.scalar(select(Participant).where(Participant.participant_id == p2))
    participant.status = "withdrawn"
    dbs.commit()
    assert choose_task_type(dbs, p2, T0, cfg) is None


# =================
# EARLY SESSION END
# =================
def _session_with_prompts(dbs: Session, participant_id: str, task_type: str, cfg: CollectionConfig):
    task_session = start_session(dbs, participant_id, task_type, T0, cfg)
    dbs.commit()
    return task_session


def _send(dbs: Session, task_session, cfg: CollectionConfig) -> Assignment:
    a = assign_next_prompt(dbs, task_session, T0, cfg)
    mark_sent(dbs, a.id, f"SM{a.id:032d}", T0, cfg)
    dbs.commit()
    return a


def test_session_ends_early_when_type_target_met(dbs: Session) -> None:
    (p1,) = people("P0001")
    for task_type in ("sentence", "image", "conversation_starter"):
        add_prompts(dbs, task_type, 10)
    cfg = config(
        speakers_per_prompt={"sentence": 5, "image": 5, "conversation_starter": 5},
        targets_minutes={"total": 10, "sentence": 1, "image": 2, "conversation_starter": 0.5},
    )
    task_session = _session_with_prompts(dbs, p1, "sentence", cfg)

    first = _send(dbs, task_session, cfg)
    result = handle_response(dbs, first.id, recording(dbs, p1, 40), T0, cfg)
    dbs.commit()
    assert not result.session_ended and result.next_task_type is None
    assert not session_type_target_reached(dbs, p1, task_session.id, cfg)

    second = _send(dbs, task_session, cfg)
    result = handle_response(dbs, second.id, recording(dbs, p1, 25), T0, cfg)  # 65 s >= 1 min
    dbs.commit()

    assert result.session_ended and result.assignment.status == "recorded"
    dbs.refresh(task_session)
    assert task_session.status == "completed" and task_session.ended_at == T0
    assert get_active_session(dbs, p1) is None
    # Image and conversation both have 100% of their target left; config order decides.
    assert result.next_task_type == "image"
    count = dbs.scalar(select(func.count(Assignment.id)).where(Assignment.session_id == task_session.id))
    assert count == 2  # stopped well short of session_size (10)


def test_early_end_returns_none_when_everything_is_met(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 3)
    cfg = config(targets_minutes={"total": 10, "sentence": 0.5, "image": 0, "conversation_starter": 0})
    task_session = _session_with_prompts(dbs, p1, "sentence", cfg)
    a = _send(dbs, task_session, cfg)

    result = handle_response(dbs, a.id, recording(dbs, p1, 30), T0, cfg)

    assert result.session_ended and result.next_task_type is None
    assert choose_task_type(dbs, p1, T0, cfg) is None


def test_session_runs_on_while_target_not_met(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 5)
    cfg = config(session_size=3, targets_minutes={"total": 10, "sentence": 4, "image": 1, "conversation_starter": 1})
    task_session = _session_with_prompts(dbs, p1, "sentence", cfg)

    for _ in range(3):
        a = _send(dbs, task_session, cfg)
        assert not handle_response(dbs, a.id, recording(dbs, p1, 20), T0, cfg).session_ended
        dbs.commit()

    assert assign_next_prompt(dbs, task_session, T0, cfg) is None  # full at session_size
    assert get_active_session(dbs, p1).id == task_session.id  # the engine ends it


def test_late_answer_to_an_ended_session_does_not_end_anything(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 5)
    add_prompts(dbs, "image", 5)
    cfg = config(targets_minutes={"total": 10, "sentence": 0.5, "image": 1, "conversation_starter": 0})
    task_session = _session_with_prompts(dbs, p1, "sentence", cfg)
    ignored = _send(dbs, task_session, cfg)
    answered = _send(dbs, task_session, cfg)
    assert handle_response(dbs, answered.id, recording(dbs, p1, 30), T0, cfg).session_ended
    dbs.commit()
    image_session = start_session(dbs, p1, "image", T0, cfg)
    dbs.commit()

    # A reply to the ignored prompt from the finished session arrives later.
    late = handle_response(dbs, ignored.id, recording(dbs, p1, 10), T0, cfg)

    assert not late.session_ended and late.assignment.status == "recorded"
    dbs.refresh(image_session)
    assert image_session.status == "active"


def test_session_type_target_reached_uses_overrides(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 3)
    cfg = config()
    participant = dbs.scalar(select(Participant).where(Participant.participant_id == p1))
    participant.attributes = {"targets_minutes": {"sentence": 0.25}}
    dbs.commit()
    task_session = _session_with_prompts(dbs, p1, "sentence", cfg)
    a = _send(dbs, task_session, cfg)

    assert handle_response(dbs, a.id, recording(dbs, p1, 15), T0, cfg).session_ended


# ===========
# CONCURRENCY
# ===========
def test_two_sessions_one_slot_left(dbs: Session) -> None:
    p1, p2, p3 = people("P0001", "P0002", "P0003")
    add_prompts(dbs, "sentence", 1)
    cfg = config()  # 2 speakers: after p1, one slot is left
    assign(dbs, p1, "sentence", cfg)
    factory = db.get_sync_sessionmaker()

    with factory() as first, factory() as second:
        chosen = choose_next_prompt(first, p2, "sentence", T0, cfg)
        assert chosen is not None  # first now holds the row lock
        # The second transaction skips the locked row instead of taking the slot.
        assert choose_next_prompt(second, p3, "sentence", T0, cfg) is None
        second.rollback()

        task_session = start_session(first, p2, "sentence", T0, cfg)
        create_assignment(first, p2, chosen, task_session, 1, T0, cfg)
        first.commit()

        # Once committed, the prompt is full for everyone.
        assert choose_next_prompt(second, p3, "sentence", T0, cfg) is None
    assert prompt_coverage(dbs, "S00001", T0) == 2


def test_waiting_transaction_recounts_after_lock_released(dbs: Session) -> None:
    """A transaction that saw the last slot as free before another committed
    must not take it: the recount under the lock catches it."""
    p1, p2, p3 = people("P0001", "P0002", "P0003")
    add_prompts(dbs, "sentence", 1)
    cfg = config()
    assign(dbs, p1, "sentence", cfg)
    factory = db.get_sync_sessionmaker()
    results: dict[str, object] = {}

    with factory() as first:
        chosen = choose_next_prompt(first, p2, "sentence", T0, cfg)
        task_session = start_session(first, p2, "sentence", T0, cfg)
        create_assignment(first, p2, chosen, task_session, 1, T0, cfg)

        def other() -> None:
            with factory() as second:
                # Without SKIP LOCKED this would wait; with it, it skips. Lock
                # the row explicitly to force the wait-then-recount path.
                second.execute(select(Prompt).where(Prompt.prompt_id == "S00001").with_for_update())
                results["coverage"] = prompt_coverage(second, "S00001", T0)
                results["choice"] = choose_next_prompt(second, p3, "sentence", T0, cfg)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()  # blocked on first's lock
        first.commit()
    thread.join(timeout=10)

    assert results["coverage"] == 2 and results["choice"] is None


def test_config_file_loads() -> None:
    real = get_collection_config()
    assert set(real.speakers_per_prompt) == {"sentence", "image", "conversation_starter"}
    assert real.targets_for({"targets_minutes": {"image": 9}})["image"] == 9


def test_minutes_never_count_rejected_clips(dbs: Session) -> None:
    (p1,) = people("P0001")
    add_prompts(dbs, "sentence", 2)
    cfg = config()
    good = assign(dbs, p1, "sentence", cfg)
    answer(dbs, good, seconds=30)
    bad = assign(dbs, p1, "sentence", cfg)
    rec_id = answer(dbs, bad, seconds=90)
    # Even if a rejected clip ends up as the assignment's current recording.
    dbs.get(Recording, rec_id).qc_status = "rejected"
    dbs.commit()

    assert minutes_recorded(dbs, p1)["sentence"] == 0.5
