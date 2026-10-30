from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from src.channels.base import MessageSendError
from src.config import get_settings
from src.conversation.engine import respond
from src.conversation.harness import ChatHarness, RecordingChannel
from src.conversation.messages import Catalog, CatalogError, get_catalog, required_keys
from src.conversation.outbound import CAPTION_LIMIT, PromptPart, Reply, emit, send_outbound
from src.media.urls import MEDIA_ROUTE, verify_token
from src.storage import db
from src.storage.models import (
    Recording,
    Assignment,
    ConversationState,
    InboundMessage,
    OutboundMessage,
    Participant,
    Prompt,
    TaskSession,
)
from src.tasks.bank import minutes_recorded
from src.tasks.config import CollectionConfig
from tests.conftest import REGISTERED_SENDER, add_participant

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
CATALOG = get_catalog()


def config(**targets: float) -> CollectionConfig:
    return CollectionConfig.model_validate(
        {
            "speakers_per_prompt": {"sentence": 5, "image": 5, "conversation_starter": 5},
            "session_size": 3,
            "assignment_expiry_hours": 24,
            "max_retakes_per_prompt": 2,
            "targets_minutes": {"total": 30, "sentence": 10, "image": 10, "conversation_starter": 10, **targets},
            "clip_seconds": {"min": 5, "max": 30},
        }
    )


def text_of(key: str, **values: object) -> str:
    """A catalog message as the engine renders it (first variant for lists)."""
    raw = CATALOG.raw(key)
    template = raw[0] if isinstance(raw, list) else raw
    return template.format(**values).strip()


# ========
# FIXTURES
# ========
@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> CollectionConfig:
    current = config()
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: current)
    monkeypatch.setattr(get_settings(), "support_contact", "+265 999 000 111 (project team)")
    return current


def set_targets(monkeypatch: pytest.MonkeyPatch, **targets: float) -> None:
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: config(**targets))


@pytest.fixture
def world(clean_db: None, cfg: CollectionConfig) -> None:
    add_participant("P0001", REGISTERED_SENDER, name="Chisomo Banda")
    with db.get_sync_sessionmaker()() as session:
        for n in range(1, 11):
            text = f"Sentence number {n} for reading aloud today."
            session.add(Prompt(prompt_id=f"S{n:05d}", prompt_type="sentence", text=text, normalized_text=text.lower(), source_file="t"))
        for n in range(1, 6):
            session.add(
                Prompt(
                    prompt_id=f"I{n:04d}", prompt_type="image", media_path=f"food/I{n:04d}.jpg",
                    media_content_type="image/jpeg", content_sha256=f"h{n}", source_file="t",
                )
            )
            question = f"Tell me about the market you visited, number {n}."
            session.add(Prompt(prompt_id=f"C{n:04d}", prompt_type="conversation_starter", text=question, normalized_text=question.lower(), source_file="t"))
        session.commit()


@pytest.fixture
def chat(world: None) -> ChatHarness:
    return ChatHarness(REGISTERED_SENDER, start=T0)


def state() -> ConversationState:
    with db.get_sync_sessionmaker()() as session:
        return session.get(ConversationState, "P0001")


def assignments() -> list[Assignment]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Assignment).order_by(Assignment.id)))


def recordings() -> list[Recording]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Recording).order_by(Recording.id)))


def minutes() -> dict[str, float]:
    with db.get_sync_sessionmaker()() as session:
        return minutes_recorded(session, "P0001")


def only(sent: list) -> str:
    assert len(sent) == 1, [str(s) for s in sent]
    return sent[0].body


