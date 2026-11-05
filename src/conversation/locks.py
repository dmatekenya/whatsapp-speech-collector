"""One conversation change at a time per participant.

Taken by each turn (engine.respond) and by the send job when an undelivered
reply rolls the conversation back, so the two never interleave.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def lock_conversation(db: Session, sender: str) -> None:
    """Block until no other transaction holds this sender's lock; held until this transaction ends."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"conversation:{sender}"})
