"""Provisional domain and topic labels for the sentence pool."""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT
from src.prompt_generation.picture_scoring import OpenRouterClient, SpendTracker
from src.prompt_generation.text.domains import DomainSettings, load_domain_settings, read_kept
from src.prompt_generation.text.pool import output_lock
from src.prompt_generation.text.sentences import DEFAULT_CONFIG, _write_csv

LABEL_COLUMNS = [
    "pool_id", "domain", "topic", "suggested_topic", "confidence", "model",
    "topic_list_version", "labelled_at",
]
INVALID = {"domain": "invalid", "topic": "invalid", "suggested_topic": "", "confidence": ""}
REPLY_FORMAT = (
    'Return only a JSON array with one object per supplied id: '
    '{"id":"…","domain":"…","topic":"…","confidence":0.9,"suggested_topic":""}. '
    'Choose the best-fitting listed domain, even when uncertain. Choose a topic belonging '
    'to that domain, or "other" if none fits. For "other", supply 2–4 English words '
    'in suggested_topic. For listed topics, leave suggested_topic empty.'
)
ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")


@dataclass(frozen=True)
class LabelSettings:
    stage3: DomainSettings
    version: str
    topics: dict[str, dict[str, dict[str, str]]]
    targets: dict[str, int]
    input_rate: float
    output_rate: float

    @property
    def system_prompt(self) -> str:
        # The stage 3 prompt mentions "unclear" in its instructions and final line.
        lines = [line for line in self.stage3.system_prompt.splitlines() if '"unclear"' not in line]
        lines.extend(["", REPLY_FORMAT, "", "Topics by domain:"])
        for domain, topics in self.topics.items():
            lines.append(f"{domain}:")
            lines.extend(f"- {key}: {spec['covers']}" for key, spec in topics.items())
        return "\n".join(lines)


def load_label_settings(path: Path = DEFAULT_CONFIG) -> LabelSettings:
    stage3 = load_domain_settings(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    draft = yaml.safe_load((PROJECT_ROOT / raw["topics_config"]).read_text(encoding="utf-8"))
    if set(draft["topics"]) != set(stage3.domain_keys) or set(raw["domain_targets"]) != set(stage3.domain_keys):
        raise ValueError("Domain keys must match across domains, draft topics and targets")
    return LabelSettings(
        stage3, str(draft["version"]), draft["topics"], raw["domain_targets"],
        float(raw["label_input_usd_per_million"]), float(raw["label_output_usd_per_million"]),
    )


def is_valid_label(row: dict, topics: dict) -> bool:
    domain, topic = row.get("domain"), row.get("topic")
    if not isinstance(domain, str) or domain not in topics or not isinstance(topic, str):
        return False
    suggestion = row.get("suggested_topic", "")
    if not isinstance(suggestion, str):
        return False
    if topic == "other":
        words = suggestion.split()
        if not 2 <= len(words) <= 4 or not all(ENGLISH_WORD.fullmatch(word) for word in words):
            return False
    elif topic not in topics[domain] or suggestion.strip():
        return False
    try:
        confidence = row.get("confidence")
        return not isinstance(confidence, bool) and 0 <= float(confidence) <= 1
    except (ValueError, TypeError, OverflowError):
        return False


def parse_reply(text: str, ids: set[str], topics: dict) -> dict[str, dict]:
    """Keep valid rows and leave missing, duplicate or invalid IDs pending."""
    try:
        items = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(items, list):
        return {}
    result = {}
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        row_id = item.get("id")
        if not isinstance(row_id, str) or row_id not in ids:
            continue
        if row_id in seen:
            result.pop(row_id, None)
        elif is_valid_label(item, topics):
            result[row_id] = {
                "domain": item["domain"], "topic": item["topic"],
                "suggested_topic": " ".join(item.get("suggested_topic", "").lower().split()),
                "confidence": float(item["confidence"]),
            }
        seen.add(row_id)
    return result


def build_messages(system_prompt: str, batch: list[dict]) -> list[dict[str, str]]:
    payload = [{"id": row["pool_id"], "text": row["text"]} for row in batch]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Classify these sentences.\n" + json.dumps(payload, ensure_ascii=False)},
    ]


