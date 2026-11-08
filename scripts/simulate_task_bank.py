"""Simulate the task bank with fake participants, then roll everything back.

    uv run python -m scripts.simulate_task_bank
    uv run python -m scripts.simulate_task_bank --participants 30 --seed 3
    uv run python -m scripts.simulate_task_bank --per-participant 6

Targets come from config/assignment_settings.yaml (per_participant x split,
rounded to 0.5 min); --per-participant tries another total with the same split.
Unprompted types (free_speech) run sessions of invitations, with no prompts
or coverage; their minutes count toward their own target like any other type.
Each task type is shown with its speech_type (scripted or spontaneous, from
the config), and minutes are also summed per speech type.

Runs in one transaction on a throwaway database (speech_collector_test by
default) that is always rolled back: nothing is kept. Your real prompt bank is
read (read-only) and copied in, so coverage reflects the real prompts; the
participants and recordings are fake. A fake clock lets ignored prompts
expire. The real database's participants and recordings are never touched.
"""

import argparse
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from src.config import get_settings
from src.storage.models import Assignment, InboundMessage, Participant, Prompt, Recording
from src.tasks.bank import (
    HOLDING_STATUSES,
    assign_next_prompt,
    choose_task_type,
    end_session,
    mark_sent,
    minutes_recorded,
    handle_response,
    set_assignment_outcome,
    start_session,
)
from src.tasks.config import CollectionConfig, get_collection_config

START = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
PROMPT_COLUMNS = ("prompt_id", "prompt_type", "text", "normalized_text", "flags", "active", "source_file")
SIM_TABLES = (
    "outbound_messages, conversation_state, assignments, sessions, recordings,"
    " inbound_messages, participants, prompts"
)


@dataclass
class Behaviour:
    answer: float = 0.80  # answers a prompt; otherwise ignores it (it expires)
    reject: float = 0.10  # QC rejects an answered prompt
    rounds: int = 40  # each round, every participant may do one session
    hours_between_rounds: float = 8.0
    minutes_per_prompt: float = 2.0


@dataclass
class Outcome:
    sessions: int = 0
    stopped: str = "rounds ran out"
    statuses: Counter[str] = field(default_factory=Counter)


# =====
# SETUP
# =====
def _database_url(database: str) -> URL:
    return get_settings().sync_database_url.set(database=database)


def _copy_prompt_bank(sim: Session) -> int:
    """Copy the real prompt bank into the simulation, reading it read-only."""
    real = create_engine(get_settings().sync_database_url)
    with real.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        rows = connection.execute(
            select(*(getattr(Prompt, column) for column in PROMPT_COLUMNS))
        ).mappings().all()
    real.dispose()
    sim.execute(Prompt.__table__.insert(), [dict(row) for row in rows])
    return len(rows)


def _add_fake_participants(sim: Session, count: int) -> list[str]:
    ids = [f"SIM{n:03d}" for n in range(1, count + 1)]
    sim.add_all(
        Participant(
            participant_id=participant_id,
            name=f"Simulated {participant_id}",
            phone_number=f"+1555000{n:04d}",  # fictional range, never real
            status="active",
            is_test=True,
            batch="simulation",
            enrolled_on=START.date(),
            source_file="simulation",
        )
        for n, participant_id in enumerate(ids, start=1)
    )
    sim.flush()
    return ids


def _fake_recording(sim: Session, participant_id: str, seconds: float, n: int) -> int:
    message = InboundMessage(
        message_sid=f"MMSIM{n:027d}",
        sender="+15550000000",
        message_type="audio",
        raw_payload={},
        status="processed",
        participant_id=participant_id,
    )
    sim.add(message)
    sim.flush()
    recording = Recording(
        inbound_message_id=message.id, participant_id=participant_id, status="stored", duration_s=seconds
    )
    sim.add(recording)
    sim.flush()
    return recording.id


# ==========
# SIMULATION
# ==========
def _run_session(
    sim: Session,
    participant_id: str,
    task_type: str,
    now: datetime,
    rng: random.Random,
    behaviour: Behaviour,
    config: CollectionConfig,
    counter: Counter[str],
) -> tuple[datetime, str | None]:
    """One session; returns the advanced clock and, if the session ended early
    because its type's target was met, the next task type offered (else None)."""
    task_session = start_session(sim, participant_id, task_type, now, config)
    sent = 0
    while (assignment := assign_next_prompt(sim, task_session, now, config)) is not None:
        counter["sid"] += 1
        mark_sent(sim, assignment.id, f"SMSIM{counter['sid']:027d}", now, config)
        sent += 1
        now += timedelta(minutes=behaviour.minutes_per_prompt)
        if rng.random() >= behaviour.answer:
            continue  # ignored: it expires later
        counter["recording"] += 1
        # Typical clips: from the minimum up to the long-clip flag (longer ones are allowed).
        limits = config.clip_limits
        seconds = rng.uniform(limits.min_seconds, limits.default.flag_over)
        recording_id = _fake_recording(sim, participant_id, seconds, counter["recording"])
        result = handle_response(sim, assignment.id, recording_id, now, config)
        # QC comes later in reality; a rejection lowers minutes again, and the
        # type is offered again in a later session.
        set_assignment_outcome(sim, assignment.id, accepted=rng.random() >= behaviour.reject)
        if result.session_ended:
            counter["ended_early"] += 1
            return now, result.next_task_type
    # Ran to session_size (or out of prompts) without meeting the target.
    end_session(sim, task_session.id, "completed" if sent else "abandoned", now)
    return now, None


