from twilio.request_validator import RequestValidator

from src.channels.base import MessageType
from src.channels.twilio import TwilioChannel

TEST_TOKEN = "test-auth-token"
TEST_URL = "https://example.ngrok-free.app/whatsapp/webhook"


# ============================================================
# Fixtures
# ============================================================
def make_channel() -> TwilioChannel:
    return TwilioChannel(
        account_sid="ACtest",
        auth_token=TEST_TOKEN,
        whatsapp_number="+14155238886",
    )


VOICE_NOTE_PAYLOAD = {
    "MessageSid": "MM0001",
    "From": "whatsapp:+265991234567",
    "To": "whatsapp:+14155238886",
    "Body": "",
    "NumMedia": "1",
    "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM0001/Media/ME0001",
    "MediaContentType0": "audio/ogg",
    "OriginalRepliedMessageSid": "MM0000",
}

TEXT_PAYLOAD = {
    "MessageSid": "SM0002",
    "From": "whatsapp:+265991234567",
    "To": "whatsapp:+14155238886",
    "Body": "Moni",
    "NumMedia": "0",
}


# ============================================================
# Parsing
# ============================================================
def test_parse_voice_note() -> None:
    message = make_channel().parse_inbound(VOICE_NOTE_PAYLOAD)

    assert message.type is MessageType.AUDIO
    assert message.message_id == "MM0001"
    assert message.sender == "+265991234567"
    assert message.media_url == VOICE_NOTE_PAYLOAD["MediaUrl0"]
    assert message.media_content_type == "audio/ogg"
    assert message.replied_to_message_id == "MM0000"
    assert message.text is None
    assert message.received_at.tzinfo is not None
    assert message.raw == VOICE_NOTE_PAYLOAD


def test_parse_text() -> None:
    message = make_channel().parse_inbound(TEXT_PAYLOAD)

    assert message.type is MessageType.TEXT
    assert message.sender == "+265991234567"
    assert message.text == "Moni"
    assert message.media_url is None
    assert message.replied_to_message_id is None


# ============================================================
# Signature validation
# ============================================================
def test_valid_signature_passes() -> None:
    signature = RequestValidator(TEST_TOKEN).compute_signature(TEST_URL, TEXT_PAYLOAD)

    assert make_channel().validate_request(TEST_URL, TEXT_PAYLOAD, signature)


def test_wrong_signature_fails() -> None:
    assert not make_channel().validate_request(TEST_URL, TEXT_PAYLOAD, "not-a-valid-signature")
