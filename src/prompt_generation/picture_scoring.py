"""Domain scoring: ask a vision model for the primary domain of each candidate photo.

Backend: OpenRouter's OpenAI-compatible chat completions API (one key, many models).
Results go to domain-scores.csv in the harvest run folder, one row per (candidate, model).
Re-running skips pairs that already have a valid score, so a run can be stopped and resumed.
"""

from __future__ import annotations

import base64
import csv
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

log = logging.getLogger(__name__)

UNCLEAR = "unclear"
NOT_RELEVANT = "not-relevant"
FLAG_NAMES = ("sensitive", "graphic_or_sexual", "visible_text", "poor_quality")
SCORE_FIELDS = [
    "candidate_id", "model", "include_metadata", "status", "domain", "second_choice",
    "confidence", "reason", "prompt_tokens", "completion_tokens", "cost_usd", "error",
    "scored_at",
    "flag_sensitive", "flag_graphic_or_sexual", "flag_visible_text", "flag_poor_quality",
]


# ======
# CONFIG
# ======


@dataclass(frozen=True)
class ScoringConfig:
    base_url: str
    api_key_env: str
    models: list[str]
    larger_models: list[str]
    max_spend_usd: float
    max_calls_per_model: int
    concurrency: int
    temperature: float
    max_tokens: int
    timeout_seconds: float
    max_retries: int
    include_metadata: bool
    instructions: str
    domains: list[dict[str, str]]

    @property
    def domain_keys(self) -> list[str]:
        return [d["key"] for d in self.domains]

    def system_prompt(self) -> str:
        lines = [self.instructions.strip(), "", "Domains:"]
        for d in self.domains:
            lines.append(f"- {d['key']}: {d['scope']} Example picture: {d['example']}")
        return "\n".join(lines)


def load_scoring_config(path: str | Path) -> ScoringConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if raw.get("backend", "openrouter") != "openrouter":
        raise ValueError("Only the 'openrouter' backend is implemented")
    return ScoringConfig(
        base_url=raw["base_url"].rstrip("/"),
        api_key_env=raw.get("api_key_env", "OPENROUTER_API_KEY"),
        models=list(raw.get("models", [])),
        larger_models=list(raw.get("larger_models", [])),
        max_spend_usd=float(raw.get("max_spend_usd", 1.0)),
        max_calls_per_model=int(raw.get("max_calls_per_model", 200)),
        concurrency=int(raw.get("concurrency", 4)),
        temperature=float(raw.get("temperature", 0)),
        max_tokens=int(raw.get("max_tokens", 300)),
        timeout_seconds=float(raw.get("timeout_seconds", 60)),
        max_retries=int(raw.get("max_retries", 3)),
        include_metadata=bool(raw.get("include_metadata", True)),
        instructions=raw["instructions"],
        domains=list(raw["domains"]),
    )


# ===========
# MODEL REPLY
# ===========


