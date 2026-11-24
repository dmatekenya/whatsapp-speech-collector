"""Configuration for the QC calibration study."""

import hashlib
from functools import lru_cache
from random import Random
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from src.config import PROJECT_ROOT, get_settings

STUDY = "qc_calibration"
TASKS = {"sentence", "conversation_starter", "free_speech", "silence"}


class Condition(BaseModel):
    title: str = Field(min_length=1, max_length=24)
    reminder: str = Field(min_length=1, max_length=80, pattern=r"^[^\r\n]+$")
    tasks: list[str] = Field(min_length=1)
    storage_order: list[str] | None = None


class DurationRange(BaseModel):
    min_seconds: int = Field(gt=0)
    max_seconds: int = Field(gt=0)
    aim_max_seconds: int | None = Field(default=None, gt=0)
    guidance: Literal["speech", "silence"] | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "DurationRange":
        if not self.min_seconds <= (self.aim_max_seconds or self.max_seconds) <= self.max_seconds:
            raise ValueError("Expected minimum ≤ suggested maximum ≤ soft maximum")
        return self


class CalibrationConfig(BaseModel):
    enabled: bool = False
    resume_reminder_minutes: int = Field(default=10, gt=0)
    prompts: dict[str, str]
    durations: dict[str, DurationRange]
    conditions: dict[str, Condition]

    @model_validator(mode="after")
    def validate_protocol(self) -> "CalibrationConfig":
        if set(self.conditions) != {f"C{i}" for i in range(1, 11)}:
            raise ValueError("Expected conditions C1 through C10")
        if set(self.prompts) != {"sentence", "conversation_starter"}:
            raise ValueError("Expected fixed sentence and conversation starter prompts")
        if set(self.durations) != TASKS:
            raise ValueError("Expected a duration range for each calibration task")
        for condition in self.conditions.values():
            if not set(condition.tasks) <= TASKS:
                raise ValueError("Unknown calibration task")
            if condition.storage_order is not None and (
                len(set(condition.tasks)) != len(condition.tasks)
                or sorted(condition.storage_order) != sorted(condition.tasks)
            ):
                raise ValueError("Storage order must list each task exactly once")
        return self


@lru_cache
def get_config() -> CalibrationConfig:
    path = PROJECT_ROOT / "config/studies/qc_calibration.yaml"
    config = CalibrationConfig.model_validate(yaml.safe_load(path.read_text())["studies"][STUDY])
    override = get_settings().study_qc_calibration_enabled
    return config if override is None else config.model_copy(update={"enabled": override})


def initial_attributes(participant_id: str, round_name: str) -> dict[str, list[str]]:
    seed = hashlib.sha256(f"{STUDY}:{participant_id}:{round_name}".encode()).digest()
    order = sorted(get_config().conditions)
    Random(seed).shuffle(order)
    return {"condition_order": order}


@lru_cache
def messages() -> dict:
    path = PROJECT_ROOT / "messages/qc_calibration_en.yaml"
    return yaml.safe_load(path.read_text())
