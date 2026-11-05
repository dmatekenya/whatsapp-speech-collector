"""MetaChannel: parsing realistic webhook payloads, and the Graph API calls (mocked)."""

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from src.channels.base import MediaDownloadError, MessageSendError, MessageType
from src.channels.meta import MetaChannel, to_e164, to_wa_id
from src.logging_config import mask_message_id
from tests import meta_payloads as mp

WA_ID = "265991234567"
SENDER = "+265991234567"
TOKEN = "test-meta-access-token"
PNID = mp.PHONE_NUMBER_ID


def channel(handler=None) -> MetaChannel:  # noqa: ANN001
    transport = httpx.MockTransport(handler) if handler else None
    return MetaChannel(TOKEN, PNID, "v23.0", transport=transport)


def parse_one(message: dict) -> object:
    stored, statuses = channel().parse_event(mp.event([message]))
    assert len(stored) == 1 and statuses == []
    return channel().parse_inbound(stored[0])


# =======
# PARSING
# =======
def test_text_message() -> None:
    message = mp.text(WA_ID, "Moni, ndili wokonzeka")
    parsed = parse_one(message)
    assert parsed.type == MessageType.TEXT
    assert parsed.text == "Moni, ndili wokonzeka"
    assert parsed.sender == SENDER  # wa_id normalised to E.164
    assert parsed.message_id == message["id"]
    assert parsed.received_at == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert parsed.replied_to_message_id is None and parsed.forwarded == "no"
    assert parsed.is_voice_note is None and parsed.media_id is None


def test_voice_note() -> None:
    message = mp.voice_note(WA_ID)
    parsed = parse_one(message)
    assert parsed.type == MessageType.AUDIO
    assert parsed.is_voice_note is True
    assert parsed.media_id == message["audio"]["id"]
    assert parsed.media_content_type == "audio/ogg; codecs=opus"
    assert parsed.text is None and parsed.media_url is None


def test_uploaded_audio_file_is_not_a_voice_note() -> None:
    parsed = parse_one(mp.audio_file(WA_ID))
    assert parsed.type == MessageType.AUDIO and parsed.is_voice_note is False
    assert parsed.media_content_type == "audio/mpeg"


def test_audio_file_sent_as_document_counts_as_audio() -> None:
    message = {
        "from": WA_ID, "id": mp.wamid(WA_ID), "timestamp": mp.TIMESTAMP, "type": "document",
        "document": {"filename": "recording.m4a", "mime_type": "audio/mp4", "sha256": "x", "id": mp.media_id()},
    }  # fmt: skip
    parsed = parse_one(message)
    assert parsed.type == MessageType.AUDIO and parsed.is_voice_note is False


def test_image_with_caption() -> None:
    parsed = parse_one(mp.image(WA_ID, caption="Msika"))
    assert parsed.type == MessageType.OTHER
    assert parsed.media_content_type == "image/jpeg" and parsed.media_id
    assert parsed.text == "Msika"


def test_swipe_reply_carries_the_prompt_id() -> None:
    prompt_id = mp.wamid(WA_ID)
    parsed = parse_one(mp.voice_note(WA_ID, **mp.reply_context(prompt_id)))
    assert parsed.replied_to_message_id == prompt_id
    assert parsed.forwarded == "no"


@pytest.mark.parametrize(("frequently", "expected"), [(False, "forwarded"), (True, "frequently_forwarded")])
def test_forwarded_voice_note(frequently: bool, expected: str) -> None:
    parsed = parse_one(mp.voice_note(WA_ID, **mp.forwarded_context(frequently)))
    assert parsed.forwarded == expected
    assert parsed.replied_to_message_id is None


def test_button_and_list_replies_read_as_text() -> None:
    button = {"from": WA_ID, "id": mp.wamid(WA_ID), "timestamp": mp.TIMESTAMP, "type": "interactive",
              "interactive": {"type": "button_reply", "button_reply": {"id": "ready", "title": "Ready"}}}  # fmt: skip
    parsed = parse_one(button)
    assert parsed.type == MessageType.TEXT and parsed.text == "Ready"


@pytest.mark.parametrize("kind", ["reaction", "location", "unsupported", "sticker"])
def test_other_types(kind: str) -> None:
    message = {"from": WA_ID, "id": mp.wamid(WA_ID), "timestamp": mp.TIMESTAMP, "type": kind, kind: {}}
    assert parse_one(message).type == MessageType.OTHER


def test_batched_event_with_messages_and_statuses() -> None:
    other = "265881112223"
    payload = mp.event([mp.text(WA_ID, "Moni"), mp.voice_note(WA_ID), mp.text(other, "Hello")])
    payload["entry"][0]["changes"].append(
        {"value": {"messaging_product": "whatsapp", "metadata": {"phone_number_id": PNID},
                   "statuses": [mp.status("wamid.OUT1", "delivered", WA_ID), mp.failed_status("wamid.OUT2", WA_ID)]},
         "field": "messages"}
    )  # fmt: skip
    stored, statuses = channel().parse_event(payload)

    parsed = [channel().parse_inbound(s) for s in stored]
    assert [(m.sender, m.type) for m in parsed] == [
        (SENDER, MessageType.TEXT),
        (SENDER, MessageType.AUDIO),
        ("+265881112223", MessageType.TEXT),
    ]
    assert all(s["metadata"]["phone_number_id"] == PNID for s in stored)
    # Profile names aren't kept with the stored message.
    assert "Chikondi" not in json.dumps(stored)
    assert [(s.message_id, s.status) for s in statuses] == [("wamid.OUT1", "delivered"), ("wamid.OUT2", "failed")]