@dataclass
class DomainScore:
    domain: str
    second_choice: str
    confidence: float
    reason: str
    sensitive: bool = False
    graphic_or_sexual: bool = False
    visible_text: bool = False
    poor_quality: bool = False


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_reply(text: str, allowed: list[str]) -> DomainScore:
    """Parse and validate the model's JSON reply. Raises ValueError if it is unusable."""
    match = _JSON_RE.search(text or "")
    if not match:
        raise ValueError("no JSON object in reply")
    data = json.loads(match.group())

    valid = set(allowed) | {UNCLEAR, NOT_RELEVANT}
    domain = str(data.get("domain", "")).strip().lower()
    if domain not in valid:
        raise ValueError(f"invalid domain {domain!r}")

    second = data.get("second_choice")
    second = str(second).strip().lower() if second not in (None, "", "null") else ""
    if second and second not in valid:
        second = ""  # a bad second choice is not worth a retry

    try:
        confidence = min(max(float(data.get("confidence", 0)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0

    reason = re.sub(r"\s+", " ", str(data.get("reason", ""))).strip()[:300]
    flags = data.get("flags")
    flags = flags if isinstance(flags, dict) else {}
    return DomainScore(domain, second, confidence, reason,
                       **{name: flags.get(name) is True for name in FLAG_NAMES})


# ======
# CLIENT
# ======


class OpenRouterClient:
    """Minimal chat-completions client; one requests.Session per thread."""

    def __init__(self, cfg: ScoringConfig, api_key: str | None = None):
        self.cfg = cfg
        self.api_key = api_key or os.environ.get(cfg.api_key_env, "")
        if not self.api_key:
            raise RuntimeError(f"{cfg.api_key_env} is not set (add it to .env)")
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            s = requests.Session()
            s.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "Chichewa speech prompt domain scoring",
            })
            self._local.session = s
        return self._local.session

    def chat(self, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "usage": {"include": True},  # ask OpenRouter to report the cost of the call
        }
        delay = 2.0
        for attempt in range(self.cfg.max_retries + 1):
            try:
                r = self._session().post(
                    f"{self.cfg.base_url}/chat/completions", json=body,
                    timeout=self.cfg.timeout_seconds,
                )
            except requests.RequestException:
                if attempt == self.cfg.max_retries:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.cfg.max_retries:
                    r.raise_for_status()
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError("unreachable")

    def list_vision_models(self) -> list[dict[str, Any]]:
        r = self._session().get(f"{self.cfg.base_url}/models", timeout=self.cfg.timeout_seconds)
        r.raise_for_status()
        models = r.json().get("data", [])
        return [
            m for m in models
            if "image" in (m.get("architecture", {}).get("input_modalities") or [])
        ]


# ========
# MESSAGES
# ========


def build_messages(cfg: ScoringConfig, row: dict[str, str], thumb: Path,
                   include_metadata: bool) -> list[dict[str, Any]]:
    image_b64 = base64.b64encode(thumb.read_bytes()).decode()
    text = "Classify this photo."
    if include_metadata:
        title = row.get("title", "")[:200]
        categories = row.get("tags", "").replace("|", "; ")[:400]
        text += (
            f"\nPhoto title: {title}\nSource categories/tags: {categories}\n"
            "Use the title and tags only as hints; decide from what is visible."
        )
    return [
        {"role": "system", "content": cfg.system_prompt()},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]},
    ]


# =======
# RUNNING
# =======


@dataclass
class SpendTracker:
    limit_usd: float
    spent_usd: float = 0.0
    calls: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, model: str, cost: float | None) -> None:
        with self.lock:
            self.calls[model] = self.calls.get(model, 0) + 1
            if cost:
                self.spent_usd += cost

    def allows(self, model: str, max_calls: int) -> bool:
        with self.lock:
            return self.spent_usd < self.limit_usd and self.calls.get(model, 0) < max_calls


