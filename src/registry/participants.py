from collections.abc import Iterable

import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.logging_config import mask_phone
from src.storage.models import Participant


# ============================================================
# Phone numbers
# ============================================================
def normalize_phone(raw: str, default_region: str, allowed_country_codes: Iterable[str]) -> str:
    """Return the number in E.164, or raise ValueError. Never guesses."""
    allowed = {str(code) for code in allowed_country_codes}
    try:
        number = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        raise ValueError(f"{mask_phone(raw)} is not a phone number") from None
    if not phonenumbers.is_valid_number(number):
        raise ValueError(f"{mask_phone(raw)} is not a valid phone number")
    if str(number.country_code) not in allowed:
        raise ValueError(
            f"country code +{number.country_code} is not allowed (allowed: "
            + ", ".join(f"+{code}" for code in sorted(allowed))
            + ")"
        )
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


# ============================================================
# Lookup
# ============================================================
def find_participant(session: Session, phone_number: str) -> Participant | None:
    return session.scalar(select(Participant).where(Participant.phone_number == phone_number))


def find_active_participant(session: Session, phone_number: str) -> Participant | None:
    participant = find_participant(session, phone_number)
    return participant if participant is not None and participant.status == "active" else None
