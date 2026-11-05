import hashlib
import logging


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def mask_phone(number: str) -> str:
    """Mask a phone number for logging, e.g. +265991234567 -> +265******567."""
    if len(number) <= 7:
        return "*" * len(number)
    return f"{number[:4]}{'*' * (len(number) - 7)}{number[-3:]}"


def mask_message_id(message_id: str) -> str:
    """A provider message ID safe for logs.

    Meta's wamid.... IDs are base64 that contains the sender's number, so they
    are replaced by a short hash (the same ID always gives the same hash).
    Twilio's SIDs are random and are returned unchanged.
    """
    if message_id.startswith("wamid."):
        return "wamid#" + hashlib.sha256(message_id.encode()).hexdigest()[:12]
    return message_id
