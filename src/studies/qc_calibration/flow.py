"""QC calibration dispatch and study-owned state."""

from sqlalchemy import select
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


def prepare_audio(db: Session, enrollment: Enrollment, message: InboundMessage) -> bool:
    """Reserve one message for the current clip before the recording worker commits."""
    state = db.get(CalibrationState, enrollment.id)
    if state is None or state.context.get("phase") != "record":
        return False
    context = state.context
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


def _menu(order: list[str], context: dict) -> tuple[str, dict]:
    config = get_config()
    completed = context["completed"]
    suggested = _suggested(order, completed)
    rows = []
    for code, condition in config.conditions.items():
        count = len(condition.tasks)
        progress = f"{count}/{count} done" if code in completed else f"0/{count} done"
        if code == suggested:
            progress += " · Suggested"
        rows.append({"id": f"cal:{context['token']}:{code}", "title": condition.title, "description": progress})
    return messages()["menu"], {
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


def _render(db: Session, order: list[str], context: dict) -> tuple[str, dict | None]:
    phase = context["phase"]
    if phase == "menu":
        return _menu(order, context)
    if phase == "setup":
        return _buttons(messages()["conditions"][context["condition"]], context["token"], ("ready", "change"))
    if phase == "review":
        return _buttons(messages()["recorded"], context["token"], ("next", "retake"))
    task = _task(context)
    prompt = _prompt(db, task)
    count = len(get_config().conditions[context["condition"]].tasks)
    return messages()[task].format(prompt=prompt.text if prompt else "", slot=context["slot"], total=count), None


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
    db.add(CalibrationClip(
        recording_id=recording.id, enrollment_id=enrollment.id, condition_code=context["condition"],
        task_type=task, task_position=context["slot"], prompt_id=prompt.prompt_id if prompt else None,
        attempt=context["attempt"], suggested_condition=context["suggested"],
        order_position=len(context["completed"]) + 1, fast_qc=fast_qc,
    ))


def save_reserved_clip(
    db: Session, enrollment: Enrollment, message: InboundMessage, recording: Recording,
) -> bool:
    """Finish a stored clip even if the enrollment was paused during audio processing."""
    state = db.get(CalibrationState, enrollment.id)
    if state is None or state.context.get("pending") != message.id or recording.status != "stored":
        return False
    _save_clip(db, enrollment, state.context, recording)
    state.context = {**state.context, "phase": "review", "pending": None, "token": message.id}
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
            "phase": "menu", "completed": [], "token": message.id,
        })
        db.add(state)
    context = dict(state.context)
    phase = context["phase"]
    inbound = get_channel(META).parse_inbound(message.raw_payload)
    prefix = f"cal:{context['token']}:"
    reply_id = inbound.interactive_reply_id or ""
    action = reply_id[len(prefix):] if reply_id.startswith(prefix) else ""
    finished = False
    if context.get("pending") is not None:
        if recording is None or not save_reserved_clip(db, enrollment, message, recording):
            return []
        context = dict(state.context)
    elif phase == "menu" and action in get_config().conditions and action not in context["completed"]:
        context.update(phase="setup", condition=action, suggested=_suggested(order, context["completed"]),
                       slot=1, attempt=1, token=message.id)
    elif phase == "setup" and action in {"ready", "change"}:
        context.update(phase="record" if action == "ready" else "menu", token=message.id)
    elif phase == "review" and action == "retake":
        context.update(phase="record", attempt=context["attempt"] + 1, token=message.id)
    elif phase == "review" and action == "next":
        condition = context["condition"]
        if context["slot"] < len(get_config().conditions[condition].tasks):
            context.update(phase="record", slot=context["slot"] + 1, attempt=1, token=message.id)
        else:
            completed = [*context["completed"], condition]
            finished = len(completed) == len(order)
            context.update(phase="done" if finished else "menu", completed=completed, token=message.id)
            if finished:
                _complete(db, participant, enrollment)
    state.context = context
    body, interactive = (messages()["done"], None) if finished else _render(db, order, context)
    row = OutboundMessage(
        inbound_message_id=message.id, participant_id=participant.participant_id,
        purpose="calibration_done" if finished else "calibration_reply",
        body=body, channel=META, interactive=interactive,
        message_keys=[{"key": STUDY, "enrollment_id": enrollment.id}],
    )
    db.add(row)
    db.flush()
    return [row.id]
