"""The bot's message catalog: messages/bot_messages_{BOT_LANGUAGE}.yaml.

A message value is a string or a list of variants; render() picks a variant
at random and reports which one (key + index) so it can be recorded. Rendering
is strict: a placeholder without a value is an error, never a silent blank.
validate() checks at startup that every key the engine uses exists and only
uses placeholders the engine supplies (REQUIRED below).
"""

import random
import re
import string
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.config import PROJECT_ROOT, get_settings
from src.tasks.config import get_collection_config

MESSAGES_DIR = PROJECT_ROOT / "messages"
KEYWORD_CATEGORIES = ("greetings", "yes", "later", "instructions", "progress", "help", "stop")
_STRIP = " \t\r\n.,!?;:*_-'\"()"


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rendered:
    key: str
    variant: int | None  # index into a list of variants; None for a plain string
    text: str

    @property
    def record(self) -> dict[str, Any]:
        return {"key": self.key, "variant": self.variant}


# =============================
# KEYS AND PLACEHOLDERS IN USE
# =============================
def _progress_values(task_types: list[str]) -> set[str]:
    return {"total_done", "total_target"} | {
        f"{task_type}_{part}" for task_type in task_types for part in ("done", "target")
    }


def required_keys(task_types: list[str]) -> dict[str, set[str]]:
    """Every key the engine renders, with the placeholders it supplies for it."""
    durations = {"duration", "min_seconds", "max_seconds"}
    totals = {"first_name", "total_done", "total_target"}
    keys: dict[str, set[str]] = {
        "welcome.first_time": {"first_name"},
        "welcome.returning": {"first_name"},
        "progress.none_yet": set(),
        "progress.summary": _progress_values(task_types),
        "progress.target_reached": _progress_values(task_types),
        "instructions.general": set(),
        "instructions.reminder": set(),
        "ready.ask": set(),
        "ready.later": set(),
        "ready.unclear": set(),
        "prompt.ack_prefix": set(),
        "prompt.sentence": {"n", "session_size", "text"},
        "prompt.conversation_starter": {"n", "session_size", "text"},
        "prompt.image_caption": {"n", "session_size"},
        "session.type_target_reached": totals | {"task_name", "next_task_name"},
        "session.type_target_reached_no_next": totals | {"task_name"},
        "session.continue_ask": totals | {"session_size", "next_task_name"},
        "session.paused": set(),
        "session.open_prompt_reminder": set(),
        "stop.confirm": set(),
        "stop.done": set(),
        "stop.cancelled": set(),
        "help": {"support_contact"},
        "unregistered": set(),
        "participant.paused": set(),
        "participant.completed": set(),
        "no_prompts_left": {"task_name"},
    }
    for problem in (
        "text_instead_of_audio",
        "image_instead_of_audio",
        "video_instead_of_audio",
        "document_instead_of_audio",
        "other_instead_of_audio",
        "empty_audio",
        "too_short",
        "too_long",
        "duplicate",
        "retake_limit",
    ):
        keys[f"problems.{problem}"] = durations
    for task_type in task_types:
        keys[f"task_names.{task_type}"] = set()
        keys[f"today.{task_type}"] = set()
        keys[f"instructions.{task_type}"] = durations
    return keys


# =======
# CATALOG
# =======
def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def normalize_reply(text: str) -> str:
    """For keyword matching: lowercase, spaces collapsed, edge punctuation removed."""
    return " ".join(text.lower().split()).strip(_STRIP)


class Catalog:
    def __init__(self, data: dict[str, Any], rng: random.Random | None = None) -> None:
        self.data = data
        self.rng = rng or random.Random()
        self.keywords = {
            category: {normalize_reply(str(word)) for word in data.get("keywords", {}).get(category, [])}
            for category in KEYWORD_CATEGORIES
        }

    @classmethod
    def load(cls, path: Path, rng: random.Random | None = None) -> "Catalog":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, rng)

    def raw(self, key: str) -> str | list[str]:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise CatalogError(f"message key {key!r} is missing")
            node = node[part]
        if isinstance(node, str):
            return node
        if isinstance(node, list) and node and all(isinstance(v, str) for v in node):
            return node
        raise CatalogError(f"message key {key!r} must be a string or a list of strings")

    def render(self, key: str, **values: Any) -> Rendered:
        value = self.raw(key)
        variant = None
        if isinstance(value, list):
            variant = self.rng.randrange(len(value))
            value = value[variant]
        missing = _placeholders(value) - values.keys()
        if missing:
            raise CatalogError(f"message {key!r} needs values for {sorted(missing)}")
        return Rendered(key, variant, value.format(**values).strip())

    def text(self, key: str, **values: Any) -> str:
        return self.render(key, **values).text

    def matches(self, category: str, text: str) -> bool:
        return normalize_reply(text) in self.keywords.get(category, set())

    def validate(self, task_types: list[str]) -> None:
        problems = []
        for key, supplied in required_keys(task_types).items():
            try:
                value = self.raw(key)
            except CatalogError as exc:
                problems.append(str(exc))
                continue
            for variant in value if isinstance(value, list) else [value]:
                unknown = _placeholders(variant) - supplied
                if unknown:
                    problems.append(f"message {key!r} uses unknown placeholders {sorted(unknown)}")
        for category in KEYWORD_CATEGORIES:
            if not self.keywords[category]:
                problems.append(f"keywords.{category} is missing or empty")
        if problems:
            raise CatalogError("message catalog problems:\n  " + "\n  ".join(problems))


def catalog_path(language: str | None = None) -> Path:
    language = language or get_settings().bot_language
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise CatalogError(f"BOT_LANGUAGE must be a language code, not {language!r}")
    return MESSAGES_DIR / f"bot_messages_{language}.yaml"


@lru_cache
def get_catalog() -> Catalog:
    catalog = Catalog.load(catalog_path())
    catalog.validate(get_collection_config().task_types)
    return catalog