# =============
# THE MAIN FLOW
# =============
def test_first_greeting_ready_full_session_then_continue(chat: ChatHarness) -> None:
    offer = only(chat.text("Hi"))
    for key in ("progress.none_yet", "today.sentence", "instructions.general", "ready.ask"):
        assert text_of(key) in offer, key
    assert text_of("welcome.first_time", first_name="Chisomo") in offer
    assert "For each sentence:" in offer
    assert state().state == "awaiting_ready"
    assert state().context["instructions_seen"] == {"general": True, "sentence": True}

    first = only(chat.text("1"))
    a1 = assignments()[0]
    assert first.startswith("Sentence 1 of 3:") and a1.prompt_text_snapshot in first
    assert state().state == "in_session"
    assert "A few tips" not in first  # already shown with the offer

    second = only(chat.voice(10))
    assert second.startswith("✅ Received. Sentence 2 of 3:")
    third = only(chat.voice(12))
    assert third.startswith("✅ Received. Sentence 3 of 3:")

    wrap = only(chat.voice(15))
    assert "That's 3 recordings" in wrap and "Next up: *describing pictures*" in wrap
    assert state().state == "awaiting_continue"
    assert state().context["offered_task_type"] == "image"
    assert [a.status for a in assignments()] == ["recorded"] * 3

    # Continue: the image tips (first time for this type) and the first picture, in one message.
    (image,) = chat.text("1")
    assert image.is_image and image.prompt_id.startswith("I")
    assert "For each picture:" in image.body and "Picture 1 of 3." in image.body
    assert "A few tips" not in image.body  # general tips only the first time ever
    url = urlparse(image.media_url)
    assert f"{url.scheme}://{url.netloc}" == get_settings().public_base_url.rstrip("/")
    assert verify_token(url.path.removeprefix(MEDIA_ROUTE + "/"), get_settings().media_signing_secret.get_secret_value()) == image.prompt_id


