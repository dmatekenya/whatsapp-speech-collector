"""The registration welcome: template payload, values, selection, sending and the window exemption."""

import csv
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.channels.base import MessageSendError
from src.channels.meta import MetaChannel, to_wa_id
from src.conversation.outbound import send_outbound
from src.registry.welcome import (
    SKIP_ACTIVE,
    SKIP_SENT,
    SKIP_TEST,
    SKIP_UNKNOWN,
    WelcomeConfig,
    first_name,
    load_welcome_config,
    minutes_text,
    select_participants,
    send_welcome,
    target_minutes,
)
from src.storage import db
from src.storage.models import InboundMessage, OutboundMessage, Participant
from src.tasks.config import get_collection_config
from tests import meta_payloads as mp

WELCOME = WelcomeConfig(template="registration_welcome", language="en")
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


# =======
# HELPERS
# =======
class TemplateChannel:
    """Stands in for MetaChannel: records template sends; free-form sends must not happen here."""

    name = "meta"

    def __init__(self, error: MessageSendError | None = None) -> None:
        self.error = error
        self.templates: list[tuple[str, str, str, list[str]]] = []
        self.texts: list[str] = []

    def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> str:
        self.templates.append((to, name, language, body_params))
        if self.error is not None:
            raise self.error
        return f"wamid.WELCOME{len(self.templates)}"

    def send_text(self, to: str, body: str, status_callback: str | None = None) -> str:
        self.texts.append(body)
        return f"wamid.TEXT{len(self.texts)}"


def person(
    participant_id: str,
    phone: str,
    name: str = "chisomo banda",
    status: str = "active",
    is_test: bool = False,
    batch: str = "pilot",
    attributes: dict | None = None,
) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(
            Participant(
                participant_id=participant_id, name=name, phone_number=phone, status=status, is_test=is_test,
                batch=batch, enrolled_on=date(2026, 11, 1), source_file=f"{batch}.csv", attributes=attributes or {},
            )
        )  # fmt: skip
        session.commit()


def inbound_from(phone: str, at: datetime = NOW) -> int:
    with db.get_sync_sessionmaker()() as session:
        n = session.query(InboundMessage).count() + 1
        message = InboundMessage(
            message_sid=f"wamid.IN{n}", sender=phone, message_type="text", raw_payload={}, status="processed",
            channel="meta", received_at=at,
        )  # fmt: skip
        session.add(message)
        session.commit()
        return message.id


def earlier_welcome(participant_id: str, status: str) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(
            OutboundMessage(
                participant_id=participant_id, purpose=WELCOME.purpose, body="", status=status, channel="meta",
            )
        )  # fmt: skip
        session.commit()


def outbound_rows() -> list[OutboundMessage]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(OutboundMessage).order_by(OutboundMessage.id)))


def selected(**kwargs: object) -> dict[str, str | None]:
    with db.get_sync_sessionmaker()() as session:
        return {c.participant_id: c.skip for c in select_participants(session, welcome=WELCOME, **kwargs)}


# =======
# PAYLOAD
# =======
def test_template_payload_has_name_language_and_body_values_in_order() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.T1"}]})

    meta = MetaChannel("token", mp.PHONE_NUMBER_ID, "v23.0", transport=httpx.MockTransport(handler))
    assert meta.send_template("+265991234567", "registration_welcome", "en", ["Chisomo", "20"]) == "wamid.T1"
    assert sent == [
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id("+265991234567"),
            "type": "template",
            "template": {
                "name": "registration_welcome",
                "language": {"code": "en"},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": "Chisomo"}, {"type": "text", "text": "20"}]}
                ],
            },
        }
    ]


@pytest.mark.parametrize("code", [132001, 132000, 132015])
def test_a_rejected_template_is_a_permanent_error(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if code == 132001 else 400, json={"error": {
            "message": f"(#{code}) Template name does not exist in the translation", "code": code}})  # fmt: skip

    meta = MetaChannel("token", mp.PHONE_NUMBER_ID, transport=httpx.MockTransport(handler))
    with pytest.raises(MessageSendError) as caught:
        meta.send_template("+265991234567", "registration_welcome", "en", ["Chisomo", "20"])
    assert caught.value.permanent and caught.value.code == code and str(code) in str(caught.value)


def test_the_real_config_loads() -> None:
    welcome = load_welcome_config()
    assert (welcome.template, welcome.language, welcome.send_on_import) == ("registration_welcome", "en", True)
    assert welcome.purpose == "template:registration_welcome"


# ======
# VALUES
# ======
@pytest.mark.parametrize(
    ("name", "expected"),
    [("chisomo banda", "Chisomo"), ("Chikondi", "Chikondi"), ("  tiyamike  phiri ", "Tiyamike"), ("McDonald x", "McDonald"), ("", "there")],
)  # fmt: skip
def test_first_name(name: str, expected: str) -> None:
    assert first_name(name) == expected


