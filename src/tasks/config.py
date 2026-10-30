"""Typed access to config/assignment_settings.yaml (how prompts are assigned, and targets)."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from src.config import PROJECT_ROOT

DEFAULT_PATH = PROJECT_ROOT / "config" / "assignment_settings.yaml"
TOTAL = "total"


class ClipSeconds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "ClipSeconds":
        if self.min > self.max:
            raise ValueError("clip_seconds: min must not exceed max")
        return self


class CollectionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    speakers_per_prompt: dict[str, PositiveInt]
    session_size: PositiveInt
    assignment_expiry_hours: float = Field(gt=0)
    max_retakes_per_prompt: int = Field(ge=0)
    targets_minutes: dict[str, float]
    clip_seconds: ClipSeconds

    @model_validator(mode="after")
    def _targets_match_types(self) -> "CollectionConfig":
        unknown = set(self.targets_minutes) - set(self.speakers_per_prompt) - {TOTAL}
        if unknown:
            raise ValueError(f"targets_minutes has unknown task types: {sorted(unknown)}")
        if any(minutes < 0 for minutes in self.targets_minutes.values()):
            raise ValueError("targets_minutes must not be negative")
        return self

    @property
    def task_types(self) -> list[str]:
        """Task types in config order (they're prompt_type values)."""
        return list(self.speakers_per_prompt)

    def targets_for(self, attributes: dict[str, Any] | None) -> dict[str, float]:
        """Targets for one participant: the defaults, overridden by
        participants.attributes["targets_minutes"] (any subset of keys)."""
        targets = dict(self.targets_minutes)
        override = (attributes or {}).get("targets_minutes") or {}
        for key, value in override.items():
            if key in targets or key in self.speakers_per_prompt:
                targets[key] = float(value)
        return targets


def load_collection_config(path: Path = DEFAULT_PATH) -> CollectionConfig:
    return CollectionConfig.model_validate(yaml.safe_load(path.read_text()) or {})


@lru_cache
def get_collection_config() -> CollectionConfig:
    return load_collection_config()
