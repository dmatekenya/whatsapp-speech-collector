"""scripts/reset_participant.py, on the test database with fake participants."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from scripts.reset_participant import main, parse_targets
from src.conversation.harness import ChatHarness
from src.conversation.states import INITIAL_STATE
from src.storage import db
from src.storage.models import Assignment, ConversationState, Participant, Prompt, Recording, TaskSession
from src.tasks.bank import minutes_recorded
from src.tasks.config import get_collection_config
from tests.conftest import add_participant

PHONE = "+265991230006"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def never_asked(prompt: str) -> str:
    raise AssertionError("should not ask for confirmation")


# ========
# FIXTURES
# ========
@pytest.fixture
def mid_session(clean_db: None) -> ChatHarness:
    """P0006 in a sentence session: sentence 1 recorded, sentence 2 open, plus an older paused session."""
    add_participant("P0006", PHONE, name="Chisomo Banda")
    with db.get_sync_sessionmaker()() as session:
        for n in range(1, 6):
            text = f"Sentence number {n} for reading aloud today."
            session.add(Prompt(prompt_id=f"S{n:05d}", prompt_type="sentence", text=text, normalized_text=text.lower(), source_file="t"))
        session.add(TaskSession(participant_id="P0006", task_type="sentence", status="paused", size=10, started_at=T0, ended_at=T0))
        session.commit()
    chat = ChatHarness(PHONE, start=T0)
    chat.text("Hi")
    chat.text("1")
    chat.voice(10)
    return chat


def set_test_participant(is_test: bool) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.scalar(select(Participant).where(Participant.participant_id == "P0006")).is_test = is_test
        session.commit()


def snapshot() -> dict:
    with db.get_sync_sessionmaker()() as session:
        state = session.get(ConversationState, "P0006")
        return {
            "state": (state.state, state.context),
            "sessions": session.execute(select(TaskSession.id, TaskSession.status).order_by(TaskSession.id)).all(),
            "assignments": session.execute(select(Assignment.id, Assignment.status, Assignment.recording_id).order_by(Assignment.id)).all(),
            "recordings": session.execute(select(Recording.id, Recording.assignment_id, Recording.qc_status)).all(),
            "minutes": minutes_recorded(session, "P0006"),
            "attributes": session.scalar(select(Participant.attributes).where(Participant.participant_id == "P0006")),
        }


# =====
# RESET
# =====
def test_reset_closes_the_session_expires_open_prompts_and_keeps_recordings(
    mid_session: ChatHarness, capsys: pytest.CaptureFixture
) -> None:
    before = snapshot()
    assert before["state"][0] == "in_session"

    assert main(["P0006", "--yes"], confirm=never_asked) == 0

    after = snapshot()
    assert after["state"] == (INITIAL_STATE, {})
    assert [status for _, status in after["sessions"]] == ["paused", "abandoned"]  # paused one left as is
    (recorded, opened) = after["assignments"]
    assert recorded == before["assignments"][0] and recorded[1] == "recorded"  # untouched
    assert opened[1] == "expired"  # the open prompt goes back to the pool
    assert after["recordings"] == before["recordings"] and after["minutes"] == before["minutes"]
    with db.get_sync_sessionmaker()() as session:
        ended = session.scalar(select(TaskSession).where(TaskSession.status == "abandoned"))
        assert ended.ended_at is not None and ended.ended_at > T0

    out = capsys.readouterr().out
    assert "Participant P0006" in out and "in_session -> idle" in out
    assert "closed (-> abandoned): 1" in out and "expired (prompts back to the pool): 1" in out
    assert "already ended, left as they are: 1" in out and "1 recorded/accepted assignments" in out
    assert PHONE not in out and PHONE[4:] not in out and "Chisomo" not in out


def test_after_a_reset_the_next_message_is_a_first_contact(mid_session: ChatHarness) -> None:
    main(["P0006", "--yes"], confirm=never_asked)
    (offer,) = mid_session.text("Hi")
    assert "welcome to the Chichewa voice project" in offer.body
    assert "swipe right on my message" in offer.body  # instructions again


def test_dry_run_changes_nothing(mid_session: ChatHarness, capsys: pytest.CaptureFixture) -> None:
    before = snapshot()
    assert main(["P0006", "--dry-run", "--targets", "free_speech=2"], confirm=never_asked) == 0
    assert snapshot() == before
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN: nothing changed.") and "would be closed" in out
    assert "targets override: none (the configured split) -> free_speech=2" in out


def test_reset_with_no_conversation_yet(clean_db: None, capsys: pytest.CaptureFixture) -> None:
    add_participant("P0006", PHONE)
    assert main(["P0006", "--yes"], confirm=never_asked) == 0
    assert "conversation: none yet" in capsys.readouterr().out


def test_unknown_participant(clean_db: None) -> None:
    with pytest.raises(SystemExit):
        main(["P9999", "--yes"], confirm=never_asked)


# ============
# CONFIRMATION
# ============
def test_asks_before_resetting_a_real_participant(mid_session: ChatHarness, capsys: pytest.CaptureFixture) -> None:
    before = snapshot()
    asked: list[str] = []

    assert main(["P0006"], confirm=lambda prompt: asked.append(prompt) or "n") == 1
    assert snapshot() == before and asked == ["Reset P0006? Type 'yes' to continue: "]
    assert capsys.readouterr().out.endswith("Nothing changed.\n")

    assert main(["P0006"], confirm=lambda prompt: "yes") == 0
    assert snapshot()["state"][0] == INITIAL_STATE


def test_test_participants_are_reset_without_asking(mid_session: ChatHarness) -> None:
    set_test_participant(True)
    assert main(["P0006"], confirm=never_asked) == 0
    assert snapshot()["state"][0] == INITIAL_STATE


# =======
# TARGETS
# =======
def test_targets_override_is_set_then_cleared(mid_session: ChatHarness, capsys: pytest.CaptureFixture) -> None:
    targets = "sentence=0,image=0,conversation_starter=0,free_speech=2"
    main(["P0006", "--yes", "--targets", targets], confirm=never_asked)
    assert snapshot()["attributes"]["targets_minutes"] == {
        "sentence": 0.0, "image": 0.0, "conversation_starter": 0.0, "free_speech": 2.0,
    }  # fmt: skip
    assert "targets in effect: sentence 0, image 0, conversation_starter 0, free_speech 2 (total 2 min)" in capsys.readouterr().out

    main(["P0006", "--yes", "--clear-targets"], confirm=never_asked)
    assert "targets_minutes" not in snapshot()["attributes"]
    defaults = get_collection_config().default_targets
    assert f"(total {sum(defaults.values()):g} min)" in capsys.readouterr().out


def test_targets_left_alone_by_default(mid_session: ChatHarness) -> None:
    main(["P0006", "--yes", "--targets", "image=3"], confirm=never_asked)
    main(["P0006", "--yes"], confirm=never_asked)
    assert snapshot()["attributes"]["targets_minutes"] == {"image": 3.0}


def test_targets_and_clear_targets_together_are_refused(mid_session: ChatHarness) -> None:
    with pytest.raises(SystemExit):
        main(["P0006", "--yes", "--clear-targets", "--targets", "image=1"], confirm=never_asked)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("video=2", "'video' isn't a task type"),
        ("image=-1", "must be a non-negative number"),
        ("image=lots", "isn't a number"),
        ("image", "expected type=minutes"),
        ("image=1,image=2", "given twice"),
        ("image=nan", "must be a non-negative number"),
        (",", "no targets given"),
    ],
)
def test_invalid_targets(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_targets(text, get_collection_config())


def test_invalid_targets_on_the_command_line(mid_session: ChatHarness, capsys: pytest.CaptureFixture) -> None:
    before = snapshot()
    with pytest.raises(SystemExit):
        main(["P0006", "--yes", "--targets", "video=2"], confirm=never_asked)
    assert "isn't a task type" in capsys.readouterr().err
    assert snapshot() == before
