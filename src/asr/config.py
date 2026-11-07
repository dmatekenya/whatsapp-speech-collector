"""Typed access to config/asr_batch.yaml (schedule, Modal app, selection), and the run slots."""

import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
)

from src.config import PROJECT_ROOT

DEFAULT_PATH = PROJECT_ROOT / "config" / "asr_batch.yaml"
# The model keys scripts/modal_asr_app.py knows (its MODELS); a test keeps them in step.
MODEL_KEYS = ("whisper_b", "whisper_v1")
# How long after a slot starts a scheduled run may still begin (cron fires hourly).
SLOT_WINDOW = timedelta(hours=1)
_HH_MM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class AsrConfigError(ValueError):
    """config/asr_batch.yaml is invalid."""


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_per_day: int = Field(ge=0, le=24)
    first_run: str
    timezone: str

    @field_validator("runs_per_day")
    @classmethod
    def _divides_a_day(cls, value: int) -> int:
        if value and 24 % value:
            raise ValueError("runs_per_day must be 0 (paused) or divide 24: 1, 2, 3, 4, 6, 8, 12 or 24")
        return value

    @field_validator("first_run")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not _HH_MM.match(value):
            raise ValueError(f'first_run must be a 24-hour time like "02:00", not {value!r}')
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r} (use an IANA name like Africa/Blantyre)") from None
        return value

    @property
    def paused(self) -> bool:
        return self.runs_per_day == 0

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def slot_times(self) -> list[time]:
        """Local start times of the day's runs, earliest first."""
        if self.paused:
            return []
        hours, minutes = map(int, self.first_run.split(":"))
        step = 24 * 60 // self.runs_per_day
        starts = sorted((hours * 60 + minutes + k * step) % (24 * 60) for k in range(self.runs_per_day))
        return [time(m // 60, m % 60) for m in starts]

    def current_slot(self, now: datetime) -> datetime | None:
        """The slot whose first hour contains now (timezone-aware, in self.timezone), or None."""
        local = now.astimezone(self.tz)
        for day in (local.date() - timedelta(days=1), local.date()):
            for start_time in self.slot_times():
                start = _local(day, start_time, self.tz)
                if start <= local < start + SLOT_WINDOW:
                    return start
        return None


def _local(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz)


class ModalSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    app_name: str = Field(min_length=1)
    class_name: str = Field(min_length=1)
    model: str
    chunk_clips: PositiveInt
    chunk_max_mb: float = Field(gt=0)

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        return check_model_key(value)

    @property
    def chunk_max_bytes(self) -> int:
        return int(self.chunk_max_mb * 1024 * 1024)


class Selection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    include_test: bool = False


class AsrBatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: Schedule
    modal: ModalSettings
    selection: Selection = Selection()
    results_dir: Path

    @property
    def results_path(self) -> Path:
        """results_dir, relative to the project root unless absolute."""
        return self.results_dir if self.results_dir.is_absolute() else PROJECT_ROOT / self.results_dir


def check_model_key(value: str) -> str:
    if value not in MODEL_KEYS:
        raise ValueError(f"unknown model {value!r}; available: {', '.join(MODEL_KEYS)}")
    return value


def load_asr_config(path: Path = DEFAULT_PATH) -> AsrBatchConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return AsrBatchConfig.model_validate(data)
    except FileNotFoundError:
        raise AsrConfigError(f"{path} not found") from None
    except yaml.YAMLError as exc:
        raise AsrConfigError(f"{path} is not valid YAML: {exc}") from None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or '(top level)'}: {error['msg']}" for error in exc.errors()
        )
        raise AsrConfigError(f"{path.name}: {problems}") from None


@lru_cache
def get_asr_config() -> AsrBatchConfig:
    return load_asr_config()
