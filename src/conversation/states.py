"""The conversation engine's states (conversation_state.state).

Kept apart from engine.py so the send job (outbound.py) and admin scripts can
use them without importing the whole engine. Must match
CONVERSATION_STATES in src/storage/models.py (the database check constraint).
"""

from typing import Any

IDLE = "idle"
AWAITING_READY = "awaiting_ready"
IN_SESSION = "in_session"
AWAITING_CONTINUE = "awaiting_continue"
AWAITING_STOP_CONFIRM = "awaiting_stop_confirm"
STATES = (IDLE, AWAITING_READY, IN_SESSION, AWAITING_CONTINUE, AWAITING_STOP_CONFIRM)
# A participant's first contact, and what scripts/reset_participant.py goes back to.
INITIAL_STATE = IDLE


def initial_context() -> dict[str, Any]:
    """An empty context: not yet welcomed, no instructions seen, nothing offered."""
    return {}
