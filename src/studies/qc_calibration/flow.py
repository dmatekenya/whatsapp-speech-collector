"""QC calibration dispatch and study-owned state."""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.channels.base import META
from src.channels.factory import get_channel
from src.registry.enrollments import MAIN, activate
from src.storage.audio import apply_length_flag
from src.storage.models import (
    CalibrationClip, CalibrationState, Enrollment, InboundMessage, OutboundMessage, Participant, Prompt, Recording,
)
from src.studies.qc_calibration.config import STUDY, get_config, initial_attributes, messages
from src.tasks.config import get_collection_config


def enabled(message: InboundMessage, active: Enrollment | None, received: Enrollment | None) -> bool:
    return (
        message.channel == META and active is not None and received is not None
        and active.id == received.id and active.study == STUDY and get_config().enabled
    )


def _completed_slots(context: dict) -> list[int]:
    return context.get("completed_slots", list(range(1, context["slot"])))


def _restore_c3_sentence(db: Session, state: CalibrationState) -> None:
    """Translate old C3 cursors without changing any recorded clip or attempt."""
    if state.context.get("c3_sentence_added"):
        return
    context = {**state.context, "c3_sentence_added": True}
    paused = dict(context.get("paused", {}))
    if context.get("condition") == "C3" and context["phase"] in {"setup", "record", "review", "reminder"}:
        completed = [slot + 1 for slot in _completed_slots(context)]
        context.update(slot=context["slot"] + 1, completed_slots=completed)
    if "C3" in paused:
        old = paused["C3"]
        paused["C3"] = {**old, "slot": old["slot"] + 1,
                        "completed_slots": [slot + 1 for slot in _completed_slots(old)]}
    if "C3" in context["completed"]:
        context["completed"] = [code for code in context["completed"] if code != "C3"]
        suggested = db.scalar(select(CalibrationClip.suggested_condition).where(
            CalibrationClip.enrollment_id == state.enrollment_id, CalibrationClip.condition_code == "C3",
        ).order_by(CalibrationClip.id).limit(1))
        paused["C3"] = {"slot": 1, "attempt": 1, "resume_phase": "record",
                        "completed_slots": [2, 3], "suggested": suggested or "C3"}
        if context["phase"] == "done":
            context["phase"] = "menu"
    context["paused"] = paused
    state.context = context


def prepare_audio(db: Session, enrollment: Enrollment, message: InboundMessage) -> bool:
    """Reserve one message for the current clip before the recording worker commits."""
    state = db.get(CalibrationState, enrollment.id)
    if state is None:
        return False
    _restore_c3_sentence(db, state)
    context = state.context
    if context.get("resume_phase", context["phase"]) != "record":
        return False
    if context.get("pending") == message.id:
        return True
    if context.get("pending") is not None or message.id <= context["token"]:
        return False
    state.context = {**context, "pending": message.id}
    return True


def release_failed_audio(db: Session, enrollment_id: int, message_id: int) -> None:
    state = db.get(CalibrationState, enrollment_id)
    if state is not None and state.context.get("pending") == message_id:
        state.context = {**state.context, "pending": None}


def _buttons(body: str, token: int, actions: tuple[str, ...]) -> tuple[str, dict]:
    return body, {
        "type": "button",
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": f"cal:{token}:{action}", "title": messages()[action]}}
            for action in actions
        ]},
    }


def _suggested(order: list[str], completed: list[str]) -> str:
    return next(code for code in order if code not in completed)


def _progress(order: list[str], context: dict, completed_condition: str | None) -> str:
    done = len(set(order).intersection(context["completed"]))
    progress = messages()["progress"].format(done=done, total=len(order))
    if completed_condition is not None:
        return messages()["condition_done"].format(code=completed_condition, progress=progress)
    return progress