def test_statuses() -> None:
    meta = channel()
    assert meta.parse_status(mp.status("wamid.X", "sent", WA_ID)).status == "sent"
    assert meta.parse_status(mp.status("wamid.X", "read", WA_ID)).status == "read"
    assert meta.parse_status(mp.status("wamid.X", "deleted", WA_ID)).status is None  # not tracked

    failed = meta.parse_status(mp.failed_status("wamid.X", WA_ID, code=131047))
    assert failed.status == "failed" and failed.error_code == 131047
    assert failed.error == "Meta delivery failed: error 131047: Re-engagement message"
    assert "https://" not in failed.error


@pytest.mark.parametrize("payload", [None, [], {"entry": "x"}, {"entry": [{"changes": [{"value": "x"}]}]}])
def test_odd_payloads_parse_to_nothing(payload: object) -> None:
    assert channel().parse_event(payload) == ([], [])


def test_number_formats() -> None:
    assert to_e164("265991234567") == "+265991234567"
    assert to_wa_id("+265991234567") == "265991234567"


def test_masked_message_id_hides_the_number_inside_wamids() -> None:
    message_id = mp.wamid(WA_ID)
    masked = mask_message_id(message_id)
    assert masked.startswith("wamid#") and message_id[6:20] not in masked
    assert mask_message_id(message_id) == masked  # stable
    assert mask_message_id("SM123") == "SM123"  # Twilio SIDs are random


# =====
# MEDIA
# =====
def test_download_media_two_step_with_bearer_token() -> None:
    audio = b"OggS fake opus bytes"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={
                "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1&ext=2",
                "mime_type": "audio/ogg", "sha256": hashlib.sha256(audio).hexdigest(), "file_size": len(audio),
                "id": "MEDIA", "messaging_product": "whatsapp"})  # fmt: skip
        return httpx.Response(200, content=audio)

    meta = channel(handler)
    parsed = meta.parse_inbound(meta.parse_event(mp.event([mp.voice_note(WA_ID)]))[0][0])
    assert meta.download_media(parsed) == audio
    assert calls[0].url.path == f"/v23.0/{parsed.media_id}"
    assert all(c.headers["Authorization"] == f"Bearer {TOKEN}" for c in calls)


@pytest.mark.parametrize("failure", ["lookup", "fetch", "checksum"])
def test_download_errors_never_include_urls_or_ids(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.facebook.com":
            if failure == "lookup":
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/secret?x=1", "sha256": "0" * 64})
        return httpx.Response(403 if failure == "fetch" else 200, content=b"x")

    meta = channel(handler)
    parsed = meta.parse_inbound(meta.parse_event(mp.event([mp.voice_note(WA_ID)]))[0][0])
    with pytest.raises(MediaDownloadError) as caught:
        meta.download_media(parsed)
    text = str(caught.value)
    assert "https://" not in text and parsed.message_id not in text and parsed.media_id not in text
    assert caught.value.__cause__ is None


def test_delete_media_does_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no Graph API call expected")

    meta = channel(handler)
    meta.delete_media(meta.parse_inbound(meta.parse_event(mp.event([mp.voice_note(WA_ID)]))[0][0]))


# =======
# SENDING
# =======
def test_send_text_and_image() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v23.0/{PNID}/messages"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"input": WA_ID, "wa_id": WA_ID}],
                                         "messages": [{"id": f"wamid.SENT{len(sent)}"}]})  # fmt: skip

    meta = channel(handler)
    assert meta.send_text(SENDER, "Moni!") == "wamid.SENT1"
    assert meta.send_media(SENDER, "https://bot.example/media/prompts/tok", caption="Fotokozani") == "wamid.SENT2"
    assert sent[0] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": WA_ID,
                       "type": "text", "text": {"body": "Moni!", "preview_url": False}}  # fmt: skip
    assert sent[1]["type"] == "image"
    assert sent[1]["image"] == {"link": "https://bot.example/media/prompts/tok", "caption": "Fotokozani"}


@pytest.mark.parametrize(
    ("status", "code", "permanent"),
    [(400, 131026, True), (400, 131047, True), (429, 130429, False), (400, 131056, False), (500, 131000, False)],
)
def test_send_errors(status: int, code: int, permanent: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {
            "message": f"(#{code}) Recipient +265991234567 see https://developers.facebook.com/x",
            "type": "OAuthException", "code": code, "fbtrace_id": "Abc"}})  # fmt: skip

    with pytest.raises(MessageSendError) as caught:
        channel(handler).send_text(SENDER, "Moni")
    assert caught.value.permanent is permanent and caught.value.code == code and caught.value.status == status
    assert "https://" not in str(caught.value) and "265991234567" not in str(caught.value)


def test_send_without_configuration_fails_permanently() -> None:
    with pytest.raises(MessageSendError, match="META_ACCESS_TOKEN") as caught:
        MetaChannel(None, None).send_text(SENDER, "Moni")
    assert caught.value.permanent