def migrate_score_header(scores_path: Path) -> None:
    """Atomically add empty flag columns to legacy scores without losing any rows."""
    if not scores_path.exists():
        return
    with open(scores_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames == SCORE_FIELDS:
            return
        if reader.fieldnames not in (None, SCORE_FIELDS[:-len(FLAG_NAMES)]):
            raise ValueError(f"Unexpected score CSV header at {scores_path}")
        rows = list(reader)
    tmp = scores_path.with_suffix(".csv.part")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCORE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(scores_path)


def load_done(scores_path: Path) -> set[tuple[str, str, str]]:
    """(candidate_id, model, include_metadata) triples that already have a valid score."""
    if not scores_path.exists():
        return set()
    with open(scores_path, newline="", encoding="utf-8") as fh:
        return {
            (r["candidate_id"], r["model"], r["include_metadata"])
            for r in csv.DictReader(fh) if r["status"] == "ok"
        }


def score_one(client, cfg: ScoringConfig, model: str, row: dict[str, str], thumb: Path,
              include_metadata: bool, tracker: SpendTracker) -> dict[str, str]:
    out = {
        "candidate_id": row["candidate_id"], "model": model,
        "include_metadata": str(include_metadata).lower(), "status": "error",
        "domain": "", "second_choice": "", "confidence": "", "reason": "",
        "prompt_tokens": "", "completion_tokens": "", "cost_usd": "", "error": "",
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{f"flag_{name}": "" for name in FLAG_NAMES},
    }
    messages = build_messages(cfg, row, thumb, include_metadata)
    tokens_in = tokens_out = 0
    cost = 0.0
    for attempt in range(2):  # one retry if the reply is not valid JSON / domain
        try:
            resp = client.chat(model, messages)
        except Exception as exc:
            out["error"] = str(exc)[:300]
            tracker.add(model, None)
            return out
        usage = resp.get("usage") or {}
        tokens_in += int(usage.get("prompt_tokens") or 0)
        tokens_out += int(usage.get("completion_tokens") or 0)
        call_cost = usage.get("cost")
        cost += float(call_cost or 0)
        tracker.add(model, float(call_cost) if call_cost is not None else None)

        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            s = parse_reply(text, cfg.domain_keys)
        except (ValueError, json.JSONDecodeError) as exc:
            out["status"], out["error"] = "invalid", f"{exc}: {text[:150]}"
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Reply with the JSON object only, using one of "
                                            "the listed domain keys, \"unclear\" or \"not-relevant\"."},
            ]
            continue
        out.update(status="ok", error="", domain=s.domain, second_choice=s.second_choice,
                   confidence=f"{s.confidence:.2f}", reason=s.reason)
        out.update({f"flag_{name}": str(getattr(s, name)).lower() for name in FLAG_NAMES})
        break
    out.update(prompt_tokens=str(tokens_in), completion_tokens=str(tokens_out),
               cost_usd=f"{cost:.6f}")
    return out


def run_scoring(client, cfg: ScoringConfig, run_dir: Path, models: list[str],
                candidate_ids: set[str] | None = None, include_metadata: bool | None = None,
                limit: int | None = None, max_spend_usd: float | None = None) -> dict[str, Any]:
    """Score kept candidates of a harvest run with each model. Returns a small summary."""
    include_metadata = cfg.include_metadata if include_metadata is None else include_metadata
    with open(run_dir / "candidates.csv", newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["status"] == "kept"]
    if candidate_ids is not None:
        rows = [r for r in rows if r["candidate_id"] in candidate_ids]
    if limit:
        rows = rows[:limit]

    scores_path = run_dir / "domain-scores.csv"
    migrate_score_header(scores_path)
    done = load_done(scores_path)
    tracker = SpendTracker(max_spend_usd if max_spend_usd is not None else cfg.max_spend_usd)
    write_lock = threading.Lock()
    new_file = not scores_path.exists()
    counts = {"ok": 0, "invalid": 0, "error": 0, "skipped_done": 0, "skipped_budget": 0}

    with open(scores_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCORE_FIELDS)
        if new_file:
            writer.writeheader()

        def task(model: str, row: dict[str, str]) -> None:
            if not tracker.allows(model, cfg.max_calls_per_model):
                with write_lock:
                    counts["skipped_budget"] += 1
                return
            thumb = _thumb_path(run_dir, row)
            if thumb is None:
                return
            result = score_one(client, cfg, model, row, thumb, include_metadata, tracker)
            with write_lock:
                writer.writerow(result)
                fh.flush()
                counts[result["status"]] += 1

        jobs = []
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            for model in models:
                for row in rows:
                    key = (row["candidate_id"], model, str(include_metadata).lower())
                    if key in done:
                        counts["skipped_done"] += 1
                        continue
                    jobs.append(pool.submit(task, model, row))
            for j in jobs:
                j.result()

    return {**counts, "spent_usd": round(tracker.spent_usd, 4), "calls": dict(tracker.calls)}


def _thumb_path(run_dir: Path, row: dict[str, str]) -> Path | None:
    """Find the thumbnail: the recorded path, else thumbs/<candidate_id>.jpg in the run."""
    for p in (Path(row.get("thumb_path") or ""), run_dir / "thumbs" / f"{row['candidate_id']}.jpg"):
        if str(p) not in ("", ".") and p.is_file():
            return p
    log.warning("No thumbnail for %s", row["candidate_id"])
    return None
