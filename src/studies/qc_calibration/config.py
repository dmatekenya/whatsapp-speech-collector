"""Configuration for the QC calibration study."""

import hashlib
from functools import lru_cache
from random import Random

import yaml
from pydantic import BaseModel, Field, model_validator

from src.config import PROJECT_ROOT, get_settings

STUDY = "qc_calibration"
TASKS = {"sentence", "conversation_starter", "free_speech", "silence"}


class Condition(BaseModel):
    title: str = Field(min_length=1, max_length=24)
    tasks: list[str] = Field(min_length=1)


class CalibrationConfig(BaseModel):
    enabled: bool = False
    prompts: dict[str, str]
    conditions: dict[str, Condition]

    @model_validator(mode="after")
    def validate_protocol(self) -> "CalibrationConfig":
        if set(self.conditions) != {f"C{i}" for i in range(1, 11)}:
            raise ValueError("Expected conditions C1 through C10")
        if set(self.prompts) != {"sentence", "conversation_starter"}:
            raise ValueError("Expected fixed sentence and conversation starter prompts")
        for condition in self.conditions.values():
            if not set(condition.tasks) <= TASKS:
                raise ValueError("Unknown calibration task")
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