def test_returning_participant_gets_progress(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    chat.voice(12)
    chat.advance(hours=30)  # the open prompt expires
    offer = only(chat.text("Hello"))
    assert text_of("welcome.returning", first_name="Chisomo") in offer
    assert "So far you have recorded *0.2* of 30.0 minutes" in offer


def test_session_ends_early_when_type_target_met(chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    set_targets(monkeypatch, sentence=0.25)
    chat.text("Hi")
    chat.text("1")

    done = only(chat.voice(20))

    assert "You've finished *reading sentences*" in done
    assert "Next up: *describing pictures*" in done
    assert state().state == "awaiting_continue"
    with db.get_sync_sessionmaker()() as session:
        assert session.scalars(select(TaskSession.status)).all() == ["completed"]


def test_type_target_reached_with_nothing_next(chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    set_targets(monkeypatch, sentence=0.25, image=0, conversation_starter=0)
    chat.text("Hi")
    chat.text("1")

    done = only(chat.voice(20))

    assert "That's everything for now" in done and state().state == "idle"
    assert text_of("progress.target_reached", total_target="30.0") in only(chat.text("Hi"))


# =======
# RETAKES
# =======
def test_retakes_for_short_long_and_empty_then_skip(chat: ChatHarness) -> None:
    chat.text("Hi")
    prompt = only(chat.text("1"))

    short = only(chat.voice(3))
    assert short.startswith("That recording was very short (3 seconds)") and prompt in short
    long = only(chat.voice(45))
    assert "longer than the 30-second limit" in long and prompt in long
    a1 = assignments()[0]
    assert a1.retake_count == 2 and a1.status == "sent"

    moved_on = only(chat.voice(0.2))  # empty, and the retakes are used up
    assert moved_on.startswith(text_of("problems.retake_limit"))
    assert "Sentence 2 of 3:" in moved_on and prompt not in moved_on
    a1, a2 = assignments()
    assert a1.status == "skipped" and a2.status == "sent"

    # All three are failed attempts at prompt 1: linked to it and rejected,
    # not free speech, and none of them counts.
    assert [(r.assignment_id, r.qc_status, r.qc_reason) for r in recordings()] == [
        (a1.id, "rejected", "too_short"),
        (a1.id, "rejected", "too_long"),
        (a1.id, "rejected", "empty"),
    ]
    assert a1.recording_id is None
    assert minutes() == {"sentence": 0.0, "image": 0.0, "conversation_starter": 0.0}


def test_failed_attempts_then_a_good_take(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")

    chat.voice(3)  # too short
    accepted = only(chat.voice(12))

    assert accepted.startswith("✅ Received. Sentence 2 of 3:")
    a1 = assignments()[0]
    short, good = recordings()
    assert (short.assignment_id, short.qc_status, short.qc_reason) == (a1.id, "rejected", "too_short")
    assert (good.assignment_id, good.qc_status, good.qc_reason) == (a1.id, "pending", None)
    # recording_id points at the newest clip that wasn't rejected.
    assert a1.status == "recorded" and a1.recording_id == good.id and a1.retake_count == 1
    assert minutes()["sentence"] == pytest.approx(12 / 60)
    with db.get_sync_sessionmaker()() as session:
        free_speech = session.scalars(select(Recording).where(Recording.assignment_id.is_(None))).all()
    assert free_speech == []


def test_free_speech_stays_unlinked(chat: ChatHarness) -> None:
    chat.voice(8)  # idle: no open prompt
    (clip,) = recordings()
    assert clip.assignment_id is None and clip.qc_status == "pending"


def test_empty_audio_message(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    assert only(chat.voice(0.1)).startswith(text_of("problems.empty_audio"))


def test_duplicate_audio_asks_for_a_new_recording(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    second_prompt = only(chat.voice(10)).removeprefix("✅ Received. ")

    again = only(chat.voice(10, duplicate=True))

    assert again.startswith(text_of("problems.duplicate")) and second_prompt in again
    assert [a.status for a in assignments()] == ["recorded", "sent"]
    assert assignments()[1].retake_count == 0  # duplicates don't use up retakes


# ================
# NOT A VOICE NOTE
# ================
def test_text_and_media_instead_of_audio(chat: ChatHarness) -> None:
    chat.text("Hi")
    prompt = only(chat.text("1"))

    assert only(chat.text("what do I do?")) == text_of("problems.text_instead_of_audio")
    assert only(chat.image()) == text_of("problems.image_instead_of_audio")
    assert only(chat.media("video/mp4")) == text_of("problems.video_instead_of_audio")
    assert only(chat.media("application/pdf")) == text_of("problems.document_instead_of_audio")
    reminder = only(chat.text("Moni"))
    assert reminder.startswith(text_of("session.open_prompt_reminder")) and prompt in reminder
    assert state().state == "in_session" and assignments()[0].status == "sent"


# ========================
# SWIPE-REPLIES AND EXPIRY
# ========================
def test_swipe_reply_binds_to_the_older_prompt(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    first_sid = chat.last_prompt_sid
    second = only(chat.voice(10)).removeprefix("✅ Received. ")

    # Swipe-reply to the first prompt: a re-recording of it.
    again = only(chat.voice(14, reply_to=first_sid))

    a1, a2 = assignments()
    assert a1.status == "recorded" and a1.retake_count == 1
    assert a2.status == "sent"
    # The second prompt is still open, so it's sent again rather than a third.
    assert again == f"✅ Received. {second}"


def test_expired_assignment_is_treated_as_idle(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    chat.advance(hours=25)

    offer = only(chat.voice(10))

    assert text_of("welcome.returning", first_name="Chisomo") in offer
    assert text_of("ready.ask") in offer and state().state == "awaiting_ready"
    assert assignments()[0].status == "expired"
    with db.get_sync_sessionmaker()() as session:
        assert session.scalars(select(TaskSession.status)).all() == ["abandoned"]


# ===============
# GLOBAL COMMANDS
# ===============
def test_help_progress_instructions_keep_the_state(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")

    assert "+265 999 000 111 (project team)" in only(chat.text("HELP"))
    assert only(chat.text("progress")) == text_of("progress.none_yet")
    tips = only(chat.text(" Instructions! "))
    assert "A few tips" in tips and "For each sentence:" in tips
    assert state().state == "in_session"


def test_stop_cancel_then_stop_confirm(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")

    assert only(chat.text("STOP")) == text_of("stop.confirm")
    assert state().state == "awaiting_stop_confirm"
    assert only(chat.text("2")) == text_of("stop.cancelled")
    assert state().state == "in_session"

    chat.text("stop")
    assert only(chat.text("1")) == text_of("stop.done")
    with db.get_sync_sessionmaker()() as session:
        assert session.scalar(select(Participant.status)) == "withdrawn"
    assert chat.text("Hi") == []  # withdrawn: no reply


def test_ready_later_and_unclear(chat: ChatHarness) -> None:
    chat.text("Hi")
    assert only(chat.text("maybe")) == text_of("ready.unclear")
    assert only(chat.voice(10)) == text_of("ready.ask")  # kept as free speech
    assert only(chat.text("later")) == text_of("ready.later")
    assert state().state == "idle"


def test_continue_later_pauses(chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    set_targets(monkeypatch, sentence=0.25)
    chat.text("Hi")
    chat.text("1")
    chat.voice(20)
    assert only(chat.text("2")) == text_of("session.paused")
    assert state().state == "idle"


# ===================================
# UNREGISTERED AND NON-ACTIVE SENDERS
# ===================================
def test_unregistered_reply_at_most_once_a_day(world: None) -> None:
    stranger = ChatHarness("+265881112223", start=T0)

    assert only(stranger.text("Hi")) == text_of("unregistered")
    assert stranger.text("Hello?") == []
    stranger.advance(hours=23)
    assert stranger.voice(10) == []
    stranger.advance(hours=2)
    assert only(stranger.text("Hi")) == text_of("unregistered")


@pytest.mark.parametrize(
    ("status", "key"), [("paused", "participant.paused"), ("completed", "participant.completed"), ("inactive", "participant.paused")]
)
def test_non_active_participants(world: None, status: str, key: str) -> None:
    add_participant("P0002", "+265881112224", status=status)
    assert only(ChatHarness("+265881112224", start=T0).text("Hi")) == text_of(key)


# ===============================
# DELIVERY, IDEMPOTENCY, VARIANTS
# ===============================
def test_retried_job_does_not_send_twice(chat: ChatHarness) -> None:
    (sent,) = chat.text("Hi")
    with db.get_sync_sessionmaker()() as session:
        sid = session.scalar(select(InboundMessage.message_sid).order_by(InboundMessage.id.desc()))

    assert respond(sid, now=T0) == []  # the turn already happened
    assert send_outbound(sent.outbound_id, chat.channel) is None  # already sent
    assert len(chat.channel.sent) == 1
    with db.get_sync_sessionmaker()() as session:
        assert len(session.scalars(select(OutboundMessage)).all()) == 1


def test_pending_reply_is_returned_again_not_duplicated(world: None, cfg: CollectionConfig) -> None:
    chat = ChatHarness(REGISTERED_SENDER, start=T0)
    sid = chat._store("SM", {"Body": "Hi", "NumMedia": "0"}, "text", None, False)
    first = respond(sid, now=T0)
    assert respond(sid, now=T0) == first  # still pending: same row, no new reply


def test_message_variants_are_recorded(chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    set_targets(monkeypatch, sentence=0)  # start with pictures
    (offer,) = chat.text("Hi")
    (image,) = chat.text("1")

    with db.get_sync_sessionmaker()() as session:
        row = session.get(OutboundMessage, image.outbound_id)
        assignment = session.get(Assignment, row.assignment_id)
    caption = [k for k in row.message_keys if k["key"] == "prompt.image_caption"]
    assert len(caption) == 1 and isinstance(caption[0]["variant"], int)
    assert assignment.message_variant == caption[0]["variant"]
    assert row.media_prompt_id == assignment.prompt_id and assignment.outbound_message_sid == image.sid
    with db.get_sync_sessionmaker()() as session:
        offer_keys = session.get(OutboundMessage, offer.outbound_id).message_keys
    assert {"key": "instructions.image", "variant": None} in offer_keys
    assert [k["key"] for k in offer_keys][:2] == ["welcome.first_time", "progress.none_yet"]


def test_long_text_goes_before_the_image(world: None) -> None:
    with db.get_sync_sessionmaker()() as session:
        inbound = InboundMessage(message_sid="SM_LONG", sender=REGISTERED_SENDER, message_type="text", raw_payload={}, status="processed")
        session.add(inbound)
        session.flush()
        task_session = TaskSession(participant_id="P0001", task_type="image", status="active", size=3, started_at=T0)
        session.add(task_session)
        session.flush()
        assignment = Assignment(
            participant_id="P0001", prompt_id="I0001", prompt_type="image", session_id=task_session.id,
            position=1, status="sent", expires_at=T0 + timedelta(days=1),
        )
        session.add(assignment)
        session.flush()
        long_text = CATALOG.render("help", support_contact="x" * CAPTION_LIMIT)
        caption = CATALOG.render("prompt.image_caption", n=1, session_size=3)
        rows = emit(session, Reply(parts=[long_text], prompt=PromptPart(assignment, caption, "I0001")), inbound.id, "P0001")
        session.commit()
        assert [(r.purpose, r.sequence, r.media_prompt_id) for r in rows] == [
            ("reply", 1, None),
            ("reply_media", 2, "I0001"),
        ]
        assert rows[1].body == caption.text

    channel = RecordingChannel()
    send_outbound(rows[0].id, channel, T0)  # sends the text, then the image
    assert [url is not None for _, _, _, url in channel.sent] == [False, True]


def test_permanent_send_failure_is_recorded(chat: ChatHarness) -> None:
    class Refusing(RecordingChannel):
        def send_text(self, to, body, status_callback=None):  # noqa: ANN001, ANN201
            raise MessageSendError("Twilio didn't accept the message: HTTP 400, error 63016: outside window", 400, 63016, permanent=True)

    chat.channel = Refusing()
    (sent,) = chat.text("Hi")

    with db.get_sync_sessionmaker()() as session:
        row = session.get(OutboundMessage, sent.outbound_id)
    assert row.status == "failed" and row.error_code == 63016 and "outside window" in row.error
    assert state().state == "awaiting_ready"  # state isn't rolled back


# ==================
# DELIVERY CALLBACKS
# ==================
def _signed_post(client: TestClient, params: dict[str, str]):
    settings = get_settings()
    url = settings.public_base_url.rstrip("/") + "/whatsapp/status"
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)
    return client.post("/whatsapp/status", data=params, headers={"X-Twilio-Signature": signature})


def test_status_callbacks_update_delivery(client: TestClient, world: None) -> None:
    chat = ChatHarness(REGISTERED_SENDER, start=T0)
    (sent,) = chat.text("Hi")

    def status() -> OutboundMessage:
        with db.get_sync_sessionmaker()() as session:
            return session.get(OutboundMessage, sent.outbound_id)

    assert _signed_post(client, {"MessageSid": sent.sid, "MessageStatus": "delivered"}).status_code == 204
    assert status().status == "delivered"
    _signed_post(client, {"MessageSid": sent.sid, "MessageStatus": "read"})
    _signed_post(client, {"MessageSid": sent.sid, "MessageStatus": "sent"})  # late: no regression
    assert status().status == "read"

    bad = client.post("/whatsapp/status", data={"MessageSid": sent.sid, "MessageStatus": "failed"}, headers={"X-Twilio-Signature": "nope"})
    assert bad.status_code == 403 and status().status == "read"


def test_failed_delivery_stores_twilio_error(client: TestClient, world: None) -> None:
    chat = ChatHarness(REGISTERED_SENDER, start=T0)
    (sent,) = chat.text("Hi")

    _signed_post(
        client,
        {"MessageSid": sent.sid, "MessageStatus": "undelivered", "ErrorCode": "63019", "ErrorMessage": "Media failed to download from https://x.example/a.jpg"},
    )

    with db.get_sync_sessionmaker()() as session:
        row = session.get(OutboundMessage, sent.outbound_id)
    assert row.status == "failed" and row.error_code == 63019
    assert "63019" in row.error and "https://" not in row.error


# =======
# CATALOG
# =======
def test_every_catalog_key_renders() -> None:
    types = ["sentence", "image", "conversation_starter"]
    CATALOG.validate(types)
    for key, supplied in required_keys(types).items():
        raw = CATALOG.raw(key)
        # Every variant, with exactly the values the engine supplies for the key.
        for variant in raw if isinstance(raw, list) else [raw]:
            assert variant.format(**dict.fromkeys(supplied, "X")).strip(), key


def test_catalog_is_strict() -> None:
    catalog = Catalog({"welcome": {"first_time": "Hi {first_name}", "returning": ["A {first_name}", "B {first_name}"]}})
    with pytest.raises(CatalogError, match="needs values"):
        catalog.render("welcome.first_time")
    with pytest.raises(CatalogError, match="missing"):
        catalog.render("welcome.nope")
    variant = catalog.render("welcome.returning", first_name="Tiya")
    assert variant.variant in (0, 1) and variant.text == ["A Tiya", "B Tiya"][variant.variant]
    bad = Catalog({**CATALOG.data, "ready": {**CATALOG.data["ready"], "ask": "Ready, {first_nam}?"}})
    with pytest.raises(CatalogError, match="unknown placeholders"):
        bad.validate(["sentence", "image", "conversation_starter"])
