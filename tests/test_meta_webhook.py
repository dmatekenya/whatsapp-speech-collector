"""Tests for the Meta WhatsApp Cloud API webhook (/webhooks/meta)."""

import hashlib
import hmac
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import meta
from src.api.meta import MetaWebhookConfig, get_meta_config, summarize_event
from src.storage.db import get_async_session
from src.storage.queue import get_inbound_queue
from tests.meta_payloads import PHONE_NUMBER_ID

# ========
# FIXTURES
# ========

VERIFY_TOKEN = "test-verify-token"
APP_SECRET = "test-app-secret"
PHONE = "265888000111"
TEXT = "moni, ndikufuna kuthandiza"

SAMPLE_EVENT = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WABA_ID",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "15550000000", "phone_number_id": PHONE_NUMBER_ID},
                        "contacts": [{"profile": {"name": "Test Person"}, "wa_id": PHONE}],
                        "messages": [
                            {"from": PHONE, "id": "wamid.A", "type": "text", "text": {"body": TEXT}},
                            {"from": PHONE, "id": "wamid.B", "type": "audio",
                             "audio": {"id": "MEDIA_ID", "mime_type": "audio/ogg; codecs=opus"}},
                        ],
                    },
                },
                {
                    "field": "messages",
                    "value": {"statuses": [{"id": "wamid.C", "status": "delivered", "recipient_id": PHONE}]},
                },
            ],
        }
    ],
}


class NoStorage:
    """Stands in for the database session and queue: any use fails the test."""

    def __getattr__(self, name):
        raise AssertionError("this request should not reach storage")


async def no_session():
    yield NoStorage()


def make_client(verify_token=VERIFY_TOKEN, app_secret=APP_SECRET) -> TestClient:
    """The Meta router alone, for requests refused before anything is stored."""
    app = FastAPI()
    app.include_router(meta.router)
    app.dependency_overrides[get_meta_config] = lambda: MetaWebhookConfig(verify_token, app_secret)
    app.dependency_overrides[get_async_session] = no_session
    app.dependency_overrides[get_inbound_queue] = NoStorage
    return TestClient(app)


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ================
# GET VERIFICATION
# ================


def test_verification_returns_challenge_as_plain_text():
    r = make_client().get("/webhooks/meta", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"})
    assert r.status_code == 200
    assert r.text == "1158201444"
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
    {},
])
def test_verification_refused(params):
    assert make_client().get("/webhooks/meta", params=params).status_code == 403


def test_verification_refused_when_token_not_configured():
    r = make_client(verify_token=None).get("/webhooks/meta", params={
        "hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"})
    assert r.status_code == 403


# ===========
# POST EVENTS
# ===========


def test_signed_event_accepted(client):
    # The full app (conftest), whose settings use the same token and secret.
    body = json.dumps(SAMPLE_EVENT).encode()
    r = client.post("/webhooks/meta", content=body,
                    headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"})
    assert r.status_code == 200


def test_signature_checked_against_raw_bytes():
    # Same JSON, different whitespace: the signature must match the exact bytes sent.
    signed = json.dumps(SAMPLE_EVENT).encode()
    sent = json.dumps(SAMPLE_EVENT, indent=2).encode()
    r = make_client().post("/webhooks/meta", content=sent, headers={"X-Hub-Signature-256": sign(signed)})
    assert r.status_code == 403


@pytest.mark.parametrize("header", [None, "", "sha1=abc", "sha256=deadbeef"])
def test_bad_or_missing_signature_refused(header):
    body = json.dumps(SAMPLE_EVENT).encode()
    headers = {} if header is None else {"X-Hub-Signature-256": header}
    assert make_client().post("/webhooks/meta", content=body, headers=headers).status_code == 403


def test_wrong_secret_refused():
    body = json.dumps(SAMPLE_EVENT).encode()
    r = make_client().post("/webhooks/meta", content=body,
                           headers={"X-Hub-Signature-256": sign(body, "other-secret")})
    assert r.status_code == 403


def test_event_refused_when_secret_not_configured():
    body = json.dumps(SAMPLE_EVENT).encode()
    r = make_client(app_secret=None).post("/webhooks/meta", content=body,
                                          headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 503


def test_signed_but_unreadable_body_still_acknowledged():
    body = b"not json"
    r = make_client().post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 200


# ====================
# METADATA AND LOGGING
# ====================


def test_summary_has_counts_and_types_only():
    s = summarize_event(SAMPLE_EVENT)
    assert s == {
        "object": "whatsapp_business_account",
        "entries": 1,
        "fields": {"messages": 2},
        "messages": 2,
        "message_types": {"text": 1, "audio": 1},
        "statuses": 1,
        "status_values": {"delivered": 1},
        "errors": 0,
    }


@pytest.mark.parametrize("payload", [None, [], "x", {"entry": "x"}, {"entry": [None, {"changes": [None]}]}])
def test_summary_tolerates_odd_payloads(payload):
    assert summarize_event(payload)["messages"] == 0


def test_logs_never_contain_personal_data_or_secrets(client, caplog):
    body = json.dumps(SAMPLE_EVENT).encode()
    with caplog.at_level(logging.DEBUG):
        client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sign(body)})
        client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sign(body)})  # duplicate
        client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})
        client.get("/webhooks/meta", params={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "42"})
    # All of the app's own logs (storing and queueing included); the test HTTP
    # client logs full request URLs.
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("src."))
    assert "Meta webhook event received" in logged and "Inbound meta message" in logged
    for forbidden in [PHONE, TEXT, "Test Person", "MEDIA_ID", "wamid.", PHONE_NUMBER_ID,
                      "15550000000", VERIFY_TOKEN, APP_SECRET]:
        assert forbidden not in logged


def test_uvicorn_access_log_redacts_verify_token():
    meta.install_log_redaction()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET",
         f"/webhooks/meta?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=42",
         "1.1", 200),
        None,
    )
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    line = record.getMessage()
    assert VERIFY_TOKEN not in line
    assert "hub.verify_token=[redacted]&hub.challenge=42" in line


def test_uvicorn_access_log_redacts_both_verify_token_forms():
    # Meta sends the token twice: hub.verify_token and hub_verify_token.
    meta.install_log_redaction()
    dotted, underscored = "dotted-token-value", "underscored-token-value"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET",
         f"/webhooks/meta?hub.mode=subscribe&hub.verify_token={dotted}"
         f"&hub_verify_token={underscored}&hub.challenge=42",
         "1.1", 200),
        None,
    )
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    line = record.getMessage()
    assert dotted not in line and underscored not in line
    assert "hub.verify_token=[redacted]&hub_verify_token=[redacted]&hub.challenge=42" in line
