"""Generate and validate Chichewa conversation starters with resumable per-call state."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT
from src.prompt_generation.picture_scoring import OpenRouterClient, ScoringConfig, SpendTracker
from src.prompt_generation.text.domains import load_domain_settings
from src.prompt_generation.text.labels import JSON_FENCE
from src.prompt_generation.text.pool import output_lock
from src.prompt_generation.text.review import _largest_remainder
from src.prompt_generation.text.sentences import _write_csv, dedupe_key, sentence_id

DEFAULT_CONFIG = PROJECT_ROOT / "config/starter-generation.yaml"
OUTPUT_ROOT = PROJECT_ROOT / "data/prompts/starters/generated"
ANY_TOPIC = "any"
COLUMNS = [
    "candidate_id", "text", "domain", "topic", "word_count", "model", "generated_at", "source_category",
]
SHORTFALL_COLUMNS = ["domain", "topic", "quota", "generated", "accepted", "calls", "shortfall", "reason"]
OPEN_QUESTION = re.compile(r"\b(?:chiyani|bwanji|motani|liti|ndani|zingati|"
                           r"chiti|iti|yiti|witi|ziti|ati)\b|\bkuti$")
YES_NO_OPENING = re.compile(r"^(?:kodi|mumakonda|mumadziwa|munayamba|munapitako|"
                            r"muli|mumalima|mumatani|mukufuna|mukhoza)\b")


@dataclass(frozen=True)
class StarterSettings:
    scoring: ScoringConfig
    model: str
    total: int
    shares: dict[str, int]
    topics: dict[str, dict]
    per_call: int
    max_calls_per_topic: int | None
    banned_phrases: tuple[str, ...]
    system_prompt: str
    input_rate: float
    output_rate: float

    def call_limit(self, quota: int) -> int:
        return self.max_calls_per_topic or ceil(3 * quota / self.per_call)


def load_settings(path: Path = DEFAULT_CONFIG) -> StarterSettings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    draft = yaml.safe_load((PROJECT_ROOT / "config/topics-draft.yaml").read_text(encoding="utf-8"))
    topics, shares = draft["topics"], raw["domain_shares"]
    if set(shares) != set(topics) or any(weight <= 0 for weight in shares.values()):
        raise ValueError("Provide a positive share for each domain in topics-draft.yaml")
    if any(not items or ANY_TOPIC in items for items in topics.values()):
        raise ValueError("Each domain needs draft topics distinct from the any-topic slot")
    call_limit = raw["max_calls_per_topic"]
    if min(raw["total"], raw["per_call"], raw["max_tokens"], raw["max_spend_usd"]) <= 0 or (
        call_limit is not None and call_limit <= 0
    ):
        raise ValueError("Generation sizes, call limits and cost cap must be positive")

    model = raw["model"]
    scoring = replace(
        load_domain_settings().scoring,
        models=[model], max_spend_usd=float(raw["max_spend_usd"]),
        temperature=float(raw["temperature"]), max_tokens=raw["max_tokens"],
        reasoning_enabled=raw["reasoning"],
    )
    prompt = "\n".join([
        "Write original Malawian Chichewa conversation starters. Follow these style rules:",
        *(f"- {rule}" for rule in raw["style_rules"]),
        "",
        "Style examples by domain. Write new starters and do not copy the examples.",
        *(f"- {domain}: {example}" for domain, example in raw["examples"].items()),
        "",
        'Return only a JSON array of objects of the form [{"text": "..."}].',
    ])
    return StarterSettings(
        scoring, model, raw["total"], shares, topics, raw["per_call"], call_limit,
        tuple(dedupe_key(phrase) for phrase in raw["banned_phrases"]), prompt,
        float(raw["input_usd_per_million"]), float(raw["output_usd_per_million"]),
    )


def quotas(settings: StarterSettings, total: int) -> dict[tuple[str, str], int]:
    result = {}
    for domain, target in _largest_remainder(settings.shares, total).items():
        any_count = (target + 5) // 10
        split = _largest_remainder(dict.fromkeys(settings.topics[domain], 1), target - any_count)
        result.update({(domain, topic): n for topic, n in split.items()})
        result[domain, ANY_TOPIC] = any_count
    return result


def build_messages(settings: StarterSettings, domain: str, topic: str, count: int, prior: list[dict]) -> list[dict]:
    spec = settings.topics[domain].get(topic)
    scope = f"{topic} ({spec['label']}: {spec['covers']})" if spec else "anything in this domain"
    request = f"Write exactly {count} starters.\nDomain: {domain}\nTopic: {scope}"
    if prior:
        request += "\nDo not repeat or paraphrase these:\n" + "\n".join(row["text"] for row in prior[-40:])
    return [{"role": "system", "content": settings.system_prompt}, {"role": "user", "content": request}]


def _starts_kodi(key: str) -> bool:
    return key.partition(" ")[0] == "kodi"


def _filter_reason(text: str, key: str, banned_phrases: tuple[str, ...]) -> str:
    if not 6 <= len(text.split()) <= 15:
        return "word_length"
    if any(char.isdigit() for char in text):
        return "digits"
    if any(f" {phrase} " in f" {key} " for phrase in banned_phrases):
        return "banned_phrase"
    # A question followed by another question or an invitation may open with yes/no.
    questions = re.split(r"[?？]", text, maxsplit=1)
    if len(questions) == 2 and not dedupe_key(questions[1]):
        if YES_NO_OPENING.match(key) and not OPEN_QUESTION.search(key):
            return "yes_no"
    return ""


class Duplicates:
    """Index normalised text and count shared 4-grams only for potential matches."""

    def __init__(self):
        self.sizes: dict[str, int] = {}
        self.by_gram: dict[str, set[str]] = defaultdict(set)

    def add(self, key: str) -> bool:
        if key in self.sizes:
            return True
        grams = {key[i:i + 4] for i in range(len(key) - 3)}
        matches = Counter(previous for gram in grams for previous in self.by_gram.get(gram, ()))
        # Jaccard >= 3/5, expressed with integer arithmetic.
        duplicate = any(8 * common >= 3 * (len(grams) + self.sizes[previous])
                        for previous, common in matches.items())
        self.sizes[key] = len(grams)
        for gram in grams:
            self.by_gram[gram].add(key)
        return duplicate


@dataclass
class TopicProgress:
    accepted: list[dict]
    generated: int = 0
    kodi: int = 0
    calls: int = 0
    empty_calls: int = 0

    def record(self, accepted: list[dict], generated: int) -> None:
        self.accepted.extend(accepted)
        self.generated += generated
        self.kodi += sum(_starts_kodi(dedupe_key(row["text"])) for row in accepted)
        self.calls += 1
        self.empty_calls = 0 if accepted else self.empty_calls + 1

    def stop_reason(self, quota: int, call_limit: int) -> str:
        if len(self.accepted) >= quota:
            return "filled"
        if self.empty_calls >= 5:
            return "empty_calls"
        if self.calls >= call_limit:
            return "call_limit"
        return ""


def _reply_items(response: dict) -> tuple[list, str | None]:
    choice = (response.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if isinstance(content, str):
        fenced = JSON_FENCE.fullmatch(content.strip())
        try:
            items = json.loads(fenced.group(1) if fenced else content)
            if isinstance(items, list):
                return items, None
        except ValueError:
            pass
    return [], str(content or "")


def evaluate_reply(response: dict, settings: StarterSettings, domain: str, topic: str, count: int,
                   progress: TopicProgress, domain_counts: Counter, duplicates: Duplicates,
                   model: str) -> tuple[list[dict], list[dict], int]:
    items, invalid_reply = _reply_items(response)
    accepted, rejected = [], []
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic_kodi = progress.kodi
    domain_total = domain_counts[domain, "total"]
    domain_kodi = domain_counts[domain, "kodi"]

    def row_for(text: str, key: str) -> dict:
        return {
            "candidate_id": sentence_id(key) if key else "",
            "text": text, "domain": domain, "topic": topic, "word_count": len(text.split()),
            "model": model, "generated_at": generated_at, "source_category": "ai-generated",
        }

    if invalid_reply is not None:
        return [], [{**row_for(invalid_reply, ""), "reason": "invalid_reply"}], 0

    candidates = []
    for index, item in enumerate(items):
        value = item.get("text") if isinstance(item, dict) else None
        text = " ".join(value.split()) if isinstance(value, str) else ""
        key = dedupe_key(text)
        candidates.append((index, text, key))

    # Consider non-Kodi openings first so they can make room for Kodi in this call.
    for index, text, key in sorted(candidates, key=lambda item: _starts_kodi(item[2])):
        row = row_for(text, key)
        reason = _filter_reason(text, key, settings.banned_phrases) if text else "invalid_item"
        duplicate = duplicates.add(key) if key else False
        kodi = _starts_kodi(key)
        if not reason:
            if duplicate:
                reason = "duplicate"
            elif index >= count:
                reason = "extra_item"
            elif kodi and (
                4 * (topic_kodi + 1) > len(progress.accepted) + len(accepted) + 1
                or 4 * (domain_kodi + 1) > domain_total + len(accepted) + 1
            ):
                reason = "kodi_cap"
        if reason:
            rejected.append({**row, "reason": reason})
        else:
            accepted.append(row)
            topic_kodi += kodi
            domain_kodi += kodi
    return accepted, rejected, len(items)


class MockClient:
    """Canned transport with deterministic accepted examples and intentional rejects."""

    items = (
        "Tiuzeni za fungo labwino limene limakukumbutsani za kwanu.",
        "Ndi phokoso liti limene mumamva mukadzuka m'mawa kwambiri?",
        "Kumbukirani munthu amene anakuphunzitsani kuimba nyimbo yachikhalidwe.",
        "Kodi mumakonda kuonera mbalame zikudya munda mwanu?",
        "Tiuzeni za fungo labwino limene limakukumbutsani za kwanu.",
        "Fotokozani zimene boma lingachite kuti anthu azisangalala.",
        "Tiuzeni.",
        "Fotokozani mmene munaphunzirira ntchito imeneyi pamodzi ndi anzanu onse "
        "amene ankabwera kunyumba kwanu nthawi iliyonse ya mlungu.",
        "Kodi ndi malo ati amene mumapitako kukapuma?",
        "Tiuzeni za zinthu 3 zimene mumanyamula paulendo.",
        "Kodi ndi nyimbo iti imene imakusangalatsani kwambiri mukamagwira ntchito?",
    )

    def chat(self, model: str, messages: list[dict]) -> dict:
        count = int(messages[-1]["content"].split()[2])
        items = [{"text": text} for text in self.items[:count]]
        return {"choices": [{"message": {"content": json.dumps(items)}}], "usage": {"cost": 0}}


def _estimate_call(settings: StarterSettings, input_tokens: int) -> float:
    return (input_tokens * settings.input_rate
            + settings.scoring.max_tokens * settings.output_rate) / 1_000_000


def _input_tokens(messages: list[dict]) -> int:
    return sum(ceil(len(message["content"].encode("utf-8")) / 3) for message in messages)


def _print_estimate(settings: StarterSettings, targets: dict, progress: dict, mock: bool) -> None:
    calls = sum(ceil(quota / settings.per_call) for quota in targets.values())
    estimate = 0.0
    if not mock:
        for (domain, topic), quota in targets.items():
            prior = progress[domain, topic].accepted
            messages = build_messages(settings, domain, topic, settings.per_call, [])
            base_input = _input_tokens(messages)
            for start in range(0, quota, settings.per_call):
                history_input = min(start + len(prior), 40) * 50
                estimate += _estimate_call(settings, base_input + history_input)
    print(f"Estimated cost for {sum(targets.values())} starters in {calls} calls: ${estimate:.4f} "
          f"(before retries, ${settings.scoring.max_spend_usd:.2f} run cap)", flush=True)


def _write_report(out: Path, targets: dict, progress: dict, rejected: list[dict], settings: StarterSettings,
                  spent: float, pilot: bool) -> None:
    accepted = [row for state in progress.values() for row in state.accepted]
    report = []
    for (domain, topic), quota in targets.items():
        state = progress[domain, topic]
        report.append({
            "domain": domain, "topic": topic, "quota": quota, "generated": state.generated,
            "accepted": len(state.accepted), "calls": state.calls, "shortfall": quota - len(state.accepted),
            "reason": state.stop_reason(quota, settings.call_limit(quota)) or "pending",
        })
    _write_csv(out / "starters.csv", accepted, COLUMNS)
    _write_csv(out / "rejected.csv", rejected, [*COLUMNS, "reason"])
    _write_csv(out / "shortfalls.csv", report, SHORTFALL_COLUMNS)
    print(f"Starters for run {out.name}: {len(accepted)} accepted, {len(rejected)} rejected, "
          f"{sum(targets.values()) - len(accepted)} shortfall; spent ${spent:.4f}")
    for reason, count in Counter(row["reason"] for row in rejected).most_common():
        print(f"  {reason}: {count}")
    if pilot:
        for domain in settings.shares:
            slots = [row for row in report if row["domain"] == domain and row["quota"]]
            accepted_count = sum(row["accepted"] for row in slots)
            generated = sum(row["generated"] for row in slots)
            kodi = sum(progress[domain, row["topic"]].kodi for row in slots)
            share = kodi / accepted_count if accepted_count else 0
            print(f"  {domain}: {generated} generated, {accepted_count} accepted, {share:.1%} Kodi")
            for row in slots:
                topic = row["topic"]
                print(f"    {topic}: {row['generated']} generated, {row['accepted']} accepted "
                      f"of {row['quota']} requested")
                for starter in progress[domain, topic].accepted:
                    print(f"      {starter['text']}")


def run_generation(out: Path, settings: StarterSettings, *, pilot: int | None = None,
                   mock: bool = False, client=None) -> None:
    targets = quotas(settings, settings.total if pilot is None else pilot)
    model = "mock" if mock else settings.model
    signature = json.dumps({
        "model": model, "configured_model": settings.model, "pilot": pilot,
        "targets": [[*key, value] for key, value in targets.items()],
        "topics": settings.topics, "per_call": settings.per_call, "limit": settings.max_calls_per_topic,
        "prompt": settings.system_prompt, "banned": settings.banned_phrases,
        "temperature": settings.scoring.temperature, "max_tokens": settings.scoring.max_tokens,
        "reasoning": settings.scoring.reasoning_enabled,
    }, ensure_ascii=False)
    progress = {key: TopicProgress([]) for key in targets}
    duplicates = Duplicates()
    domain_counts = Counter()
    rejected = []
    tracker = SpendTracker(limit_usd=settings.scoring.max_spend_usd)

    def record(domain, topic, accepted, declined, generated):
        progress[domain, topic].record(accepted, generated)
        rejected.extend(declined)
        domain_counts[domain, "total"] += len(accepted)
        domain_counts[domain, "kodi"] += sum(_starts_kodi(dedupe_key(row["text"])) for row in accepted)

    with output_lock(out), closing(sqlite3.connect(out / "calls.sqlite3")) as journal:
        with journal:
            journal.execute("CREATE TABLE IF NOT EXISTS run (signature TEXT NOT NULL)")
            journal.execute("CREATE TABLE IF NOT EXISTS calls ("
                            "id INTEGER PRIMARY KEY, domain TEXT, topic TEXT, accepted TEXT, "
                            "rejected TEXT, generated INTEGER)")
            saved = journal.execute("SELECT signature FROM run").fetchone()
            if saved is None:
                journal.execute("INSERT INTO run VALUES (?)", (signature,))
            elif saved[0] != signature:
                raise ValueError("Run settings differ from the saved run; use the original settings or a new run ID")

        for domain, topic, accepted_json, rejected_json, generated in journal.execute(
            "SELECT domain, topic, accepted, rejected, generated FROM calls ORDER BY id"
        ):
            accepted, declined = json.loads(accepted_json), json.loads(rejected_json)
            for row in [*accepted, *declined]:
                if row["candidate_id"]:
                    duplicates.add(dedupe_key(row["text"]))
            record(domain, topic, accepted, declined, generated)

        pending = {
            key: quota - len(progress[key].accepted) for key, quota in targets.items()
            if not progress[key].stop_reason(quota, settings.call_limit(quota))
        }
        _print_estimate(settings, pending, progress, mock)
        max_calls = sum(settings.call_limit(quota) for quota in targets.values())
        try:
            for (domain, topic), quota in targets.items():
                state = progress[domain, topic]
                while not state.stop_reason(quota, settings.call_limit(quota)):
                    if not tracker.allows(model, max_calls):
                        print("Stopped at the run cap. Resume with the same run ID.", flush=True)
                        return
                    if client is None:
                        client = MockClient() if mock else OpenRouterClient(settings.scoring)
                    count = min(settings.per_call, quota - len(state.accepted))
                    messages = build_messages(settings, domain, topic, count, state.accepted)
                    response = client.chat(model, messages)
                    cost = (response.get("usage") or {}).get("cost")
                    if cost is None and not mock:
                        cost = _estimate_call(settings, _input_tokens(messages))
                    tracker.add(model, float(cost) if cost is not None else None)
                    accepted, declined, generated = evaluate_reply(
                        response, settings, domain, topic, count, state, domain_counts, duplicates, model,
                    )
                    with journal:
                        journal.execute("INSERT INTO calls (domain, topic, accepted, rejected, generated) "
                                        "VALUES (?, ?, ?, ?, ?)",
                                        (domain, topic, json.dumps(accepted), json.dumps(declined), generated))
                    record(domain, topic, accepted, declined, generated)
        finally:
            _write_report(out, targets, progress, rejected, settings, tracker.spent_usd, pilot is not None)