@pytest.mark.parametrize(
    ("override", "minutes", "text"),
    [
        (None, 20.0, "20"),  # the configured per_participant
        ({"per_participant": 4}, 4.0, "4"),  # a total, split as configured
        ({"sentence": 2, "image": 1, "conversation_starter": 1, "free_speech": 0.5}, 4.5, "4.5"),  # per type
        ({"sentence": 2}, 16.0, "16"),  # one type given; the others keep their defaults (as the bot counts)
    ],
)
def test_target_is_the_total_the_bot_shows(override: dict | None, minutes: float, text: str) -> None:
    config = get_collection_config()
    participant = Participant(attributes={"targets_minutes": override} if override else {})
    assert target_minutes(participant, config) == minutes == config.total(config.targets_for(participant.attributes))
    assert minutes_text(minutes) == text


@pytest.mark.parametrize(("minutes", "text"), [(20.0, "20"), (6.0, "6"), (4.5, "4.5"), (2.25, "2.2"), (0.0, "0")])
def test_minutes_whole_numbers_without_decimals(minutes: float, text: str) -> None:
    assert minutes_text(minutes) == text


# =========
# SELECTION
# =========
@pytest.fixture
def people(clean_db: None) -> None:
    person("P0001", "+265991000001")  # new: gets it
    person("P0002", "+265991000002", is_test=True)
    person("P0003", "+265991000003", status="paused")
    person("P0004", "+265991000004")  # has already written to the bot
    inbound_from("+265991000004")
    person("P0005", "+265991000005")  # already welcomed
    earlier_welcome("P0005", "delivered")
    person("P0006", "+265991000006")  # an earlier welcome failed: gets it again
    earlier_welcome("P0006", "failed")
    person("P0007", "+265991000007", batch="cohort-2")


def test_selection_rules(people: None) -> None:
    assert selected() == {
        "P0001": None,
        "P0002": SKIP_TEST,
        "P0003": "inactive (status paused)",
        "P0004": SKIP_ACTIVE,
        "P0005": SKIP_SENT,
        "P0006": None,
        "P0007": None,
    }
    # --batch, --participants (unknown IDs reported), --include-test
    assert selected(batch="cohort-2") == {"P0007": None}
    assert selected(participant_ids=["P0001", "P0099"]) == {"P0001": None, "P0099": SKIP_UNKNOWN}
    assert selected(participant_ids=["P0002"], include_test=True) == {"P0002": None}
    # --force: again to those already sent it or already active; never to inactive participants.
    forced = selected(force=True)
    assert forced["P0004"] is None and forced["P0005"] is None
    assert forced["P0003"] == "inactive (status paused)" and forced["P0002"] == SKIP_TEST


# =======
# SENDING
# =======
def test_send_records_the_outbound_row_and_never_sends_twice(people: None) -> None:
    channel = TemplateChannel()
    with db.get_sync_sessionmaker()() as session:
        candidates = select_participants(session, participant_ids=["P0001", "P0005"], welcome=WELCOME)

    results = send_welcome(db.get_sync_sessionmaker(), candidates, channel, WELCOME, now=lambda: NOW)

    assert [(r.participant_id, r.outcome) for r in results] == [("P0001", "sent"), ("P0005", "skipped")]
    assert channel.templates == [("+265991000001", "registration_welcome", "en", ["Chisomo", "20"])]
    row = outbound_rows()[-1]
    assert (row.participant_id, row.purpose, row.channel, row.status) == ("P0001", "template:registration_welcome", "meta", "sent")
    assert row.inbound_message_id is None and row.provider_message_id == "wamid.WELCOME1" and row.sent_at == NOW
    assert row.message_keys[0]["params"] == ["Chisomo", "20"] and row.message_keys[0]["language"] == "en"

    # Never twice without --force.
    assert selected(participant_ids=["P0001"]) == {"P0001": SKIP_SENT}


def test_delivery_statuses_from_the_webhook_update_the_welcome(people: None, client: TestClient) -> None:
    with db.get_sync_sessionmaker()() as session:
        candidates = select_participants(session, participant_ids=["P0001"], welcome=WELCOME)
    send_welcome(db.get_sync_sessionmaker(), candidates, TemplateChannel(), WELCOME)

    body, headers = mp.signed(mp.event(statuses=[mp.status("wamid.WELCOME1", "delivered", "265991000001")]))
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200
    assert outbound_rows()[-1].status == "delivered"


