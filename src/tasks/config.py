"""Typed access to config/assignment_settings.yaml (how prompts are assigned, and targets)."""

import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator

from src.config import PROJECT_ROOT

DEFAULT_PATH = PROJECT_ROOT / "config" / "assignment_settings.yaml"
PER_PARTICIPANT = "per_participant"
# How far the split may be from adding up to 1.0.
SPLIT_TOLERANCE = 0.001


class CollectionConfigError(ValueError):
    """config/assignment_settings.yaml is invalid (raised when it's loaded, at startup)."""


def round_half(minutes: float) -> float:
    """To the nearest 0.5 minute, halves up (1.25 -> 1.5, 2.75 -> 3.0)."""
    return math.floor(minutes * 2 + 0.5) / 2


class ClipSeconds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "ClipSeconds":
        if self.min > self.max:
            raise ValueError("clip_seconds: min must not exceed max")
        return self


ClipProblem = Literal["no_speech", "low_speech"]


class ClipChecks(BaseModel):
    """clip_checks: is there speech in a clip?

    The worker measures each voice note on its 16 kHz WAV (src/audio/levels.py):
    active_seconds is the time in frame_ms frames louder than active_dbfs. The
    conversation engine applies the rules after the duration check:
    active_seconds below min_active_seconds is no_speech (rejected, retake);
    otherwise an active share of the clip below min_active_ratio is low_speech
    (accepted, flagged for review).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame_ms: int = Field(default=30, ge=10, le=1000)
    active_dbfs: float = Field(default=-45.0, ge=-120, lt=0)
    min_active_seconds: float = Field(default=1.5, ge=0)
    min_active_ratio: float = Field(default=0.20, ge=0, le=1)

    def problem(self, active_seconds: float | None, duration_s: float | None) -> ClipProblem | None:
        """None when the clip wasn't measured (e.g. recordings from before the check)."""
        if active_seconds is None:
            return None
        if active_seconds < self.min_active_seconds:
            return "no_speech"
        if duration_s and active_seconds / duration_s < self.min_active_ratio:
            return "low_speech"
        return None


SpeechType = Literal["scripted", "spontaneous"]
SPEECH_TYPES: tuple[str, ...] = ("scripted", "spontaneous")


AudioPolicy = Literal["accept", "reject", "accept_flagged"]
AUDIO_POLICY_REASONS: dict[str, tuple[str, str]] = {
    # policy -> (qc_status, qc_reason) it sets; accept sets nothing.
    "reject": ("rejected", "not_recorded"),
    "accept_flagged": ("needs_review", "speaker_check_pending"),
}


