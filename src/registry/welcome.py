"""The registration welcome: a WhatsApp template sent to newly registered participants.

They haven't written to the bot yet, so only an approved template can reach them
(WhatsApp's 24-hour rule). Each send is an outbound_messages row with purpose
"template:<name>", sent through send_outbound (which skips the window check for
templates) on the Meta channel; the status webhook then records delivered/read/failed.

Used by scripts/send_welcome.py and scripts/import_participants.py --send-welcome.
Returns and logs participant IDs only: never phone numbers or names.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.channels.base import META, ChannelAdapter
from src.channels.factory import get_channel
from src.config import PROJECT_ROOT
from src.conversation.outbound import TEMPLATE_PREFIX, send_outbound
from src.storage.models import InboundMessage, OutboundMessage, Participant
from src.tasks.config import CollectionConfig, get_collection_config

logger = logging.getLogger(__name__)

DEFAULT_PATH = PROJECT_ROOT / "config" / "welcome.yaml"
# A previous welcome in one of these statuses counts as sent (pending: a send under way or
# interrupted; resend it with --force). failed and skipped_window don't count.
SENT_STATUSES = ("pending", "sent", "delivered", "read")

SKIP_UNKNOWN = "no such participant"
SKIP_INACTIVE = "inactive"
SKIP_TEST = "test participant (add --include-test)"
SKIP_SENT = "already sent (add --force to resend)"
SKIP_ACTIVE = "already active in the bot (add --force to send anyway)"


# ======
# CONFIG
# ======
class WelcomeConfigError(ValueError):
    """config/welcome.yaml is missing something or has a wrong value."""


class WelcomeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1)
    language: str = Field(min_length=2)
    send_on_import: bool = False

    @property
    def purpose(self) -> str:
        """outbound_messages.purpose of this template's sends."""
        return f"{TEMPLATE_PREFIX}{self.template}"


def load_welcome_config(path: Path = DEFAULT_PATH) -> WelcomeConfig:
    try:
        return WelcomeConfig.model_validate(yaml.safe_load(path.read_text()) or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(file)'}: {error['msg']}" for error in exc.errors()
        )
        raise WelcomeConfigError(f"{path.name}: {problems}") from None


@lru_cache
def get_welcome_config() -> WelcomeConfig:
    return load_welcome_config()


# ===============
# TEMPLATE VALUES
# ===============
def first_name(name: str | None) -> str:
    """{{1}}: the first word of the name, with a capital first letter ("chisomo banda" -> "Chisomo")."""
    words = (name or "").split()
    if not words:
        return "there"
    return words[0][:1].upper() + words[0][1:]


def target_minutes(participant: Participant, config: CollectionConfig | None = None) -> float:
    """The total target the bot shows this participant (welcome and progress summaries):
    the sum of their per-type targets, with their own override if they have one."""
    config = config or get_collection_config()
    return config.total(config.targets_for(participant.attributes))


def minutes_text(minutes: float) -> str:
    """{{2}}: "20" for a whole number, otherwise one decimal as the bot shows it ("4.5")."""
    rounded = round(minutes, 1)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"


def body_params(participant: Participant, config: CollectionConfig | None = None) -> list[str]:
    return [first_name(participant.name), minutes_text(target_minutes(participant, config))]


# =========
# SELECTION
# =========
@dataclass
class Candidate:
    participant_id: str
    first_name: str = ""
    target: str = ""
    skip: str | None = None  # why it won't be sent; None: it will


