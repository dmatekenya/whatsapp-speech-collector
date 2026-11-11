"""Tier-2 levels: config/qc_tier2.yaml and the weakest-link rule.

Only task types in levelled_task_types get a level (since 2026-11-11: sentences); the others
get none (band None, band_reason "not_applicable"). For those that do, each metric that sets a
level (sets_level: true) gets one from its cut-offs, and the clip's level is the lowest (4 if
none is lower). band_reason lists the metric(s) at that level, sorted by name, e.g. "cer:2";
"all_ok" for level 4. Report-only metrics (sets_level: false) are still computed and stored.
A levelled clip with no level-setting metric (e.g. a sentence without prompt text, so no CER)
also gets none.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.config import PROJECT_ROOT
from src.qc.metrics import METRICS_VERSION
from src.tasks.config import get_collection_config

DEFAULT_PATH = PROJECT_ROOT / "config" / "qc_tier2.yaml"
ALL_OK = "all_ok"
NOT_APPLICABLE = "not_applicable"
# Metrics that can have cut-offs (wer and length_ratio are stored but set no level).
LEVELLED_METRICS = ("cer", "compression_ratio", "words_per_second", "avg_logprob", "snr_db", "speech_ratio", "mean_dbfs")
Level = int


# ======
# CONFIG
# ======
class QcConfigError(ValueError):
    """config/qc_tier2.yaml is missing something or has a wrong value."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Band(_Model):
    level: int = Field(ge=1, le=4)
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> "Band":
        if self.min is None and self.max is None:
            raise ValueError("a band needs min, max or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min {self.min:g} is above max {self.max:g}")
        return self

    def contains(self, value: float) -> bool:
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


class MetricLevels(_Model):
    sets_level: bool = True  # false: computed, stored and reported, but sets no level
    applies_to: list[str] | None = None  # task types; None: all
    bands: list[Band] = Field(min_length=1)
    otherwise: int = Field(ge=1, le=4)

    @model_validator(mode="after")
    def _order(self) -> "MetricLevels":
        levels = [band.level for band in self.bands]
        if levels != sorted(levels, reverse=True) or len(set(levels)) != len(levels):
            raise ValueError(f"bands must go from the highest level down, each level once (got {levels})")
        if self.otherwise >= levels[-1]:
            raise ValueError(f"otherwise ({self.otherwise}) must be below the last band's level ({levels[-1]})")
        return self

    def level(self, value: float) -> Level:
        return next((band.level for band in self.bands if band.contains(value)), self.otherwise)


class WordCountRules(_Model):
    sets_level: bool = True
    empty_level: int = Field(ge=1, le=4)
    short_under: int = Field(gt=0)
    short_level: int = Field(ge=1, le=4)
    short_applies_to: list[str]


class DigitsCap(_Model):
    sets_level: bool = True
    applies_to: list[str]
    max_level: int = Field(ge=1, le=4)


class QcConfig(_Model):
    metrics_version: str
    thresholds_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    # Task types that get a level; clips of other types get none ("not_applicable").
    levelled_task_types: list[str]
    levels: dict[str, MetricLevels]
    word_count: WordCountRules
    has_digits: DigitsCap

    @model_validator(mode="after")
    def _known(self) -> "QcConfig":
        unknown = sorted(set(self.levels) - set(LEVELLED_METRICS))
        if unknown:
            raise ValueError(f"levels: unknown metric(s) {unknown}; known: {list(LEVELLED_METRICS)}")
        if self.metrics_version != METRICS_VERSION:
            raise ValueError(
                f"metrics_version is {self.metrics_version!r} but the code computes version {METRICS_VERSION!r}"
            )
        task_types = set(get_collection_config().task_types)
        named = [*self.levelled_task_types, *self.word_count.short_applies_to, *self.has_digits.applies_to]
        for rules in self.levels.values():
            named += rules.applies_to or []
        wrong = sorted(set(named) - task_types)
        if wrong:
            raise ValueError(f"applies_to: {wrong} not in task_types {sorted(task_types)}")
        return self


def load_qc_config(path: Path = DEFAULT_PATH) -> QcConfig:
    """Read and check the file (read again on every run, so an edit needs no restart)."""
    try:
        return QcConfig.model_validate(yaml.safe_load(path.read_text()) or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(file)'}: {error['msg'].removeprefix('Value error, ')}"
            for error in exc.errors()
        )
        raise QcConfigError(f"{path.name}: {problems}") from None


# ======
# LEVELS
# ======
def metric_levels(metrics: dict[str, Any], task_type: str, config: QcConfig) -> dict[str, Level]:
    """The level each level-setting, applicable, present metric gives (metrics: name -> value or None)."""
    levels: dict[str, Level] = {}
    for name, rules in config.levels.items():
        value = metrics.get(name)
        if not rules.sets_level or value is None or (rules.applies_to is not None and task_type not in rules.applies_to):
            continue
        levels[name] = rules.level(float(value))
    words = metrics.get("word_count")
    rules = config.word_count
    if rules.sets_level and words is not None:
        if words == 0:
            levels["word_count"] = rules.empty_level
        elif words < rules.short_under and task_type in rules.short_applies_to:
            levels["word_count"] = rules.short_level
    cap = config.has_digits
    if cap.sets_level and metrics.get("has_digits") is True and task_type in cap.applies_to:
        levels["has_digits"] = cap.max_level
    return levels


def clip_level(metrics: dict[str, Any], task_type: str, config: QcConfig) -> tuple[Level | None, str]:
    """(band, band_reason) by the weakest link; (None, "not_applicable") for a task type that
    gets no level, or when no level-setting metric applies."""
    if task_type not in config.levelled_task_types:
        return None, NOT_APPLICABLE
    levels = metric_levels(metrics, task_type, config)
    if not levels:
        return None, NOT_APPLICABLE
    lowest = min(levels.values())
    if lowest >= 4:
        return 4, ALL_OK
    return lowest, ",".join(f"{name}:{level}" for name, level in sorted(levels.items()) if level == lowest)