class AudioOriginOverrides(BaseModel):
    """task_types.<type>.audio_origin: rules for this task type only. An origin
    left out uses the global audio_origin rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recorded: AudioPolicy | None = None
    forwarded: AudioPolicy | None = None
    uploaded: AudioPolicy | None = None
    unknown: AudioPolicy | None = None


class TaskType(BaseModel):
    """One task type. Prompted types send a prompt from the prompt bank (sentence,
    picture, question); unprompted ones (free_speech) just invite a voice note.

    speech_type groups task types for reporting: scripted (read from a text) or
    spontaneous (the participant's own words). It's derived from the task type,
    never stored per recording.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompted: bool
    speech_type: SpeechType
    # How many different participants should record each prompt (prompted types only).
    speakers_per_prompt: PositiveInt | None = None
    # Audio origin rules that differ from the global ones for this type (e.g.
    # forwarded voice notes and audio files accepted, flagged, in free_speech only).
    audio_origin: AudioOriginOverrides | None = None

    @model_validator(mode="after")
    def _speakers_only_when_prompted(self) -> "TaskType":
        if self.prompted and self.speakers_per_prompt is None:
            raise ValueError("a prompted task type needs speakers_per_prompt")
        if not self.prompted and self.speakers_per_prompt is not None:
            raise ValueError("speakers_per_prompt is only for prompted task types (prompted: true)")
        return self


class AudioOriginRules(BaseModel):
    """What to do with a voice note, by where it came from (recordings.audio_origin).

    accept: use it as usual. reject: never use it (qc_status rejected,
    qc_reason not_recorded) and ask for a new recording. accept_flagged: use and
    count it, but mark it for review (qc_status needs_review,
    qc_reason speaker_check_pending).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recorded: AudioPolicy = "accept"
    forwarded: AudioPolicy = "reject"
    uploaded: AudioPolicy = "reject"
    # Twilio doesn't say where audio came from.
    unknown: AudioPolicy = "accept"

    def policy(self, origin: str | None) -> AudioPolicy:
        return getattr(self, origin or "unknown", self.unknown)


class Targets(BaseModel):
    """targets_minutes: minutes per participant, split across the task types."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    per_participant: float = Field(ge=0)
    split: dict[str, float]

    @model_validator(mode="before")
    @classmethod
    def _not_the_old_format(cls, data: Any) -> Any:
        if isinstance(data, dict) and PER_PARTICIPANT not in data and "split" not in data:
            raise ValueError(
                "targets_minutes now takes per_participant (minutes) and split (a share per task type),"
                f" not per-type minutes; found {sorted(data)}"
            )
        return data

    @model_validator(mode="after")
    def _split_valid(self) -> "Targets":
        negative = sorted(t for t, share in self.split.items() if share < 0)
        if negative:
            raise ValueError(f"targets_minutes.split must not be negative: {negative}")
        total = sum(self.split.values())
        if abs(total - 1.0) > SPLIT_TOLERANCE:
            raise ValueError(f"targets_minutes.split must add up to 1.0; it adds up to {total:g}")
        return self


class CollectionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_types: dict[str, TaskType] = Field(min_length=1)
    session_size: PositiveInt
    assignment_expiry_hours: float = Field(gt=0)
    max_retakes_per_prompt: int = Field(ge=0)
    targets_minutes: Targets
    clip_seconds: ClipSeconds
    clip_checks: ClipChecks = ClipChecks()
    # After this long without a message from the participant, their next
    # message starts afresh (welcome back). None: same as assignment_expiry_hours.
    fresh_start_after_hours: float | None = Field(default=None, gt=0)
    # WhatsApp only allows free-form replies within 24 hours of the
    # participant's last message to that number; a little less, for safety.
    reply_window_hours: float = Field(default=23.5, gt=0, le=24)
    audio_origin: AudioOriginRules = AudioOriginRules()

    @model_validator(mode="before")
    @classmethod
    def _not_the_old_format(cls, data: Any) -> Any:
        if isinstance(data, dict) and "speakers_per_prompt" in data:
            raise ValueError(
                "speakers_per_prompt is now set per task type: replace it with a task_types section, e.g."
                " task_types: {sentence: {prompted: true, speakers_per_prompt: 5},"
                " free_speech: {prompted: false}}"
            )
        return data

    @model_validator(mode="after")
    def _split_matches_types(self) -> "CollectionConfig":
        """The split covers exactly the configured task types."""
        split, types = set(self.targets_minutes.split), set(self.task_types)
        if split != types:
            problems = []
            if types - split:
                problems.append(f"missing {sorted(types - split)}")
            if split - types:
                problems.append(f"not in task_types {sorted(split - types)}")
            raise ValueError(
                "targets_minutes.split must list exactly the task types in task_types: "
                + "; ".join(problems)
            )
        return self

    @property
    def fresh_start_hours(self) -> float:
        return self.fresh_start_after_hours or self.assignment_expiry_hours

    # task_types iterates as the type names in config order (they're the
    # sessions.task_type and assignments.prompt_type values).

    @property
    def speakers_per_prompt(self) -> dict[str, int]:
        """Prompted types only."""
        return {name: t.speakers_per_prompt for name, t in self.task_types.items() if t.prompted}

    def speech_type(self, task_type: str) -> str:
        """scripted or spontaneous (from task_types; not stored per recording)."""
        return self.task_type(task_type).speech_type

    def by_speech_type(self, minutes: dict[str, float]) -> dict[str, float]:
        """Per-type minutes (targets or recorded) summed per speech type, scripted first."""
        totals = dict.fromkeys(SPEECH_TYPES, 0.0)
        for task_type, value in minutes.items():
            if task_type in self.task_types:
                totals[self.speech_type(task_type)] += value
        return totals

    def audio_policy(self, origin: str | None, task_type: str | None = None) -> AudioPolicy:
        """The rule for a voice note: the task type's override if it has one for
        this origin, else the global audio_origin rule. No task type (outside a
        session, or not yet matched to a prompt): the global rule."""
        overrides = self.task_types[task_type].audio_origin if task_type in self.task_types else None
        override = getattr(overrides, origin or "unknown", None) if overrides is not None else None
        return override or self.audio_origin.policy(origin)

    def is_prompted(self, task_type: str) -> bool:
        return self.task_type(task_type).prompted

    def task_type(self, task_type: str) -> TaskType:
        if task_type not in self.task_types:
            raise ValueError(f"Unknown task type {task_type!r}; configured: {list(self.task_types)}")
        return self.task_types[task_type]

    def split_targets(self, per_participant: float) -> dict[str, float]:
        """per_participant minutes split across the task types, each rounded to 0.5 min."""
        return {t: round_half(per_participant * self.targets_minutes.split[t]) for t in self.task_types}

    @property
    def default_targets(self) -> dict[str, float]:
        return self.split_targets(self.targets_minutes.per_participant)

    def targets_for(self, attributes: dict[str, Any] | None) -> dict[str, float]:
        """Per-type targets (minutes) for one participant.

        participants.attributes["targets_minutes"] can override them, either as
        {"per_participant": 6} (split as configured) or as explicit per-type
        minutes, e.g. {"sentence": 2, "image": 1, "conversation_starter": 1}
        (used as given; a type left out keeps its default). Other keys are ignored.
        """
        override = (attributes or {}).get("targets_minutes")
        override = override if isinstance(override, dict) else {}
        per_participant = override.get(PER_PARTICIPANT, self.targets_minutes.per_participant)
        targets = self.split_targets(float(per_participant))
        for task_type in self.task_types:
            if task_type in override:
                targets[task_type] = float(override[task_type])
        return targets

    @staticmethod
    def total(targets: dict[str, float]) -> float:
        """The total shown to participants: always the sum of the per-type targets."""
        return sum(targets.values())


def load_collection_config(path: Path = DEFAULT_PATH) -> CollectionConfig:
    """Load and validate the file; CollectionConfigError says what's wrong and where."""
    try:
        return CollectionConfig.model_validate(yaml.safe_load(path.read_text()) or {})
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            problems.append(f"{where}: {message}" if where else message)
        raise CollectionConfigError(f"{path}: " + "; ".join(problems)) from None


@lru_cache
def get_collection_config() -> CollectionConfig:
    return load_collection_config()