def _menu(order: list[str], context: dict, completed_condition: str | None) -> tuple[str, dict]:
    config = get_config()
    completed = context["completed"]
    suggested = _suggested(order, completed)
    rows = []
    for code, condition in config.conditions.items():
        if code not in order:
            continue
        count = len(condition.tasks)
        paused = context.get("paused", {}).get(code)
        done = count if code in completed else (len(_completed_slots(paused)) if paused else 0)
        progress = f"{done}/{count} done"
        if paused and paused["resume_phase"] == "review":
            progress += " · Clip saved"
        if code == suggested:
            progress += " · Suggested"
        rows.append({"id": f"cal:{context['token']}:{code}", "title": condition.title, "description": progress})
    progress = _progress(order, context, completed_condition)
    return f"{progress}\n\n{messages()['menu']}", {
        "type": "list",
        "action": {"button": messages()["menu_button"], "sections": [{"rows": rows}]},
    }


def _task(context: dict) -> str:
    return get_config().conditions[context["condition"]].tasks[context["slot"] - 1]


def _prompt(db: Session, task: str) -> Prompt | None:
    prompt_id = get_config().prompts.get(task)
    if prompt_id is None:
        return None
    prompt = db.scalar(select(Prompt).where(Prompt.prompt_id == prompt_id, Prompt.prompt_type == task))
    if prompt is None or not prompt.text:
        raise ValueError(f"Missing calibration {task} prompt {prompt_id}")
    return prompt


def _task_header(context: dict) -> str:
    code = context["condition"]
    condition = get_config().conditions[code]
    return messages()["task_header"].format(
        code=code, name=condition.title, reminder=condition.reminder, slot=context["slot"], total=len(condition.tasks),
    )


def _task_body(db: Session, context: dict) -> str:
    task = _task(context)
    prompt = _prompt(db, task)
    parts = [f"*{messages()['instructions'][task]}*"]
    if prompt is not None:
        parts.append("> " + " ".join(prompt.text.splitlines()))
    limits = get_config().durations[task]
    if limits.guidance:
        catalog = messages()
        cap = (
            catalog["duration_cap_minute"] if limits.max_seconds == 60
            else catalog["duration_cap_seconds"].format(seconds=limits.max_seconds)
        )
        guidance = catalog["duration_guidance"][limits.guidance].format(
            minimum=limits.min_seconds, maximum=limits.aim_max_seconds or limits.max_seconds, cap=cap,
        )
        parts.append(f"_{guidance}_")
    return "\n\n".join(parts)


def _duration_feedback(context: dict, recording: Recording | None) -> str | None:
    if recording is None or recording.duration_s is None:
        return None
    duration = recording.duration_s
    limits = get_config().durations[_task(context)]
    if limits.min_seconds <= duration <= limits.max_seconds:
        return None
    seconds = f"{duration:.2f}".rstrip("0").rstrip(".")
    return messages()["duration_feedback"].format(
        seconds=seconds, unit="second" if seconds == "1" else "seconds",
        minimum=limits.min_seconds, maximum=limits.aim_max_seconds or limits.max_seconds,
    )


def _render(
    db: Session, order: list[str], context: dict, *, saved_clip: Recording | None, resumed: bool,
    completed_condition: str | None,
) -> tuple[str, dict | None]:
    phase = context["phase"]
    if phase == "menu":
        return _menu(order, context, completed_condition)
    feedback = _duration_feedback(context, saved_clip)
    header = _task_header(context)
    token = context["token"]
    if phase in {"setup", "reminder"}:
        instruction = messages()["conditions"][context["condition"]]
        if phase == "setup":
            return _buttons(f"{header}\n{instruction}", token, ("ready", "change"))
        if saved_clip is not None:
            instruction = f"{instruction}\n\n{messages()['saved_after_break']}"
            if feedback:
                instruction = f"{instruction} {feedback}"
        return _buttons(f"{header}\n{instruction}", token, ("continue", "change"))
    if phase == "review":
        if resumed:
            return _buttons(f"{header}\n\n{_task_body(db, context)}", token, ("next", "retake"))
        if feedback:
            body = f"{messages()['saved_after_break']} {feedback}"
            return _buttons(f"{header}\n{body}", token, ("retake", "next"))
        return _buttons(f"{header}\n{messages()['recorded']}", token, ("next", "retake"))
    return f"{header}\n\n{_task_body(db, context)}", None