def test_a_refused_template_is_failed_with_metas_error_and_not_retried(people: None) -> None:
    refused = MessageSendError(
        "Meta didn't accept the message: HTTP 404, error 132001: (#132001) Template name does not exist in the translation",
        status=404, code=132001, permanent=True, details={"provider": "meta", "code": 132001},
    )  # fmt: skip
    channel = TemplateChannel(error=refused)
    with db.get_sync_sessionmaker()() as session:
        candidates = select_participants(session, participant_ids=["P0001", "P0006"], welcome=WELCOME)

    results = send_welcome(db.get_sync_sessionmaker(), candidates, channel, WELCOME)

    assert [r.outcome for r in results] == ["failed", "failed"]
    assert "132001" in results[0].detail and "Template name does not exist" in results[0].detail
    assert len(channel.templates) == 2  # one attempt each
    failed = [r for r in outbound_rows() if r.participant_id == "P0001"]
    assert [(r.status, r.error_code) for r in failed] == [("failed", 132001)]
    # A failed welcome doesn't count as sent: the next run tries again.
    assert selected(participant_ids=["P0001"]) == {"P0001": None}


# =============
# REPLY WINDOW
# =============
def test_the_window_check_skips_templates_but_still_blocks_other_messages(people: None) -> None:
    channel = TemplateChannel()
    # A template to a participant who has never written: sent.
    with db.get_sync_sessionmaker()() as session:
        candidates = select_participants(session, participant_ids=["P0001"], welcome=WELCOME)
    assert send_welcome(db.get_sync_sessionmaker(), candidates, channel, WELCOME)[0].outcome == "sent"

    # A free-form reply to a message 30 hours old: still not sent.
    inbound_id = inbound_from("+265991000007", at=NOW - timedelta(hours=30))
    with db.get_sync_sessionmaker()() as session:
        reply = OutboundMessage(
            participant_id="P0007", inbound_message_id=inbound_id, purpose="reply", body="Moni", status="pending",
            channel="meta", created_at=NOW,
        )  # fmt: skip
        session.add(reply)
        session.commit()
        reply_id = reply.id
    assert send_outbound(reply_id, channel, NOW) is None
    with db.get_sync_sessionmaker()() as session:
        assert session.get(OutboundMessage, reply_id).status == "skipped_window"
    assert channel.texts == []


# ======
# SCRIPT
# ======
def test_dry_run_lists_ids_names_and_targets_and_sends_nothing(
    people: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from scripts.send_welcome import main

    def no_channel(name: str | None = None) -> None:
        raise AssertionError("a dry run must not send")

    monkeypatch.setattr("src.registry.welcome.get_channel", no_channel)
    before = len(outbound_rows())

    assert main(["--dry-run", "--batch", "pilot"]) == 0

    out = capsys.readouterr().out
    assert "P0001  Chisomo  target 20 min" in out and "P0006  Chisomo  target 20 min" in out
    assert f"P0005  skipped: {SKIP_SENT}" in out
    assert "+265" not in out and "991000001" not in out
    assert len(outbound_rows()) == before


def test_script_exits_1_when_a_send_fails(people: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from scripts.send_welcome import main

    refusing = TemplateChannel(error=MessageSendError("Meta didn't accept the message: HTTP 400, error 132000: (#132000) Number of parameters does not match", code=132000, permanent=True))  # fmt: skip
    monkeypatch.setattr("src.registry.welcome.get_channel", lambda name=None: refusing)

    assert main(["--participants", "p0001"]) == 1
    out = capsys.readouterr().out
    assert "P0001  failed: Meta didn't accept the message: HTTP 400, error 132000" in out


# ======
# IMPORT
# ======
def write_people(tmp_path: Path, rows: list[list[str]]) -> Path:
    path = tmp_path / "pilot-1.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "phone_number", "is_test"])
        writer.writerows(rows)
    return path


def test_import_send_welcome_sends_to_new_participants_only(
    clean_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.import_participants import main

    channel = TemplateChannel()
    monkeypatch.setattr("src.registry.welcome.get_channel", lambda name=None: channel)
    path = write_people(tmp_path, [["Alice Banda", "+265 888 72 15 91", "false"], ["Tess Tester", "0999 123 456", "true"]])

    # A dry run and an explicit opt-out send nothing.
    for argv in ([str(path), "--dry-run"], [str(path), "--no-send-welcome"]):
        with pytest.raises(SystemExit) as done:
            main(argv)
        assert done.value.code == 0
    assert channel.templates == []

    # The participants already exist now: a second import has no new ones to welcome.
    with pytest.raises(SystemExit) as done:
        main([str(path)])
    assert done.value.code == 0 and channel.templates == []

    # New participants get it; the test participant is skipped.
    more = write_people(tmp_path, [["Ben Phiri", "0888 111 222", "false"], ["Tina Test", "0999 222 333", "true"]])
    with pytest.raises(SystemExit) as done:
        main([str(more)])
    assert done.value.code == 0
    assert [(to, params) for to, _, _, params in channel.templates] == [("+265888111222", ["Ben", "20"])]
