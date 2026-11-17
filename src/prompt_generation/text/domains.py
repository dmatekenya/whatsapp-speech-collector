"""Stage 3: suggest one domain per kept sentence with a text-only model via OpenRouter."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.config import PROJECT_ROOT
from src.prompt_generation.picture_scoring import OpenRouterClient, ScoringConfig, SpendTracker
from src.prompt_generation.text.sentences import (
    DEFAULT_CONFIG,
    merge_domain_scores,
    read_domain_scores,
    write_domain_scores,
    write_kept,
)

DOMAINS_PATH_KEY = "domains_config"
UNCLEAR = "unclear"
UNSCORED = "unscored"
MODEL_SOURCE = "model"
MAX_LOGGED_ERRORS = 20
RETRY_NOTE = (
    "Reply with the JSON array only. Use one of the listed domain keys, or \"unclear\", "
    "for every sentence, and give every id exactly once."
)


# ========
# SETTINGS
# ========


@dataclass(frozen=True)
class DomainSettings:
    scoring: ScoringConfig
    model: str
    batch_size: int
    max_calls: int
    sentences_root: Path
    system_prompt: str

    @property
    def domain_keys(self) -> list[str]:
        return [d["key"] for d in self.scoring.domains]


def load_domain_settings(path: Path = DEFAULT_CONFIG) -> DomainSettings:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw["backend"] != "openrouter":
        raise ValueError("Only the 'openrouter' backend is implemented")
    spec = yaml.safe_load((PROJECT_ROOT / raw[DOMAINS_PATH_KEY]).read_text(encoding="utf-8")) or {}
    domains = [dict(item) for item in spec["domains"]]
    boundary_rules = [str(rule) for rule in spec.get("boundary_rules", [])]
    system_prompt = build_system_prompt(str(spec["instructions"]), boundary_rules, domains)
    scoring = ScoringConfig(
        base_url=str(raw["base_url"]).rstrip("/"),
        api_key_env=str(raw["api_key_env"]),
        models=[str(raw["model"])],
        larger_models=[],
        max_spend_usd=float(raw["max_spend_usd"]),
        max_calls_per_model=int(raw["max_calls"]),
        concurrency=1,
        temperature=float(raw["temperature"]),
        max_tokens=int(raw["max_tokens"]),
        timeout_seconds=float(raw["timeout_seconds"]),
        max_retries=int(raw["max_retries"]),
        include_metadata=False,
        instructions=system_prompt,
        domains=domains,
    )
    return DomainSettings(
        scoring=scoring,
        model=str(raw["model"]),
        batch_size=int(raw["batch_size"]),
        max_calls=int(raw["max_calls"]),
        sentences_root=PROJECT_ROOT / raw["sentences_root"],
        system_prompt=system_prompt,
    )


def build_system_prompt(instructions: str, boundary_rules: list[str], domains: list[dict[str, str]]) -> str:
    lines = [instructions.strip(), "", "Boundary rules:"]
    lines += [f"- {rule}" for rule in boundary_rules]
    lines += ["", "Domains:"]
    lines += [f"- {d['key']}: {d['scope']} Example: {d['example']}" for d in domains]
    lines += ["", f"Use \"{UNCLEAR}\" when no domain fits."]
    return "\n".join(lines)


# ======
# PROMPT
# ======


def build_messages(settings: DomainSettings, batch: list[dict[str, str]]) -> list[dict[str, str]]:
    payload = [{"id": row["sentence_id"], "text": row["text"]} for row in batch]
    user = (
        "Classify each sentence. Reply with a JSON array, one object per sentence, in this form:\n"
        '[{"id": "<id>", "domain": "<domain key or unclear>", "confidence": <0-1>}]\n\n'
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    return [
        {"role": "system", "content": settings.system_prompt},
        {"role": "user", "content": user},
    ]


# =====
# REPLY
# =====


def parse_batch_reply(text: str, ids: list[str], domain_keys: set[str]) -> dict[str, tuple[str, float]]:
    """Strictly validate a batch reply; raise ValueError if any part of it is wrong."""
    try:
        items = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply is not JSON: {exc}") from exc
    if not isinstance(items, list):
        raise ValueError("reply is not a JSON array")

    allowed_domains = domain_keys | {UNCLEAR}
    expected_ids = set(ids)
    result: dict[str, tuple[str, float]] = {}
    for item in items:
        if not isinstance(item, dict) or not {"id", "domain", "confidence"} <= set(item):
            raise ValueError(f"item lacks id, domain or confidence: {item!r}"[:200])
        sentence_id, domain = item["id"], item["domain"]
        if sentence_id not in expected_ids:
            raise ValueError(f"unknown id {sentence_id!r}")
        if sentence_id in result:
            raise ValueError(f"duplicate id {sentence_id!r}")
        if domain not in allowed_domains:
            raise ValueError(f"invalid domain {domain!r}")
        try:
            confidence = float(item["confidence"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"confidence is not a number: {item['confidence']!r}") from exc
        if not 0 <= confidence <= 1:
            raise ValueError(f"confidence out of range: {confidence}")
        result[sentence_id] = (domain, confidence)

    missing = expected_ids - result.keys()
    if missing:
        raise ValueError(f"no answer for {len(missing)} sentence(s)")
    return result


# ===========
# CLASSIFYING
# ===========


def classify_batch(
    client: Any, settings: DomainSettings, tracker: SpendTracker, batch: list[dict[str, str]]
) -> tuple[dict[str, tuple[str, float]] | None, str]:
    """Return (assignments, error). Assignments is None when both attempts fail."""
    ids = [row["sentence_id"] for row in batch]
    messages = build_messages(settings, batch)
    error = ""
    for attempt in range(2):
        if attempt and not tracker.allows(settings.model, settings.max_calls):
            return None, error or "spend or call limit reached before retry"
        try:
            resp = client.chat(settings.model, messages)
        except Exception as exc:  # the client already retries transport errors
            tracker.add(settings.model, None)
            return None, str(exc)[:300]
        cost = (resp.get("usage") or {}).get("cost")
        tracker.add(settings.model, float(cost) if cost is not None else None)

        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            return parse_batch_reply(text, ids, set(settings.domain_keys)), ""
        except ValueError as exc:
            error = f"{exc}: {text[:150]}"
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": RETRY_NOTE},
            ]
    return None, error


def read_kept(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def run_domains(*, run_id: str, settings: DomainSettings, client: Any | None = None) -> dict[str, Any]:
    """Persist domain scores separately and merge them into the kept CSV by sentence_id."""
    run_dir = settings.sentences_root / run_id
    kept_path = run_dir / "sentences-kept.csv"
    if not kept_path.exists():
        raise FileNotFoundError(f"{kept_path} not found; run the sentences stage first")

    rows = read_kept(kept_path)
    scores_path = run_dir / "domain-scores.csv"
    scores = read_domain_scores(run_dir)
    merge_domain_scores(rows, scores)
    write_domain_scores(scores_path, scores)
    write_kept(kept_path, rows)
    pending = [row for row in rows if not row.get("domain")]
    client = client or OpenRouterClient(settings.scoring)
    tracker = SpendTracker(limit_usd=settings.scoring.max_spend_usd)
    scored = unscored = processed = 0
    errors: list[str] = []

    for start in range(0, len(pending), settings.batch_size):
        if not tracker.allows(settings.model, settings.max_calls):
            break
        batch = pending[start : start + settings.batch_size]
        assignments, error = classify_batch(client, settings, tracker, batch)
        for row in batch:
            score = {
                "sentence_id": row["sentence_id"],
                "model": settings.model,
                "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if assignments is None:
                score.update(domain="", domain_confidence="", domain_source=UNSCORED)
                unscored += 1
            else:
                domain, confidence = assignments[row["sentence_id"]]
                score.update(domain=domain, domain_confidence=f"{confidence:.2f}", domain_source=MODEL_SOURCE)
                scored += 1
            scores[row["sentence_id"]] = score
        if error:
            errors.append(error)
        processed += len(batch)
        write_domain_scores(scores_path, scores)
        merge_domain_scores(rows, scores)
        write_kept(kept_path, rows)

    summary = {
        "model": settings.model,
        "already_scored": len(rows) - len(pending),
        "scored": scored,
        "unscored": unscored,
        "remaining": len(pending) - processed,
        "stopped_by_limit": processed < len(pending),
        "calls": sum(tracker.calls.values()),
        "spent_usd": round(tracker.spent_usd, 6),
        "max_spend_usd": settings.scoring.max_spend_usd,
        "errors": errors[:MAX_LOGGED_ERRORS],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _update_run_log(run_dir / "run-log.json", summary)
    return summary


# ======
# OUTPUT
# ======


def _update_run_log(path: Path, summary: dict[str, Any]) -> None:
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    log["domains"] = summary
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