def _stop_reason(sim: Session, participant_id: str, config: CollectionConfig) -> str:
    done = minutes_recorded(sim, participant_id)
    targets = config.default_targets  # simulated participants have no overrides
    unmet = [t for t in config.task_types if done[t] < targets[t]]
    return "all type targets met" if not unmet else "no prompts for " + ", ".join(unmet)


def simulate(
    sim: Session, participants: list[str], rng: random.Random, behaviour: Behaviour, config: CollectionConfig
) -> tuple[dict[str, Outcome], datetime, Counter[str]]:
    outcomes = {participant_id: Outcome() for participant_id in participants}
    finished: set[str] = set()
    counter: Counter[str] = Counter()
    now = START
    for _ in range(behaviour.rounds):
        order = [p for p in participants if p not in finished]
        if not order:
            break
        rng.shuffle(order)
        for participant_id in order:
            task_type = choose_task_type(sim, participant_id, now, config)
            if task_type is None:
                finished.add(participant_id)
                outcomes[participant_id].stopped = _stop_reason(sim, participant_id, config)
                continue
            # When a session ends early, the participant takes the next type
            # offered straight away; otherwise they pause until the next round.
            while task_type is not None:
                now, task_type = _run_session(
                    sim, participant_id, task_type, now, rng, behaviour, config, counter
                )
                outcomes[participant_id].sessions += 1
        now += timedelta(hours=behaviour.hours_between_rounds)
    for participant_id, status, count in sim.execute(
        select(Assignment.participant_id, Assignment.status, func.count()).group_by(
            Assignment.participant_id, Assignment.status
        )
    ):
        outcomes[participant_id].statuses[status] = count
    return outcomes, now, counter


# ======
# REPORT
# ======
def report(
    sim: Session,
    outcomes: dict[str, Outcome],
    now: datetime,
    config: CollectionConfig,
    prompt_count: int,
    counter: Counter[str],
) -> str:
    lines = [f"TASK BANK SIMULATION: {len(outcomes)} participants, {prompt_count} prompts (copied)"]
    lines.append(f"  fake clock: {START:%Y-%m-%d %H:%M} -> {now:%Y-%m-%d %H:%M} UTC")

    holding = (Assignment.status.in_(HOLDING_STATUSES)) | (
        (Assignment.status == "sent") & (Assignment.expires_at > now)
    )
    coverage = (
        select(Prompt.prompt_type, Prompt.prompt_id, func.count(Assignment.id).filter(holding))
        .outerjoin(Assignment, Assignment.prompt_id == Prompt.prompt_id)
        .where(Prompt.active.is_(True))
        .group_by(Prompt.prompt_type, Prompt.prompt_id)
    )
    by_type: dict[str, Counter[int]] = defaultdict(Counter)
    for prompt_type, _, covered in sim.execute(coverage):
        by_type[prompt_type][covered] += 1

    lines.append("")
    lines.append("  prompts per coverage level (accepted + recorded + unexpired sent):")
    lines.append(f"    {'task_type':<21} {'speech_type':<11}")
    for task_type in config.task_types:
        speech = config.speech_type(task_type)
        if not config.is_prompted(task_type):
            invitations = sim.scalar(
                select(func.count(Assignment.id)).where(Assignment.prompt_type == task_type)
            )
            lines.append(
                f"    {task_type:<21} {speech:<11} unprompted: {invitations} invitations to talk (no prompts, no coverage)"
            )
            continue
        limit = config.speakers_per_prompt[task_type]
        levels = by_type.get(task_type, Counter())
        cells = "  ".join(f"{level}:{levels.get(level, 0):>4}" for level in range(limit + 1))
        over = sum(count for level, count in levels.items() if level > limit)
        top = max(levels, default=0)
        low = min(levels, default=0)
        lines.append(
            f"    {task_type:<21} {speech:<11} {cells}   min {low} / max {top} (limit {limit})"
            f"{'  OVER LIMIT: ' + str(over) if over else ''}"
        )

    repeats = sim.scalar(
        select(func.count()).select_from(
            select(Assignment.participant_id, Assignment.prompt_id)
            .where(Assignment.prompt_id.is_not(None))  # free_speech invitations have no prompt
            .group_by(Assignment.participant_id, Assignment.prompt_id)
            .having(func.count() > 1)
            .subquery()
        )
    )
    lines.append("")
    lines.append(f"  repeated prompts per participant: {repeats}")
    totals: Counter[str] = Counter()
    for outcome in outcomes.values():
        totals.update(outcome.statuses)
    lines.append("  assignments: " + ", ".join(f"{s} {totals[s]}" for s in sorted(totals)))

    lines.append(f"  sessions ended early (type target met): {counter['ended_early']}")

    lines.append("")
    targets = config.default_targets  # simulated participants have no overrides
    types = config.task_types
    split = config.targets_minutes.split
    short = {t: t.replace("conversation_starter", "conversation")[:12] for t in types}
    lines.append(
        f"  minutes recorded vs target ({config.targets_minutes.per_participant:g} per participant split "
        + ", ".join(f"{t} ({config.speech_type(t)}) {split[t]:g} = {targets[t]:g}" for t in types)
        + f"; total {config.total(targets):g}):"
    )
    lines.append(
        f"    {'participant':<11}  "
        + "  ".join(f"{short[t]:>12}" for t in types)
        + f"  {'total':>6}  overshoot ({' / '.join(short[t] for t in types)})  sessions  stopped"
    )
    overshoot_all: dict[str, list[float]] = defaultdict(list)
    for participant_id, outcome in outcomes.items():
        done = minutes_recorded(sim, participant_id)
        cells = "  ".join(
            f"{done[t]:>5.1f}/{targets[t]:<4g}{'*' if done[t] >= targets[t] else ' '}".rjust(12)
            for t in types
        )
        over = {t: max(0.0, done[t] - targets[t]) for t in types}
        for t in types:
            overshoot_all[t].append(over[t])
        over_cells = " / ".join(f"+{over[t]:.1f}" for t in types)
        width = len(f"overshoot ({' / '.join(short[t] for t in types)})")
        lines.append(
            f"    {participant_id:<11}  {cells}  {sum(done.values()):>6.1f}  {over_cells:<{width}}"
            f"  {outcome.sessions:>8}  {outcome.stopped}"
        )
    lines.append("    (* = type target met; overshoot = minutes over each type's target)")
    lines.append(
        "  mean overshoot: "
        + ", ".join(f"{t} +{sum(v) / len(v):.2f} min (max +{max(v):.2f})" for t, v in overshoot_all.items())
    )
    lines.extend(_speech_type_summary(sim, outcomes, config, targets))
    return "\n".join(lines)