def select_participants(
    db: Session,
    participant_ids: Iterable[str] = (),
    batch: str | None = None,
    include_test: bool = False,
    force: bool = False,
    welcome: WelcomeConfig | None = None,
    config: CollectionConfig | None = None,
) -> list[Candidate]:
    """The participants to welcome, each with the reason it is skipped (if it is).

    Sent to: active participants, not test participants (unless include_test), who
    have never been sent this template and have never written to the bot. force
    sends anyway to those already sent it or already active in the bot (never to
    inactive participants). participant_ids and batch narrow the selection; with
    neither, every participant is considered.
    """
    welcome = welcome or get_welcome_config()
    ids = list(dict.fromkeys(participant_ids))
    query = select(Participant).order_by(Participant.participant_id)
    if ids:
        query = query.where(Participant.participant_id.in_(ids))
    if batch:
        query = query.where(Participant.batch == batch)
    participants = list(db.scalars(query))
    if not participants and not ids:
        return []

    numbers = [p.phone_number for p in participants]
    messaged = set(db.scalars(select(InboundMessage.sender).where(InboundMessage.sender.in_(numbers)).distinct()))
    already_sent = set(
        db.scalars(
            select(OutboundMessage.participant_id).where(
                OutboundMessage.purpose == welcome.purpose,
                OutboundMessage.status.in_(SENT_STATUSES),
                OutboundMessage.participant_id.in_([p.participant_id for p in participants]),
            )
        )
    )

    candidates = []
    for p in participants:
        params = body_params(p, config)
        candidate = Candidate(p.participant_id, params[0], params[1])
        if p.status != "active":
            candidate.skip = f"{SKIP_INACTIVE} (status {p.status})"
        elif p.is_test and not include_test:
            candidate.skip = SKIP_TEST
        elif p.participant_id in already_sent and not force:
            candidate.skip = SKIP_SENT
        elif p.phone_number in messaged and not force:
            candidate.skip = SKIP_ACTIVE
        candidates.append(candidate)
    found = {c.participant_id for c in candidates}
    candidates.extend(Candidate(pid, skip=SKIP_UNKNOWN) for pid in ids if pid not in found)
    return candidates


# =======
# SENDING
# =======
@dataclass
class Result:
    participant_id: str
    outcome: str  # sent, failed or skipped
    detail: str = ""


def send_welcome(
    session_factory: sessionmaker[Session],
    candidates: list[Candidate],
    channel: ChannelAdapter | None = None,
    welcome: WelcomeConfig | None = None,
    config: CollectionConfig | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[Result]:
    """Send the template to each candidate that isn't skipped, one at a time.

    Each send is written as a pending outbound_messages row first, then sent
    straight away (not through the queue). A refusal (template not approved,
    wrong name, language or values) is stored on the row and returned as failed;
    nothing is retried automatically.
    """
    welcome = welcome or get_welcome_config()
    adapter = channel or get_channel(META)
    results = []
    for candidate in candidates:
        if candidate.skip:
            results.append(Result(candidate.participant_id, "skipped", candidate.skip))
            continue
        with session_factory() as db:
            participant = db.scalar(select(Participant).where(Participant.participant_id == candidate.participant_id))
            params = body_params(participant, config)
            row = OutboundMessage(
                participant_id=participant.participant_id,
                inbound_message_id=None,
                purpose=welcome.purpose,
                sequence=1,
                message_keys=[
                    {
                        "key": welcome.purpose,
                        "variant": None,
                        "template": welcome.template,
                        "language": welcome.language,
                        "params": params,
                    }
                ],
                body=f"[template {welcome.template} ({welcome.language})] " + " | ".join(params),
                status="pending",
                channel=META,
                created_at=now(),
            )
            db.add(row)
            db.commit()
            outbound_id = row.id
        try:
            send_outbound(outbound_id, adapter, now())
        except Exception as exc:  # noqa: BLE001 - reported per participant, never retried
            logger.error("Welcome for %s not sent: %s", candidate.participant_id, type(exc).__name__)
        with session_factory() as db:
            row = db.get(OutboundMessage, outbound_id)
            if row.status == "sent":
                results.append(Result(candidate.participant_id, "sent"))
            else:
                if row.status == "pending":  # an unexpected error before the send finished
                    row.status, row.error = "failed", row.error or "not sent (unexpected error; see the log)"
                    db.commit()
                # Meta's error, e.g. "Meta didn't accept the message: HTTP 404, error 132001: ..."
                results.append(Result(candidate.participant_id, "failed", row.error or row.status))
    sent = sum(r.outcome == "sent" for r in results)
    failed = sum(r.outcome == "failed" for r in results)
    logger.info("Registration welcome (%s): %s sent, %s failed", welcome.template, sent, failed)
    return results
