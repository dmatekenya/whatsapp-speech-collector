"""Reset a participant's conversation (for testing, and for unsticking pilot participants).

    uv run python scripts/reset_participant.py P0006 --dry-run
    uv run python scripts/reset_participant.py P0006
    uv run python scripts/reset_participant.py P0006 --targets sentence=0,image=0,conversation_starter=0,free_speech=2
    uv run python scripts/reset_participant.py P0006 --clear-targets --yes

In one transaction, under the same per-participant lock the conversation
engine uses:
  1. their active session is closed: status abandoned, ended_at now (paused
     and other ended sessions are already closed and are left as they are);
  2. their open assignments (status sent) expire, so the prompts go back to
     the pool;
  3. their conversation goes back to the engine's initial state (idle, empty
     context: their next message gets the first-time welcome and the
     instructions again).
Never touched: recordings, recorded/accepted/rejected/skipped assignments, and
so recorded minutes (progress). Replies still waiting to be sent are listed,
not changed.

--targets sets an explicit per-type override (participants.attributes
.targets_minutes); --clear-targets removes it, back to the configured split.

Asks for confirmation unless --yes is given or the participant is a test
participant (is_test). Prints participant IDs only, never names or numbers.
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from src.conversation.locks import lock_conversation
from src.conversation.states import INITIAL_STATE, initial_context
from src.storage.db import get_sync_sessionmaker
from src.storage.models import (
    Assignment,
    ConversationState,
    OutboundMessage,
    Participant,
    Recording,
    TaskSession,
)
from src.tasks.bank import COUNTED_STATUSES, minutes_recorded
from src.tasks.config import CollectionConfig, get_collection_config

TARGETS_KEY = "targets_minutes"
UNCHANGED = object()  # targets: leave the override as it is


# =======
# TARGETS
# =======
def parse_targets(text: str, config: CollectionConfig) -> dict[str, float]:
    """"sentence=0,free_speech=2" -> {"sentence": 0.0, "free_speech": 2.0}.

    Task types must be in config/assignment_settings.yaml; minutes must be
    non-negative numbers. Types left out keep their configured target.
    """
    targets: dict[str, float] = {}
    for part in (piece.strip() for piece in text.split(",")):
        if not part:
            continue
        name, sep, value = part.partition("=")
        name = name.strip()
        if not sep:
            raise ValueError(f"{part!r}: expected type=minutes")
        if name not in config.task_types:
            raise ValueError(f"{name!r} isn't a task type; configured: {', '.join(config.task_types)}")
        if name in targets:
            raise ValueError(f"{name!r} is given twice")
        try:
            minutes = float(value)
        except ValueError:
            raise ValueError(f"{name}: {value.strip()!r} isn't a number") from None
        if minutes < 0 or minutes != minutes or minutes == float("inf"):
            raise ValueError(f"{name}: minutes must be a non-negative number")
        targets[name] = minutes
    if not targets:
        raise ValueError("no targets given")
    return targets


def _format_targets(targets: dict[str, float] | None) -> str:
    if not targets:
        return "none (the configured split)"
    return ", ".join(f"{name}={minutes:g}" for name, minutes in targets.items())


# =====
# RESET
# =====
@dataclass
class Plan:
    participant_id: str
    is_test: bool
    status: str
    state_before: str | None  # None: no conversation yet
    context_keys: list[str]
    sessions_closed: list[tuple[int, str]] = field(default_factory=list)  # (id, task_type)
    sessions_left: int = 0  # already ended (paused, completed, abandoned)
    assignments_expired: list[tuple[int, str, str | None, int]] = field(default_factory=list)
    targets_before: dict[str, float] | None = None
    targets_after: Any = UNCHANGED
    effective_targets: dict[str, float] = field(default_factory=dict)
    recordings_kept: int = 0
    answered_kept: int = 0  # recorded or accepted
    minutes_kept: dict[str, float] = field(default_factory=dict)
    pending_replies: int = 0

    def render(self, applied: bool) -> str:
        verb = "" if applied else "would be "
        lines = [
            f"Participant {self.participant_id} (test participant: {'yes' if self.is_test else 'no'},"
            f" status: {self.status})",
        ]
        if self.state_before is None:
            lines.append(f"  conversation: none yet (already at the start: {INITIAL_STATE})")
        else:
            cleared = f" (context {verb}cleared: {', '.join(self.context_keys)})" if self.context_keys else ""
            lines.append(f"  conversation: {self.state_before} -> {INITIAL_STATE}{cleared}")
        lines.append(
            f"  sessions {verb}closed (-> abandoned): {len(self.sessions_closed)}"
            + "".join(f"  [#{sid} {task_type}]" for sid, task_type in self.sessions_closed)
        )
        if self.sessions_left:
            lines.append(f"  sessions already ended, left as they are: {self.sessions_left}")
        lines.append(
            f"  open assignments {verb}expired (prompts back to the pool): {len(self.assignments_expired)}"
            + "".join(
                f"  [#{aid} {task_type} {prompt_id or '(free speech)'} pos {position}]"
                for aid, task_type, prompt_id, position in self.assignments_expired
            )
        )
        if self.targets_after is UNCHANGED:
            lines.append(f"  targets override: {_format_targets(self.targets_before)} (unchanged)")
        else:
            lines.append(
                f"  targets override: {_format_targets(self.targets_before)} -> {_format_targets(self.targets_after)}"
            )
        total = sum(self.effective_targets.values())
        lines.append(
            "  targets in effect: "
            + ", ".join(f"{name} {minutes:g}" for name, minutes in self.effective_targets.items())
            + f" (total {total:g} min)"
        )
        minutes = sum(self.minutes_kept.values())
        lines.append(
            f"  kept as they are: {self.recordings_kept} recordings, {self.answered_kept} recorded/accepted assignments,"
            f" {minutes:.1f} recorded minutes"
        )
        if self.pending_replies:
            lines.append(
                f"  note: {self.pending_replies} reply(ies) still waiting to be sent; not changed"
                " (they go out unless outside the 24-hour window)"
            )
        return "\n".join(lines)


def reset(
    session_factory: sessionmaker[Session],
    participant_id: str,
    targets: Any = UNCHANGED,
    apply: bool = True,
    config: CollectionConfig | None = None,
    now: datetime | None = None,
) -> Plan:
    """Work out (and with apply, make) the reset, in one transaction.

    targets: UNCHANGED, None (clear the override) or a per-type dict.
    Raises LookupError if there's no such participant.
    """
    config = config or get_collection_config()
    now = now or datetime.now(UTC)
    with session_factory() as db:
        participant = db.scalar(select(Participant).where(Participant.participant_id == participant_id))
        if participant is None:
            raise LookupError(f"no participant {participant_id}")
        # The engine's lock: no turn for this participant runs while we change things.
        lock_conversation(db, participant.phone_number)

        state = db.get(ConversationState, participant_id, with_for_update=True)
        attributes = dict(participant.attributes or {})
        before = attributes.get(TARGETS_KEY)
        plan = Plan(
            participant_id=participant_id,
            is_test=bool(participant.is_test),
            status=participant.status,
            state_before=state.state if state is not None else None,
            context_keys=sorted((state.context or {}).keys()) if state is not None else [],
            targets_before=before if isinstance(before, dict) else None,
            targets_after=targets,
        )

        active = db.scalars(
            select(TaskSession)
            .where(TaskSession.participant_id == participant_id, TaskSession.status == "active")
            .order_by(TaskSession.id)
            .with_for_update()
        ).all()
        plan.sessions_closed = [(s.id, s.task_type) for s in active]
        plan.sessions_left = db.scalar(
            select(func.count(TaskSession.id)).where(
                TaskSession.participant_id == participant_id, TaskSession.status != "active"
            )
        )
        open_assignments = db.scalars(
            select(Assignment)
            .where(Assignment.participant_id == participant_id, Assignment.status == "sent")
            .order_by(Assignment.id)
            .with_for_update()
        ).all()
        plan.assignments_expired = [(a.id, a.prompt_type, a.prompt_id, a.position) for a in open_assignments]
        plan.recordings_kept = db.scalar(
            select(func.count(Recording.id)).where(Recording.participant_id == participant_id)
        )
        plan.answered_kept = db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.participant_id == participant_id, Assignment.status.in_(COUNTED_STATUSES)
            )
        )
        plan.minutes_kept = minutes_recorded(db, participant_id)
        plan.pending_replies = db.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.participant_id == participant_id, OutboundMessage.status == "pending"
            )
        )

        after = dict(attributes)
        if targets is None:
            after.pop(TARGETS_KEY, None)
        elif targets is not UNCHANGED:
            after[TARGETS_KEY] = dict(targets)
        plan.effective_targets = config.targets_for(after)

        if not apply:
            db.rollback()
            return plan

        for task_session in active:
            task_session.status = "abandoned"
            task_session.ended_at = now
        if open_assignments:
            db.execute(
                update(Assignment)
                .where(Assignment.id.in_([a.id for a in open_assignments]))
                .values(status="expired")
                .execution_options(synchronize_session="fetch")
            )
        if state is not None:
            state.state = INITIAL_STATE
            state.context = initial_context()
            flag_modified(state, "context")
        if targets is not UNCHANGED:
            participant.attributes = after
            flag_modified(participant, "attributes")
        db.commit()
        return plan


# ===
# CLI
# ===
def main(argv: list[str] | None = None, confirm=input) -> int:  # noqa: ANN001 - confirm is input() in tests
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("participant_id", help="e.g. P0006")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    parser.add_argument("--clear-targets", action="store_true", help="remove the participant's targets override")
    parser.add_argument("--targets", metavar="TYPE=MINUTES,...", help="set an explicit per-type targets override")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args(argv)

    if args.clear_targets and args.targets is not None:
        parser.error("use either --targets or --clear-targets, not both")
    config = get_collection_config()
    targets: Any = UNCHANGED
    if args.clear_targets:
        targets = None
    elif args.targets is not None:
        try:
            targets = parse_targets(args.targets, config)
        except ValueError as exc:
            parser.error(f"--targets: {exc}")

    factory = get_sync_sessionmaker()
    try:
        plan = reset(factory, args.participant_id, targets, apply=False, config=config)
    except LookupError as exc:
        parser.error(str(exc))

    if args.dry_run:
        print("DRY RUN: nothing changed.")
        print(plan.render(applied=False))
        return 0
    if not args.yes and not plan.is_test:
        print(plan.render(applied=False))
        answer = confirm(f"Reset {plan.participant_id}? Type 'yes' to continue: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing changed.")
            return 1

    # Worked out again inside the transaction that makes the change.
    plan = reset(factory, args.participant_id, targets, apply=True, config=config)
    print(plan.render(applied=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