def _speech_type_summary(
    sim: Session, outcomes: dict[str, Outcome], config: CollectionConfig, targets: dict[str, float]
) -> list[str]:
    """Scripted vs spontaneous minutes (speech_type is derived from task_type)."""
    goal = config.by_speech_type(targets)
    total_goal = sum(goal.values()) or 1.0
    lines = [
        "",
        "  minutes by speech type (target "
        + ", ".join(f"{name} {minutes:g} = {minutes / total_goal:.0%}" for name, minutes in goal.items())
        + "):",
        f"    {'participant':<11}  " + "  ".join(f"{name:>17}" for name in goal) + "  spontaneous share",
    ]
    overall = dict.fromkeys(goal, 0.0)
    for participant_id in outcomes:
        done = config.by_speech_type(minutes_recorded(sim, participant_id))
        for name, minutes in done.items():
            overall[name] += minutes
        share = done["spontaneous"] / (sum(done.values()) or 1.0)
        cells = "  ".join(f"{done[name]:>8.1f}/{goal[name]:<5g}".rjust(17) for name in goal)
        lines.append(f"    {participant_id:<11}  {cells}  {share:>17.0%}")
    recorded = sum(overall.values()) or 1.0
    lines.append(
        "    all participants: "
        + ", ".join(f"{name} {minutes:.1f} min ({minutes / recorded:.0%})" for name, minutes in overall.items())
    )
    return lines


# ===
# CLI
# ===
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--participants", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--database", default="speech_collector_test", help="throwaway database to use")
    parser.add_argument("--per-participant", type=float, help="minutes per participant (default: from the config)")
    args = parser.parse_args()

    if args.database == get_settings().postgres_db:
        parser.error("refusing to simulate in the real database; use a throwaway one")
    engine = create_engine(_database_url(args.database))
    config = get_collection_config()
    if args.per_participant is not None:
        if args.per_participant < 0:
            parser.error("--per-participant must not be negative")
        targets = config.targets_minutes.model_copy(update={"per_participant": args.per_participant})
        config = config.model_copy(update={"targets_minutes": targets})
    rng = random.Random(args.seed)

    with engine.connect() as connection:
        if connection.scalar(text("SELECT to_regclass('assignments')")) is None:
            sys.exit(
                f"{args.database} has no task bank tables; run "
                f"POSTGRES_DB={args.database} uv run alembic upgrade head"
            )
        connection.rollback()  # end the check's implicit transaction
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as sim:
                sim.execute(text(f"TRUNCATE {SIM_TABLES} RESTART IDENTITY"))  # rolled back too
                prompt_count = _copy_prompt_bank(sim)
                participants = _add_fake_participants(sim, args.participants)
                outcomes, now, counter = simulate(sim, participants, rng, Behaviour(), config)
                print(report(sim, outcomes, now, config, prompt_count, counter))
        finally:
            transaction.rollback()  # always: nothing from the simulation is kept
    engine.dispose()


if __name__ == "__main__":
    main()