def _classify(client, settings: LabelSettings, tracker: SpendTracker, batch: list[dict],
              system_prompt: str) -> tuple[dict[str, dict], Exception | None]:
    stage3 = settings.stage3
    labels = {}
    pending = batch
    for attempt in range(2):
        if not pending or not tracker.allows(stage3.model, stage3.max_calls):
            break
        messages = build_messages(system_prompt, pending)
        if attempt:
            messages[1]["content"] = "Correct the previous invalid or missing labels.\n" + messages[1]["content"]
        try:
            response = client.chat(stage3.model, messages)
        except Exception as exc:
            # The client already retries transport failures. Save any valid
            # first-pass labels and let the remaining rows resume on rerun.
            return labels, exc
        cost = (response.get("usage") or {}).get("cost")
        tracker.add(stage3.model, float(cost) if cost is not None else None)
        text = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        labels.update(parse_reply(text, {row["pool_id"] for row in pending}, settings.topics))
        pending = [row for row in pending if row["pool_id"] not in labels]
        if attempt:
            labels.update((row["pool_id"], INVALID) for row in pending)
    return labels, None


def current_labels(path: Path, settings: LabelSettings) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {
        row["pool_id"]: row for row in read_kept(path)
        if row["model"] == settings.stage3.model and row["topic_list_version"] == settings.version
    }


def valid_labels(path: Path, settings: LabelSettings) -> dict[str, dict]:
    return {key: row for key, row in current_labels(path, settings).items()
            if is_valid_label(row, settings.topics)}


def _estimate_cost(pending: list[dict], settings: LabelSettings, prompt: str) -> None:
    stage3 = settings.stage3
    input_tokens = 0
    for start in range(0, len(pending), stage3.batch_size):
        batch = pending[start:start + stage3.batch_size]
        messages = build_messages(prompt, batch)
        input_tokens += sum(ceil(len(message["content"].encode("utf-8")) / 3) for message in messages)
    batches = ceil(len(pending) / stage3.batch_size)
    output_tokens = batches * stage3.scoring.max_tokens
    estimate = (input_tokens * settings.input_rate + output_tokens * settings.output_rate) / 1_000_000
    print(f"Estimated cost for {len(pending)} rows in {batches} batches: ${estimate:.4f} "
          f"(before retries, ${stage3.scoring.max_spend_usd:.2f} run cap)", flush=True)


def run_labels(out: Path, settings: LabelSettings, *, limit: int | None = None, seed: int = 42,
               client=None) -> None:
    stage3 = settings.stage3
    path = out / "labels-model.csv"
    prompt = settings.system_prompt
    with output_lock(out):
        rows = [row for row in read_kept(out / "pool.csv") if row["status"] != "retired"]
        saved = {row["pool_id"]: row for row in read_kept(path)} if path.exists() else {}
        pending = [
            row for row in rows if row["pool_id"] not in saved
            or saved[row["pool_id"]]["model"] != stage3.model
            or saved[row["pool_id"]]["topic_list_version"] != settings.version
        ]
        if limit is not None:
            pending.sort(key=lambda row: row["pool_id"])
            pending = random.Random(seed).sample(pending, min(limit, len(pending)))
        _estimate_cost(pending, settings, prompt)
        if pending and client is None:
            client = OpenRouterClient(stage3.scoring)
        tracker = SpendTracker(limit_usd=stage3.scoring.max_spend_usd)
        for start in range(0, len(pending), stage3.batch_size):
            if not tracker.allows(stage3.model, stage3.max_calls):
                break
            batch = pending[start:start + stage3.batch_size]
            labels, error = _classify(client, settings, tracker, batch, prompt)
            labelled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for pool_id, label in labels.items():
                saved[pool_id] = {
                    **label, "pool_id": pool_id, "model": stage3.model,
                    "topic_list_version": settings.version, "labelled_at": labelled_at,
                }
            if labels:
                _write_csv(path, list(saved.values()), LABEL_COLUMNS)
            if error is not None:
                raise error
        if not path.exists():
            _write_csv(path, [], LABEL_COLUMNS)
        current = current_labels(path, settings)

    results = [current[row["pool_id"]] for row in rows if row["pool_id"] in current]
    valid = [row for row in results if is_valid_label(row, settings.topics)]
    print(f"Labels for pool {out.name}: {len(valid)} valid, {len(results) - len(valid)} invalid, "
          f"{len(rows) - len(results)} unlabelled; spent ${tracker.spent_usd:.4f}")
    for domain, count in sorted(Counter(row["domain"] for row in valid).items()):
        print(f"  domain {domain}: {count}")
    for (domain, topic), count in sorted(Counter((row["domain"], row["topic"]) for row in valid).items()):
        print(f"  topic {domain}/{topic}: {count}")
    other = Counter(row["suggested_topic"] for row in valid if row["topic"] == "other")
    print(f"  other: {sum(other.values())}")
    for suggestion, count in other.most_common(20):
        print(f"    {suggestion}: {count}")