def _save_clip(db: Session, enrollment: Enrollment, context: dict, recording: Recording) -> None:
    task = _task(context)
    prompt = _prompt(db, task)
    config = get_collection_config()
    apply_length_flag(recording, task, config)
    duration = recording.duration_s
    fast_qc = {
        "duration_s": duration,
        "active_seconds": recording.active_seconds,
        "mean_dbfs": recording.mean_dbfs,
        "length_flag": recording.length_flag,
        "audio_origin": recording.audio_origin,
        "origin_policy": config.audio_policy(recording.audio_origin, task),
        "below_minimum": duration is not None and duration < config.clip_limits.min_seconds,
        "above_maximum": duration is not None and duration > config.clip_limits.max_seconds(task, recording.audio_origin),
        "speech_problem": config.clip_checks.problem(recording.active_seconds, duration),
        "duplicate": recording.duplicate_of_recording_id is not None,
    }
    positions = dict(db.execute(
        select(CalibrationClip.condition_code, func.max(CalibrationClip.order_position))
        .where(CalibrationClip.enrollment_id == enrollment.id)
        .group_by(CalibrationClip.condition_code)
    ).all())
    position = positions.get(context["condition"], max(positions.values(), default=0) + 1)
    condition = get_config().conditions[context["condition"]]
    stored_slot = condition.storage_order.index(task) + 1 if condition.storage_order else context["slot"]
    db.add(CalibrationClip(
        recording_id=recording.id, enrollment_id=enrollment.id, condition_code=context["condition"],
        task_type=task, task_position=stored_slot, prompt_id=prompt.prompt_id if prompt else None,
        attempt=context["attempt"], suggested_condition=context["suggested"],
        order_position=position, fast_qc=fast_qc,
    ))


def save_reserved_clip(
    db: Session, enrollment: Enrollment, message: InboundMessage, recording: Recording,
) -> bool:
    """Finish a stored clip even if the enrollment was paused during audio processing."""
    state = db.get(CalibrationState, enrollment.id)
    if state is None or state.context.get("pending") != message.id or recording.status != "stored":
        return False
    _restore_c3_sentence(db, state)
    _save_clip(db, enrollment, state.context, recording)
    context = {**state.context, "pending": None, "token": message.id}
    if context["phase"] == "reminder":
        context["resume_phase"] = "review"
    else:
        context["phase"] = "review"
    state.context = context
    return True


def _complete(db: Session, participant: Participant, enrollment: Enrollment) -> None:
    enrollment.status = "completed"
    db.flush()
    paused_main = db.scalar(select(Enrollment).where(
        Enrollment.participant_id == participant.participant_id, Enrollment.study == MAIN,
        Enrollment.status == "paused",
    ).order_by(Enrollment.updated_at.desc(), Enrollment.id.desc()).limit(1))
    if paused_main is not None:
        activate(db, participant, paused_main)


def _last_activity(db: Session, context: dict) -> datetime | None:
    last = context.get("last_activity_at")
    if last:
        return datetime.fromisoformat(last)
    # Turns saved by older releases have a transition token pointing at an inbound message.
    return db.scalar(select(InboundMessage.received_at).where(InboundMessage.id == context["token"]))


def _pause_condition(context: dict) -> None:
    paused = dict(context.get("paused", {}))
    paused[context["condition"]] = {
        **{key: context[key] for key in ("slot", "attempt", "suggested", "resume_phase")},
        "completed_slots": _completed_slots(context),
    }
    context["paused"] = paused


