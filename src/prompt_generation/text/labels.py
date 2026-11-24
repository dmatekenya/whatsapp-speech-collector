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
JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
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


def _extract_items(text: str) -> list:
    """Find a JSON list or an object containing a single list inside a reply."""
    decoder = json.JSONDecoder()
    decode_error = None
    # A fenced reply takes precedence over bracketed prose such as citations.
    for candidate in [*JSON_FENCE.findall(text), text]:
        position = 0
        for match in re.finditer(r"[\[{]", candidate):
            if match.start() < position:
                continue
            try:
                value, position = decoder.raw_decode(candidate, match.start())
            except json.JSONDecodeError as exc:
                if decode_error is None:
                    decode_error = exc
                continue
            if isinstance(value, list):
                if not value or any(isinstance(item, dict) for item in value):
                    return value
                continue
            lists = [item for item in value.values() if isinstance(item, list)]
            if len(lists) == 1:
                return lists[0]
            row_lists = [items for items in lists if any(isinstance(item, dict) for item in items)]
            if len(row_lists) == 1:
                return row_lists[0]
    if decode_error is not None:
        raise ValueError(f"Could not decode a JSON label list ({decode_error})")
    raise ValueError("No JSON label list found")


def _resolve_key(value: object, names: dict[str, str]) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.casefold().split())
    return names.get(normalized, "")


def _key_names(specs: dict[str, dict]) -> dict[str, str]:
    names = {" ".join(spec["label"].casefold().split()): key for key, spec in specs.items()}
    names.update((key.casefold(), key) for key in specs)
    return names


def parse_reply(text: str, ids: list[str], settings: LabelSettings) -> tuple[dict[str, dict], str | None]:
    """Return valid rows and a diagnostic for any parsing or validation failures."""
    try:
        items = _extract_items(text)
    except ValueError as exc:
        return {}, str(exc)

    domain_names = _key_names({spec["key"]: spec for spec in settings.stage3.scoring.domains})
    topic_names = {domain: {**_key_names(specs), "other": "other"}
                   for domain, specs in settings.topics.items()}
    expected = set(ids)
    result = {}
    seen = set()
    errors = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"Item {position + 1} is not an object")
            continue
        row_id = item.get("pool_id")
        if row_id in (None, ""):
            row_id = item.get("id")
        if row_id in (None, "") and position < len(ids):
            row_id = ids[position]
        if not isinstance(row_id, str) or row_id not in expected:
            errors.append(f"Item {position + 1} has unknown pool ID {row_id!r}")
            continue
        if row_id in seen:
            result.pop(row_id, None)
            errors.append(f"Duplicate pool ID {row_id!r}")
            continue
        seen.add(row_id)
        domain = _resolve_key(item.get("domain"), domain_names)
        topic = _resolve_key(item.get("topic"), topic_names.get(domain, {}))
        label = {**item, "domain": domain, "topic": topic}
        if not domain:
            errors.append(f"{row_id}: unknown domain {item.get('domain')!r}")
        elif not topic:
            errors.append(f"{row_id}: unknown topic {item.get('topic')!r} for {domain}")
        elif not is_valid_label(label, settings.topics):
            errors.append(f"{row_id}: invalid confidence or suggested_topic")
        else:
            result[row_id] = {
                "domain": domain, "topic": topic,
                "suggested_topic": " ".join(item.get("suggested_topic", "").lower().split()),
                "confidence": float(item["confidence"]),
            }
    missing = [row_id for row_id in ids if row_id not in seen]
    if missing:
        errors.append("Missing labels for " + ", ".join(missing))
    return result, "; ".join(errors) or None


def build_messages(system_prompt: str, batch: list[dict]) -> list[dict[str, str]]:
    payload = [{"id": row["pool_id"], "text": row["text"]} for row in batch]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Classify these sentences.\n" + json.dumps(payload, ensure_ascii=False)},
    ]


def _classify(client, settings: LabelSettings, tracker: SpendTracker, batch: list[dict],
              system_prompt: str, batch_number: int, report_reply) -> tuple[dict[str, dict], Exception | None]:
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
        choice = (response.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        ids = [row["pool_id"] for row in pending]
        if content is None or isinstance(content, str):
            text = content or ""
            parsed, parse_error = parse_reply(text, ids, settings)
        else:
            text = json.dumps(content, ensure_ascii=False)
            parsed, parse_error = {}, "Model reply content is not text"
        report_reply(batch_number, attempt + 1, ids, text, parse_error, choice.get("finish_reason"))
        labels.update(parsed)
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
    responses_path = out / "label-responses.jsonl"
    printed_failure = False

    def report_reply(batch_number, attempt, ids, text, parse_error, finish_reason):
        nonlocal printed_failure
        record = {
            "batch_number": batch_number, "attempt": attempt, "pool_ids": ids,
            "raw_text": text, "parse_error": parse_error, "finish_reason": finish_reason,
            "model": stage3.model, "topic_list_version": settings.version,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with responses_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if parse_error and not printed_failure:
            print(f"First failing reply (batch {batch_number}, attempt {attempt}): "
                  f"{parse_error}\n{text}", flush=True)
            printed_failure = True

    with output_lock(out):
        rows = [row for row in read_kept(out / "pool.csv") if row["status"] != "retired"]
        saved = {row["pool_id"]: row for row in read_kept(path)} if path.exists() else {}
        completed = valid_labels(path, settings)
        pending = [row for row in rows if row["pool_id"] not in completed]
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
            labels, error = _classify(
                client, settings, tracker, batch, prompt, start // stage3.batch_size + 1, report_reply,
            )
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
