"""Chat with the bot in the terminal: fake inbound messages through the real engine.

    uv run python -m scripts.chat_simulator --as P0006 --reset
    uv run python -m scripts.chat_simulator --as P0006

Runs on a throwaway database (speech_collector_test by default, never the
real one). --reset empties it, copies the real prompt bank in (read-only),
and creates a fake participant with the given ID (fake name and number; the
real participant is never read). A fake channel prints what the bot would
send instead of calling Twilio. pytest also empties the test database, so a
test run resets the simulator too.

Input:
    any text          a text message ("Hi", "1", "STOP", ...)
    voice 7           a 7-second voice note
    voice 7 reply     ... sent as a swipe-reply to the last prompt
    voice 7 dup       ... with the same audio as the previous voice note
    image             a photo (also: video, document)
    wait 25           advance the clock 25 hours (to see prompts expire)
    quit
"""

import argparse
import os
import sys

# Settings must point at the throwaway database before any app module loads
# them, so the real database name is read first, then overridden.
from src.config import Settings  # noqa: E402

REAL_DB = Settings().postgres_db


def _configure(database: str) -> None:
    if database == REAL_DB:
        sys.exit("Refusing to run against the real database; use a throwaway one.")
    os.environ["POSTGRES_DB"] = database
    os.environ.setdefault("MEDIA_SIGNING_SECRET", "chat-simulator-only")
    if not os.environ.get("PUBLIC_BASE_URL") and not Settings().public_base_url:
        os.environ["PUBLIC_BASE_URL"] = "https://simulator.invalid"


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as", dest="participant_id", required=True, help="participant ID to chat as, e.g. P0006")
    parser.add_argument("--database", default="speech_collector_test")
    parser.add_argument("--reset", action="store_true", help="empty the database and set it up again")
    return parser.parse_args()


ARGS = _parse() if __name__ == "__main__" else None
if ARGS is not None:
    _configure(ARGS.database)

from datetime import UTC, date, datetime  # noqa: E402

from sqlalchemy import create_engine, select, text  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.conversation.harness import ChatHarness  # noqa: E402
from src.storage.db import get_sync_sessionmaker  # noqa: E402
from src.storage.models import ConversationState, Participant, Prompt  # noqa: E402

FAKE_PHONE_PREFIX = "+1555010"  # fictional range
TABLES = (
    "outbound_messages, conversation_state, assignments, sessions, recordings,"
    " inbound_messages, participants, prompts"
)
PROMPT_COLUMNS = (
    "prompt_id", "prompt_type", "text", "normalized_text", "media_path", "media_content_type",
    "media_size_bytes", "media_width", "media_height", "content_sha256", "source", "domain",
    "attributes", "flags", "source_file", "active",
)  # fmt: skip


# =====
# SETUP
# =====
def _fake_phone(participant_id: str) -> str:
    digits = "".join(ch for ch in participant_id if ch.isdigit()).zfill(4)[-4:]
    return f"{FAKE_PHONE_PREFIX}{digits}"


def reset(participant_id: str) -> None:
    real = create_engine(get_settings().sync_database_url.set(database=REAL_DB))
    with real.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        prompts = connection.execute(
            select(*(getattr(Prompt, column) for column in PROMPT_COLUMNS))
        ).mappings().all()
    real.dispose()
    with get_sync_sessionmaker()() as db:
        db.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY"))
        db.execute(Prompt.__table__.insert(), [dict(row) for row in prompts])
        db.add(
            Participant(
                participant_id=participant_id,
                name=f"Tester {participant_id}",
                phone_number=_fake_phone(participant_id),
                status="active",
                is_test=True,
                batch="chat-simulator",
                enrolled_on=date.today(),
                source_file="chat-simulator",
            )
        )
        db.commit()
    print(f"Reset {ARGS.database}: {len(prompts)} prompts copied, fake participant {participant_id}.")


def _check(participant_id: str) -> str:
    with get_sync_sessionmaker()() as db:
        if db.scalar(text("SELECT to_regclass('conversation_state')")) is None:
            sys.exit(f"Run: POSTGRES_DB={ARGS.database} uv run alembic upgrade head")
        participant = db.scalar(select(Participant).where(Participant.participant_id == participant_id))
        if participant is None:
            sys.exit(f"{participant_id} isn't set up in {ARGS.database}; run with --reset.")
        return participant.phone_number


# ====
# CHAT
# ====
def _print_bot(sent: list) -> None:
    if not sent:
        print("bot: (no reply)")
    for message in sent:
        header = f"bot [image {message.prompt_id}]:" if message.is_image else "bot:"
        lines = message.body.splitlines() or [""]
        print(header, lines[0])
        for line in lines[1:]:
            print("    " + line if line else "")
    print()


def _state(participant_id: str) -> str:
    with get_sync_sessionmaker()() as db:
        state = db.get(ConversationState, participant_id)
        return state.state if state else "idle"


def run(chat: ChatHarness, participant_id: str, line: str) -> bool:
    words = line.split()
    command = words[0].lower() if words else ""
    if command in ("quit", "exit"):
        return False
    if command == "voice" and len(words) >= 2:
        try:
            seconds = float(words[1])
        except ValueError:
            print("usage: voice <seconds> [reply|dup]")
            return True
        sent = chat.voice(seconds, reply="reply" in words[2:], duplicate="dup" in words[2:])
    elif command == "wait" and len(words) == 2:
        chat.advance(hours=float(words[1]))
        print(f"(clock: {chat.now:%Y-%m-%d %H:%M} UTC)\n")
        return True
    elif line.strip().lower() == "image":
        sent = chat.image()
    elif line.strip().lower() == "video":
        sent = chat.media("video/mp4")
    elif line.strip().lower() == "document":
        sent = chat.media("application/pdf")
    else:
        sent = chat.text(line)
    _print_bot(sent)
    print(f"  (state: {_state(participant_id)})\n")
    return True


def main() -> None:
    participant_id = ARGS.participant_id
    if ARGS.reset:
        reset(participant_id)
    phone = _check(participant_id)
    chat = ChatHarness(phone, start=datetime.now(UTC))
    print(f"Chatting as {participant_id} on {ARGS.database}. Type 'quit' to stop.\n")
    interactive = sys.stdin.isatty()
    for line in sys.stdin if not interactive else iter(lambda: input("you: "), None):
        line = line.rstrip("\n")
        if not interactive:
            print(f"you: {line}")
        if not line.strip():
            continue
        if not run(chat, participant_id, line):
            break


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