def respond(
    db: Session, participant: Participant, enrollment: Enrollment, message: InboundMessage,
    recording: Recording | None,
) -> list[int]:
    """Advance a study turn and write its outbox row in the same transaction."""
    order = enrollment.attributes.get("condition_order")
    if not order:
        enrollment.attributes = {**enrollment.attributes, **initial_attributes(participant.participant_id, enrollment.round)}
        order = enrollment.attributes["condition_order"]
    state = db.get(CalibrationState, enrollment.id)
    if state is None:
        state = CalibrationState(enrollment_id=enrollment.id, context={
            "phase": "menu", "completed": [], "token": message.id, "c3_sentence_added": True,
        })
        db.add(state)
    _restore_c3_sentence(db, state)
    context = dict(state.context)
    phase = context["phase"]
    inbound = get_channel(META).parse_inbound(message.raw_payload)
    prefix = f"cal:{context['token']}:"
    reply_id = inbound.interactive_reply_id or ""
    action = reply_id[len(prefix):] if reply_id.startswith(prefix) else ""
    last_activity = _last_activity(db, context)
    overdue = (
        phase in {"setup", "record", "review"} and last_activity is not None
        and message.received_at - last_activity > timedelta(minutes=get_config().resume_reminder_minutes)
    )
    finished = resumed = False
    saved_clip = completed_condition = None
    if context.get("pending") is not None:
        if recording is None or not save_reserved_clip(db, enrollment, message, recording):
            return []
        context = dict(state.context)
        saved_clip = recording
    if overdue:
        context.update(phase="reminder", resume_phase=context["phase"], token=message.id)
    elif saved_clip is not None:
        pass  # The reserved recording already moved the turn to review.
    elif phase == "reminder" and action in {"continue", "change"}:
        if action == "change":
            _pause_condition(context)
            context["phase"] = "menu"
        else:
            context["phase"] = "review" if context["resume_phase"] == "review" else "record"
            resumed = True
        del context["resume_phase"]
        context["token"] = message.id
    elif phase == "menu" and action in order and action not in context["completed"]:
        paused = dict(context.get("paused", {}))
        saved = paused.pop(action, None)
        if saved is None:
            context.update(phase="setup", suggested=_suggested(order, context["completed"]),
                           slot=1, attempt=1, completed_slots=[])
        else:
            context.update(saved, phase="reminder")
        context.update(condition=action, paused=paused, token=message.id)
    elif phase == "setup" and action in {"ready", "change"}:
        context.update(phase="record" if action == "ready" else "menu", token=message.id)
    elif phase == "review" and action == "retake":
        context.update(phase="record", attempt=context["attempt"] + 1, token=message.id)
    elif phase == "review" and action == "next":
        condition = context["condition"]
        completed_slots = [*_completed_slots(context), context["slot"]]
        slots = range(1, len(get_config().conditions[condition].tasks) + 1)
        next_slot = next((slot for slot in slots if slot not in completed_slots), None)
        if next_slot is not None:
            context.update(phase="record", slot=next_slot, attempt=1, token=message.id,
                           completed_slots=completed_slots)
        else:
            completed = [*context["completed"], condition]
            completed_condition = condition
            finished = all(code in completed for code in order)
            context.update(phase="done" if finished else "menu", completed=completed, token=message.id)
            if finished:
                _complete(db, participant, enrollment)
    context["last_activity_at"] = max(last_activity or message.received_at, message.received_at).isoformat()
    state.context = context
    if finished:
        progress = _progress(order, context, completed_condition)
        body, interactive = f"{progress}\n\n{messages()['done']}", None
    else:
        body, interactive = _render(
            db, order, context, saved_clip=saved_clip, resumed=resumed, completed_condition=completed_condition,
        )
    rows = [OutboundMessage(
        inbound_message_id=message.id, participant_id=participant.participant_id,
        purpose="calibration_done" if finished else "calibration_reply",
        body=body, channel=META, interactive=interactive,
        message_keys=[{"key": STUDY, "enrollment_id": enrollment.id}],
    )]
    if resumed and interactive and len(body) > 1024:
        # Send a long resumed prompt as text, then offer the review buttons separately.
        rows[0].interactive = None
        review_body, buttons = _render(
            db, order, context, saved_clip=None, resumed=False, completed_condition=None,
        )
        rows.append(OutboundMessage(
            inbound_message_id=message.id, participant_id=participant.participant_id,
            purpose="calibration_review", sequence=2, body=review_body, channel=META, interactive=buttons,
            message_keys=[{"key": STUDY, "enrollment_id": enrollment.id}],
        ))
    db.add_all(rows)
    db.flush()
    return [row.id for row in rows]
