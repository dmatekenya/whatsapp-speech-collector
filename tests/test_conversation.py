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


THIRD = 1 / 3
SPEECH = {"sentence": "scripted", "image": "spontaneous", "conversation_starter": "spontaneous"}


def config() -> CollectionConfig:
    return CollectionConfig.model_validate(
        {
            "task_types": {t: {"prompted": True, "speakers_per_prompt": 5, "speech_type": SPEECH[t]} for t in ("sentence", "image", "conversation_starter")},
            "session_size": 3,
            "assignment_expiry_hours": 24,
            "max_retakes_per_prompt": 2,
            "targets_minutes": {"per_participant": 30, "split": {"sentence": THIRD, "image": THIRD, "conversation_starter": THIRD}},
            "clip_limits": {"min_seconds": 5, "default": {"flag_over": 30, "max": 30}},
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
    """Give P0001 explicit per-type targets (a participant override; types left out keep 10)."""
    set_override(targets)


def set_override(targets_minutes: dict[str, float]) -> None:
    with db.get_sync_sessionmaker()() as session:
        participant = session.scalar(select(Participant).where(Participant.participant_id == "P0001"))
        participant.attributes = {**(participant.attributes or {}), "targets_minutes": targets_minutes}
        session.commit()


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
    # The total is the sum of the per-type targets: 0.25 + 0 + 0.
    assert text_of("progress.target_reached", total_target="0.2") in only(chat.text("Hi"))


# =======
# RETAKES
# =======
def test_retakes_for_short_long_and_empty_then_skip(chat: ChatHarness) -> None:
    chat.text("Hi")
    prompt = only(chat.text("1"))

    short = only(chat.voice(3))
    assert short.startswith("That recording was very short (3 seconds)") and prompt in short
    long = only(chat.voice(45))
    assert long.startswith("That recording was 45 seconds. Please record it again and keep it under 30 seconds.") and prompt in long
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
    assert minutes() == {"sentence": 0.0, "image": 0.0, "conversation_starter": 0.0, "free_speech": 0.0}


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


def test_no_speech_uses_up_retakes_like_too_short(chat: ChatHarness) -> None:
    chat.text("Hi")
    prompt = only(chat.text("1"))
    no_speech = CATALOG.raw("problems.no_speech")

    for _ in range(2):
        reply = only(chat.voice(12, active_seconds=0.3))
        assert any(reply.startswith(variant) for variant in no_speech) and prompt in reply
    moved_on = only(chat.voice(12, active_seconds=1.0))
    assert moved_on.startswith(text_of("problems.retake_limit")) and "Sentence 2 of 3:" in moved_on

    a1 = assignments()[0]
    assert a1.status == "skipped" and a1.retake_count == 2
    assert [(r.qc_status, r.qc_reason) for r in recordings()] == [("rejected", "no_speech")] * 3
    assert minutes()["sentence"] == 0


def test_low_speech_is_counted_but_flagged(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")

    assert only(chat.voice(20, active_seconds=2)).startswith("✅ Received.")
    assert only(chat.voice(20, active_seconds=2)).startswith("✅ Received.")
    assert only(chat.voice(20, active_seconds=12)).startswith("✅ That's 3 recordings.")  # session over

    assert [(r.qc_status, r.qc_reason) for r in recordings()] == [
        ("needs_review", "low_speech"),
        ("needs_review", "low_speech"),
        ("pending", None),
    ]
    assert minutes()["sentence"] == pytest.approx(60 / 60)
    # A clip that is also flagged for a speaker check keeps that reason: see the free speech tests.


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
        # A gap longer than fresh_start_after_hours: the session is paused.
        assert session.scalars(select(TaskSession.status)).all() == ["paused"]


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


class Refusing(RecordingChannel):
    """Refuses every send with the given provider error."""

    def __init__(self, code: int, permanent: bool = True) -> None:
        super().__init__()
        self.code = code
        self.permanent = permanent
        self.attempts = 0

    def send_text(self, to, body, status_callback=None):  # noqa: ANN001, ANN201
        self.attempts += 1
        raise MessageSendError(
            f"Twilio didn't accept the message: HTTP 400, error {self.code}: refused", 400, self.code,
            permanent=self.permanent, details={"provider": "twilio", "http_status": 400, "code": self.code},
        )  # fmt: skip

    send_media = send_text


def row_of(sent) -> OutboundMessage:  # noqa: ANN001
    with db.get_sync_sessionmaker()() as session:
        return session.get(OutboundMessage, sent.outbound_id)


def test_permanent_send_failure_is_recorded_and_rolls_back(chat: ChatHarness) -> None:
    chat.channel = Refusing(21211)
    (sent,) = chat.text("Hi")

    row = row_of(sent)
    assert row.status == "failed" and row.error_code == 21211 and "21211" in row.error
    assert row.error_details == {"provider": "twilio", "http_status": 400, "code": 21211}
    # Never delivered, so the conversation doesn't move on.
    assert state().state == "idle" and "welcomed" not in state().context


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


# ===========
# FRESH START
# ===========
def test_gap_mid_session_gives_the_welcome_back_summary(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    chat.voice(10)  # sentence 1 answered, sentence 2 open
    chat.advance(hours=25)

    offer = only(chat.text("Hi"))

    assert text_of("welcome.returning", first_name="Chisomo") in offer
    assert "So far you have recorded *0.2* of 30.0 minutes" in offer  # progress
    assert "Today's task is" in offer and text_of("ready.ask") in offer  # today's task
    assert state().state == "awaiting_ready"
    assert [a.status for a in assignments()] == ["recorded", "expired"]
    with db.get_sync_sessionmaker()() as session:
        assert session.scalars(select(TaskSession.status)).all() == ["paused"]


def test_no_fresh_start_within_the_gap(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    chat.advance(hours=20)
    assert only(chat.voice(10)).startswith("✅ Received. Sentence 2 of 3:")


def use_longer_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompts stay open for 48 h, but 24 h of silence still means a fresh start."""
    longer = config().model_copy(update={"assignment_expiry_hours": 48, "fresh_start_after_hours": 24})
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: longer)
    monkeypatch.setattr("src.tasks.bank.get_collection_config", lambda: longer)  # mark_sent's expiry


def test_swipe_reply_after_the_gap_still_binds_to_an_unexpired_prompt(
    chat: ChatHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_longer_expiry(monkeypatch)
    chat.text("Hi")
    chat.text("1")
    first = assignments()[0]
    chat.advance(hours=25)

    answer = only(chat.voice(10, reply=True))

    assert answer.startswith("✅ Received. Sentence 2 of 3:")
    assert assignments()[0].id == first.id and assignments()[0].status == "recorded"
    assert recordings()[-1].assignment_id == first.id
    assert state().state == "in_session"


def test_voice_note_after_the_gap_without_a_swipe_starts_afresh(
    chat: ChatHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_longer_expiry(monkeypatch)
    chat.text("Hi")
    chat.text("1")
    chat.advance(hours=25)

    offer = only(chat.voice(10))

    assert text_of("welcome.returning", first_name="Chisomo") in offer
    assert assignments()[0].status == "expired"  # unexpired, but expired by the fresh start
    assert recordings()[-1].assignment_id is None  # kept as free speech


# ============
# REPLY WINDOW
# ============
def test_send_is_skipped_outside_the_reply_window(chat: ChatHarness, caplog: pytest.LogCaptureFixture) -> None:
    sid = chat._store("SM", {"Body": "Hi", "NumMedia": "0"}, "text", None, False)
    (outbound_id,) = respond(sid, now=T0)

    with caplog.at_level("WARNING", logger="src.conversation.outbound"):
        assert send_outbound(outbound_id, chat.channel, T0 + timedelta(hours=24)) is None

    with db.get_sync_sessionmaker()() as session:
        row = session.get(OutboundMessage, outbound_id)
    assert row.status == "skipped_window" and row.provider_message_id is None
    assert "24.0 h ago" in row.error
    assert chat.channel.sent == []
    assert state().state == "idle"  # their next message gets the welcome
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    message = record.getMessage()
    assert "skipped_window" in message and REGISTERED_SENDER[4:] not in message and "Chisomo" not in message


def test_inside_the_window_is_sent(chat: ChatHarness) -> None:
    sid = chat._store("SM", {"Body": "Hi", "NumMedia": "0"}, "text", None, False)
    (outbound_id,) = respond(sid, now=T0)
    assert send_outbound(outbound_id, chat.channel, T0 + timedelta(hours=23)) is not None


@pytest.mark.parametrize("code", [131047, 63016])
def test_outside_window_error_is_not_retried(chat: ChatHarness, code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.conversation.outbound._last_attempt", lambda: False)  # retries left
    chat.channel = Refusing(code, permanent=False)

    (sent,) = chat.text("Hi")  # would raise (so RQ retries) for any other transient error

    row = row_of(sent)
    assert row.status == "skipped_window" and row.error_code == code
    assert chat.channel.attempts == 1
    assert state().state == "idle"


def test_transient_error_with_retries_left_is_retried(chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.conversation.outbound._last_attempt", lambda: False)
    chat.channel = Refusing(20429, permanent=False)
    with pytest.raises(MessageSendError):
        chat.text("Hi")
    assert outbound_rows_status() == ["pending"]


def outbound_rows_status() -> list[str]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(OutboundMessage.status).order_by(OutboundMessage.id)))


# ============
# FAILED SENDS
# ============
def test_failed_welcome_then_hi_gives_the_welcome_again(chat: ChatHarness) -> None:
    chat.channel = Refusing(21211)
    chat.text("Hi")
    assert state().state == "idle"

    chat.channel = RecordingChannel()
    offer = only(chat.text("Hi"))

    assert text_of("welcome.first_time", first_name="Chisomo") in offer
    assert "For each sentence:" in offer  # the instructions too: never seen
    assert state().state == "awaiting_ready"


def test_failed_first_prompt_returns_to_the_ready_question(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.channel = Refusing(21211)
    chat.text("1")

    assert state().state == "awaiting_ready"
    assert [a.status for a in assignments()] == ["expired"]
    with db.get_sync_sessionmaker()() as session:
        assert session.scalars(select(TaskSession.status)).all() == ["abandoned"]

    chat.channel = RecordingChannel()
    assert only(chat.text("1")).startswith("Sentence 1 of 3:")


def test_failed_next_prompt_is_sent_again_on_the_next_message(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    chat.channel = Refusing(21211)
    chat.voice(10)  # sentence 1 is recorded; sentence 2 isn't delivered
    assert assignments()[0].status == "recorded"  # the answer stands

    chat.channel = RecordingChannel()
    again = only(chat.text("hello?"))

    assert again.startswith("Sentence 2 of 3:")
    assert state().state == "in_session"


def test_a_later_turn_is_never_rolled_back(chat: ChatHarness) -> None:
    sid = chat._store("SM", {"Body": "Hi", "NumMedia": "0"}, "text", None, False)
    (first_reply,) = respond(sid, now=T0)
    chat.text("1")  # the next turn has already moved the conversation on

    chat.channel = Refusing(21211)
    send_outbound(first_reply, chat.channel, T0)

    assert state().state == "in_session"


def test_meta_send_error_details_are_stored_without_personal_data(
    chat: ChatHarness, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx

    from src.channels.meta import MetaChannel

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {
            "message": "(#131026) Message undeliverable to +265991234567",
            "type": "OAuthException", "code": 131026, "error_subcode": 2494010,
            "error_user_title": "Message undeliverable", "error_data": {"messaging_product": "whatsapp",
            "details": "Recipient 265991234567 cannot be reached"}, "fbtrace_id": "AbCdEf123"}})  # fmt: skip

    chat.channel = MetaChannel("token-secret", "100000000000001", transport=httpx.MockTransport(refuse))
    chat.channel.sent = []  # what the harness reads back
    with caplog.at_level("WARNING", logger="src.conversation.outbound"):
        (sent,) = chat.text("Hi")

    row = row_of(sent)
    assert row.status == "failed" and row.error_code == 131026
    assert row.error_details == {
        "provider": "meta", "http_status": 400, "code": 131026, "subcode": 2494010, "type": "OAuthException",
        "title": "Message undeliverable", "message": "(#131026) Message undeliverable to <number>",
        "details": "Recipient <number> cannot be reached", "fbtrace_id": "AbCdEf123",
    }  # fmt: skip
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "code=131026" in logged and "subcode=2494010" in logged and "fbtrace_id=AbCdEf123" in logged
    for forbidden in ("265991234567", "token-secret", "Chisomo", text_of("ready.ask")):
        assert forbidden not in logged and forbidden not in str(row.error_details)


# =======
# TARGETS
# =======
def test_progress_with_a_per_participant_override(chat: ChatHarness) -> None:
    set_override({"per_participant": 6})  # split as configured (a third each)
    chat.text("Hi")
    chat.text("1")
    chat.voice(12)
    progress = only(chat.text("progress"))
    assert "So far you have recorded *0.2* of 6.0 minutes" in progress
    assert "Reading sentences: 0.2 of 2.0 min" in progress and "Describing pictures: 0.0 of 2.0 min" in progress


def test_progress_with_explicit_per_type_targets_totals_their_sum(chat: ChatHarness) -> None:
    set_override({"sentence": 3, "image": 1.5, "conversation_starter": 0.5})
    chat.text("Hi")
    chat.text("1")
    chat.voice(12)
    progress = only(chat.text("progress"))
    assert "So far you have recorded *0.2* of 5.0 minutes" in progress
    assert "Reading sentences: 0.2 of 3.0 min" in progress and "Answering questions: 0.0 of 0.5 min" in progress


# ===========
# FREE SPEECH
# ===========
def free_speech_config() -> CollectionConfig:
    """Four types, half a minute each. Only free_speech can run: the test bank has no prompts."""
    return CollectionConfig.model_validate(
        {
            "task_types": {
                **{t: {"prompted": True, "speakers_per_prompt": 5, "speech_type": SPEECH[t]} for t in ("sentence", "image", "conversation_starter")},
                # As in config/assignment_settings.yaml: forwarded and uploaded audio count in free speech only.
                "free_speech": {
                    "prompted": False,
                    "speech_type": "spontaneous",
                    "audio_origin": {"forwarded": "accept_flagged", "uploaded": "accept_flagged"},
                },
            },
            "session_size": 5,
            "assignment_expiry_hours": 24,
            "max_retakes_per_prompt": 2,
            "targets_minutes": {"per_participant": 2, "split": dict.fromkeys(("sentence", "image", "conversation_starter", "free_speech"), 0.25)},
            # As in config/assignment_settings.yaml.
            "clip_limits": {
                "min_seconds": 5,
                "default": {"flag_over": 30, "max": 60},
                "task_types": {"free_speech": {"flag_over": 30, "max": 60, "max_forwarded_or_uploaded": 120}},
            },
        }
    )


@pytest.fixture
def free_chat(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> ChatHarness:
    add_participant("P0001", REGISTERED_SENDER, name="Chisomo Banda")
    current = free_speech_config()
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: current)
    monkeypatch.setattr("src.tasks.bank.get_collection_config", lambda: current)
    return ChatHarness(REGISTERED_SENDER, start=T0)


def test_free_speech_session_end_to_end(free_chat: ChatHarness) -> None:
    offer = only(free_chat.text("Hi"))
    assert text_of("today.free_speech") in offer
    assert "For each voice note:" in offer and "Aim for 15 to 30 seconds; a voice note can be up to 1 minute." in offer
    assert "forward an older voice note of your own (up to 2 minutes)" in offer

    first = only(free_chat.text("1"))
    assert first.startswith("Voice note 1 of 5: tell me about anything you like.")
    (invitation,) = assignments()
    assert invitation.prompt_id is None and invitation.prompt_type == "free_speech"
    assert state().state == "in_session"

    # Same retake rules as other types.
    retake = only(free_chat.voice(3))
    assert retake.startswith(text_of("problems.too_short", duration="3", min_seconds="5"))
    assert "Voice note 1 of 5:" in retake

    assert only(free_chat.voice(10)).startswith("✅ Received. Voice note 2 of 5:")
    assert "For each voice note:" in only(free_chat.text("INSTRUCTIONS"))

    progress = only(free_chat.text("progress"))
    assert progress.splitlines() == [
        "So far you have recorded *0.2* of 2.0 minutes:",
        "• Reading sentences: 0.0 of 0.5 min",
        "• Describing pictures: 0.0 of 0.5 min",
        "• Answering questions: 0.0 of 0.5 min",
        "• Talking about anything: 0.2 of 0.5 min",
    ]

    free_chat.voice(12)
    done = only(free_chat.voice(15))  # 37 s: the 0.5-minute target is met
    assert "You've finished *talking about anything*" in done
    assert "That's everything for now" in done  # no prompts for the other types
    assert [a.status for a in assignments()] == ["recorded"] * 3  # ended early, before 5
    with db.get_sync_sessionmaker()() as session:
        assert session.scalars(select(TaskSession.status)).all() == ["completed"]
    assert minutes()["free_speech"] == pytest.approx(37 / 60)


def test_free_speech_is_chosen_like_any_type_with_a_target(free_chat: ChatHarness) -> None:
    from src.tasks.bank import choose_task_type

    with db.get_sync_sessionmaker()() as session:
        assert choose_task_type(session, "P0001", T0, free_speech_config()) == "free_speech"


# ============
# AUDIO ORIGIN
# ============
def test_recorded_voice_note_is_accepted(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    assert only(chat.voice(10, origin="recorded")).startswith("✅ Received. Sentence 2 of 3:")
    recording = recordings()[-1]
    assert recording.audio_origin == "recorded" and recording.qc_status == "pending"
    assert minutes()["sentence"] > 0


def test_forwarded_voice_note_while_a_sentence_is_open(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    first = assignments()[0]

    reply = only(chat.voice(10, origin="forwarded"))

    assert reply.startswith(text_of("problems.not_recorded"))
    assert "Sentence 1 of 3:" in reply and first.prompt_text_snapshot in reply  # the same prompt
    recording = recordings()[-1]
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")
    assert recording.assignment_id == first.id  # linked as a failed attempt
    assignment = assignments()[0]
    assert assignment.status == "sent" and assignment.retake_count == 0 and assignment.recording_id is None
    assert minutes()["sentence"] == 0

    # More forwards never use up the retakes (max 2) or skip the prompt.
    for _ in range(3):
        assert "Sentence 1 of 3:" in only(chat.voice(10, origin="forwarded"))
    assert assignments()[0].status == "sent"
    assert only(chat.voice(10, origin="recorded")).startswith("✅ Received. Sentence 2 of 3:")


@pytest.mark.parametrize("origin", ["forwarded", "uploaded"])
def test_forwarded_or_uploaded_audio_outside_a_session_is_rejected_with_the_usual_reply(chat: ChatHarness, origin: str) -> None:
    offer = only(chat.voice(10, origin=origin))  # idle: the offer, as before
    assert text_of("ready.ask") in offer
    chat_reply = only(chat.voice(10, origin=origin))  # awaiting_ready: as before
    assert chat_reply == text_of("ready.ask")
    for recording in recordings():
        assert recording.assignment_id is None
        assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")


def test_uploaded_audio_while_a_sentence_is_open_is_rejected(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    first = assignments()[0]

    reply = only(chat.voice(10, origin="uploaded"))

    assert reply.startswith(text_of("problems.uploaded_audio_file"))
    assert "Sentence 1 of 3:" in reply and first.prompt_text_snapshot in reply  # the same prompt
    recording = recordings()[-1]
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")
    assert recording.assignment_id == first.id  # linked as a failed attempt
    # More uploads never use up the retakes (max 2) or skip the prompt.
    for _ in range(3):
        assert "Sentence 1 of 3:" in only(chat.voice(10, origin="uploaded"))
    assignment = assignments()[0]
    assert assignment.status == "sent" and assignment.retake_count == 0 and assignment.recording_id is None
    assert minutes()["sentence"] == 0
    assert only(chat.voice(10, origin="recorded")).startswith("✅ Received. Sentence 2 of 3:")


NOT_RECORDED_REPLY = {"forwarded": "problems.not_recorded", "uploaded": "problems.uploaded_audio_file"}


@pytest.mark.parametrize("origin", ["forwarded", "uploaded"])
@pytest.mark.parametrize(
    ("task_type", "label"),
    [("sentence", "Sentence"), ("image", "Picture"), ("conversation_starter", "Question")],
)
def test_forwarded_or_uploaded_audio_is_rejected_in_prompted_types(
    chat: ChatHarness, monkeypatch: pytest.MonkeyPatch, task_type: str, label: str, origin: str
) -> None:
    set_targets(monkeypatch, **{t: (1 if t == task_type else 0) for t in SPEECH})
    chat.text("Hi")
    chat.text("1")
    (first,) = assignments()
    assert first.prompt_type == task_type

    reply = "\n".join(s.body for s in chat.voice(10, origin=origin))

    assert reply.startswith(text_of(NOT_RECORDED_REPLY[origin])) and f"{label} 1 of 3" in reply
    recording = recordings()[-1]
    assert recording.audio_origin == origin
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")
    assignment = assignments()[0]
    assert assignment.status == "sent" and assignment.retake_count == 0 and assignment.recording_id is None
    assert minutes()[task_type] == 0


def test_unknown_origin_behaves_as_before(chat: ChatHarness) -> None:
    chat.text("Hi")
    chat.text("1")
    assert only(chat.voice(10)).startswith("✅ Received. Sentence 2 of 3:")
    recording = recordings()[-1]
    assert recording.audio_origin == "unknown" and recording.qc_status == "pending"


# ===========================================
# FORWARDED AND UPLOADED AUDIO IN FREE SPEECH
# ===========================================
FLAGGED_ORIGINS = pytest.mark.parametrize("origin", ["forwarded", "uploaded"])


def start_free_speech(free_chat: ChatHarness) -> Assignment:
    free_chat.text("Hi")
    free_chat.text("1")
    (invitation,) = assignments()
    assert invitation.prompt_type == "free_speech"
    return invitation


def starts_with_a_variant(reply: str, key: str, **values: object) -> bool:
    raw = CATALOG.raw(key)
    return any(reply.startswith(v.format(**values).strip()) for v in (raw if isinstance(raw, list) else [raw]))


@FLAGGED_ORIGINS
def test_forwarded_or_uploaded_audio_counts_in_free_speech_but_is_flagged(free_chat: ChatHarness, origin: str) -> None:
    invitation = start_free_speech(free_chat)

    reply = only(free_chat.voice(10, origin=origin))

    assert reply.startswith("✅ Received. Voice note 2 of 5:")  # the usual reply: the session moves on
    assert text_of(NOT_RECORDED_REPLY[origin]) not in reply
    recording = recordings()[-1]
    assert recording.audio_origin == origin
    assert (recording.qc_status, recording.qc_reason) == ("needs_review", "speaker_check_pending")
    assert recording.assignment_id == invitation.id
    first = assignments()[0]
    assert first.status == "recorded" and first.recording_id == recording.id
    assert minutes()["free_speech"] == pytest.approx(10 / 60)


@FLAGGED_ORIGINS
@pytest.mark.parametrize(
    ("seconds", "active", "problem"),
    [(3, None, "too_short"), (121, None, "too_long"), (10, 0.5, "no_speech")],
)
def test_forwarded_or_uploaded_free_speech_clip_still_gets_the_usual_retake(
    free_chat: ChatHarness, origin: str, seconds: float, active: float | None, problem: str
) -> None:
    start_free_speech(free_chat)

    reply = only(free_chat.voice(seconds, origin=origin, active_seconds=active))

    lengths = {"max_length": "2 minutes", "max_length_older": "2 minutes"}
    assert starts_with_a_variant(reply, f"problems.{problem}", duration=f"{seconds:.0f}", min_seconds="5", **lengths)
    assert "Voice note 1 of 5:" in reply
    recording = recordings()[-1]
    assert (recording.qc_status, recording.qc_reason) == ("rejected", problem)
    assignment = assignments()[0]
    assert assignment.status == "sent" and assignment.retake_count == 1
    assert minutes()["free_speech"] == 0


@FLAGGED_ORIGINS
def test_free_speech_clip_with_little_speech_keeps_the_speaker_check_flag(free_chat: ChatHarness, origin: str) -> None:
    start_free_speech(free_chat)
    assert only(free_chat.voice(10, origin=origin, active_seconds=1.6)).startswith("✅ Received.")
    recording = recordings()[-1]
    assert (recording.qc_status, recording.qc_reason) == ("needs_review", "speaker_check_pending")


def test_forwarded_duplicate_in_free_speech_is_not_counted(free_chat: ChatHarness) -> None:
    start_free_speech(free_chat)
    free_chat.voice(10, origin="forwarded")
    reply = only(free_chat.voice(10, origin="forwarded", duplicate=True))
    assert reply.startswith(text_of("problems.duplicate").splitlines()[0])
    assert minutes()["free_speech"] == pytest.approx(10 / 60)


@FLAGGED_ORIGINS
def test_forwarded_or_uploaded_audio_before_a_free_speech_session_is_still_rejected(free_chat: ChatHarness, origin: str) -> None:
    offer = only(free_chat.voice(10, origin=origin))  # idle: the offer, as before
    assert text_of("today.free_speech") in offer
    (recording,) = recordings()
    assert recording.assignment_id is None
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")


# ===========
# CLIP LENGTH
# ===========
REAL_LIMITS = {
    "min_seconds": 5,
    "default": {"flag_over": 30, "max": 60},
    "task_types": {
        "sentence": {"flag_over": 30, "max": 45, "flag": "long_sentence"},
        "image": {"flag_over": 30, "max": 60},
        "conversation_starter": {"flag_over": 30, "max": 60},
    },
}


@pytest.fixture
def limits_chat(world: None, monkeypatch: pytest.MonkeyPatch) -> ChatHarness:
    """The three prompted types with the clip limits of config/assignment_settings.yaml."""
    current = CollectionConfig.model_validate({**config().model_dump(), "clip_limits": REAL_LIMITS})
    monkeypatch.setattr("src.conversation.engine.get_collection_config", lambda: current)
    return ChatHarness(REGISTERED_SENDER, start=T0)


def start_type(chat: ChatHarness, monkeypatch: pytest.MonkeyPatch, task_type: str) -> Assignment:
    set_targets(monkeypatch, **{t: (10 if t == task_type else 0) for t in SPEECH})
    chat.text("Hi")
    chat.text("1")
    (first,) = assignments()
    assert first.prompt_type == task_type
    return first


TYPES = [("sentence", 45, "long_sentence", "45 seconds"), ("image", 60, "long_clip", "1 minute"), ("conversation_starter", 60, "long_clip", "1 minute")]


@pytest.mark.parametrize(("task_type", "max_s", "flag", "limit_text"), TYPES)
@pytest.mark.parametrize(
    ("seconds", "outcome"),
    [(4.9, "too_short"), (5.1, "ok"), (30, "ok"), (31, "long"), ("max-0.5", "long"), ("max+0.5", "too_long")],
)
def test_clip_limits_per_task_type(
    limits_chat: ChatHarness, monkeypatch: pytest.MonkeyPatch,
    task_type: str, max_s: float, flag: str, limit_text: str, seconds: float | str, outcome: str,
) -> None:  # fmt: skip
    first = start_type(limits_chat, monkeypatch, task_type)
    seconds = {"max-0.5": max_s - 0.5, "max+0.5": max_s + 0.5}.get(seconds, seconds)

    reply = only(limits_chat.voice(seconds))

    recording = recordings()[-1]
    assignment = assignments()[0]
    if outcome in ("too_short", "too_long"):
        assert (recording.qc_status, recording.qc_reason) == ("rejected", outcome)
        assert assignment.status == "sent" and assignment.retake_count == 1
        assert minutes()[task_type] == 0
        if outcome == "too_long":
            assert reply.startswith(f"That recording was {seconds:.0f} seconds. Please record it again and keep it under {limit_text}.")
        return
    assert reply.startswith("✅ Received.")
    assert assignment.id == first.id and assignment.status == "recorded"
    # Counted in full; the long flag is information only, never a review flag.
    assert minutes()[task_type] == pytest.approx(seconds / 60)
    assert (recording.qc_status, recording.qc_reason) == ("pending", None)
    assert recording.length_flag == (flag if outcome == "long" else None)


def test_long_flag_and_review_flags_are_independent(limits_chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    start_type(limits_chat, monkeypatch, "image")
    limits_chat.voice(40, active_seconds=9)  # 22.5% active: not low speech, long
    limits_chat.voice(20, active_seconds=2)  # low speech, not long
    first, second = recordings()
    assert (first.qc_status, first.length_flag) == ("pending", "long_clip")
    assert (second.qc_status, second.qc_reason, second.length_flag) == ("needs_review", "low_speech", None)


@pytest.mark.parametrize(("active", "accepted"), [(6, False), (10, True)])
def test_long_clip_that_is_mostly_silent_is_no_speech(
    limits_chat: ChatHarness, monkeypatch: pytest.MonkeyPatch, active: float, accepted: bool
) -> None:
    start_type(limits_chat, monkeypatch, "conversation_starter")
    reply = only(limits_chat.voice(40, active_seconds=active))  # 15% or 25% active
    recording = recordings()[-1]
    if accepted:
        assert reply.startswith("✅ Received.") and recording.qc_status == "pending"
        assert recording.length_flag == "long_clip"
    else:
        assert starts_with_a_variant(reply, "problems.no_speech")  # the usual no-speech retake
        assert (recording.qc_status, recording.qc_reason) == ("rejected", "no_speech")
        assert assignments()[0].retake_count == 1


def test_forwarded_long_sentence_is_still_rejected_as_not_recorded(limits_chat: ChatHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    start_type(limits_chat, monkeypatch, "sentence")
    reply = only(limits_chat.voice(100, origin="forwarded"))
    assert reply.startswith(text_of("problems.not_recorded"))
    recording = recordings()[-1]
    assert (recording.qc_status, recording.qc_reason) == ("rejected", "not_recorded")
    assert assignments()[0].retake_count == 0


@pytest.mark.parametrize(
    ("origin", "seconds", "accepted", "limit_text"),
    [
        ("recorded", 59, True, None),
        ("recorded", 61, False, "1 minute"),
        ("forwarded", 61, True, None),
        ("forwarded", 119, True, None),
        ("uploaded", 119, True, None),
        ("forwarded", 121, False, "2 minutes"),
        ("uploaded", 121, False, "2 minutes"),
    ],
)
def test_free_speech_clip_limits_by_origin(
    free_chat: ChatHarness, origin: str, seconds: float, accepted: bool, limit_text: str | None
) -> None:
    start_free_speech(free_chat)

    reply = only(free_chat.voice(seconds, origin=origin))

    recording = recordings()[-1]
    flagged = ("needs_review", "speaker_check_pending") if origin != "recorded" else ("pending", None)
    if accepted:
        assert recording.length_flag == "long_clip" and assignments()[0].status == "recorded"
        assert (recording.qc_status, recording.qc_reason) == flagged
        assert minutes()["free_speech"] == pytest.approx(seconds / 60)
    else:
        assert reply.startswith(f"That recording was {seconds} seconds. Please record it again and keep it under {limit_text}.")
        assert (recording.qc_status, recording.qc_reason) == ("rejected", "too_long")
        assert assignments()[0].retake_count == 1 and minutes()["free_speech"] == 0


@pytest.mark.parametrize(("origin", "qc"), [("recorded", ("pending", None)), ("forwarded", ("rejected", "not_recorded"))])
def test_long_clip_outside_a_session_is_kept_and_flagged(free_chat: ChatHarness, origin: str, qc: tuple) -> None:
    offer = only(free_chat.voice(150, origin=origin))  # over every max: never rejected for length here
    assert text_of("today.free_speech") in offer
    (recording,) = recordings()
    assert recording.assignment_id is None and recording.length_flag == "long_clip"
    assert (recording.qc_status, recording.qc_reason) == qc


@pytest.mark.parametrize(("seconds", "text"), [(45, "45 seconds"), (60, "1 minute"), (120, "2 minutes"), (90, "90 seconds"), (29.5, "29.5 seconds")])
def test_length_text(seconds: float, text: str) -> None:
    from src.conversation.engine import length_text

    assert length_text(seconds) == text
