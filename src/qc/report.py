"""The tier-2 report and the listening sample.

Recording, participant and prompt IDs only: never names, phone numbers or transcript text.
"""

import csv
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from src.qc.scoring import REPORT_METRICS, ReportRow

LEVELS = (1, 2, 3, 4)
NA = "n/a"  # no level: task types tier 2 doesn't level (band_reason not_applicable)
SAMPLE_SEED = 20261110
SAMPLE_COLUMNS = (
    "recording_id", "participant_id", "task_type", "prompt_id", "level", "band_reason",
    "duration_s", "speech_ratio", "mean_dbfs", "snr_db", "word_count", "words_per_second",
    "avg_logprob", "compression_ratio", "cer", "wer", "length_ratio", "has_digits", "wav_path",
)  # fmt: skip


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _minutes(rows: Iterable[ReportRow]) -> float:
    return sum(r.metrics.get("duration_s") or 0.0 for r in rows) / 60


def _spread(values: list[float]) -> tuple[float, float, float] | None:
    """(median, p10, p90), or None if there are no values."""
    if not values:
        return None
    array = np.array(values, dtype=float)
    return float(np.median(array)), float(np.percentile(array, 10)), float(np.percentile(array, 90))


# ======
# REPORT
# ======
def render(rows: list[ReportRow], title: str, untranscribed: int | None = None) -> str:
    lines = [title]
    if untranscribed is not None:
        lines.append(f"  Eligible clips still without a transcript (waiting for the ASR batch): {untranscribed}")
    if not rows:
        return "\n".join([*lines, "  No scored clips."])
    by_type: dict[str, list[ReportRow]] = defaultdict(list)
    for row in rows:
        by_type[row.task_type].append(row)
    types = sorted(by_type)
    lines.append(f"  {len(rows)} clips, {_minutes(rows):.1f} audio minutes")

    # Levels per task type
    lines += ["", "Clips per task type and level (clips / minutes; n/a = no level for this task type):"]
    header = f"  {'task type':<22}" + "".join(f"{f'level {lvl}':>16}" for lvl in LEVELS) + f"{'n/a':>16}{'total':>16}"
    lines.append(header)
    for task_type in [*types, "all"]:
        group = rows if task_type == "all" else by_type[task_type]
        levelled = any(r.band is not None for r in group)
        cells = []
        for level in LEVELS:
            at = [r for r in group if r.band == level]
            cells.append(f"{len(at)} / {_minutes(at):.1f}" if levelled else "-")
        unlevelled = [r for r in group if r.band is None]
        cells.append(f"{len(unlevelled)} / {_minutes(unlevelled):.1f}" if unlevelled else "-")
        cells.append(f"{len(group)} / {_minutes(group):.1f}")
        lines.append(f"  {task_type:<22}" + "".join(f"{c:>16}" for c in cells))

    # Reasons
    lines += ["", "Most common reasons (below level 4):"]
    reasons = Counter(r.band_reason for r in rows if r.band is not None and r.band < 4)
    lines += [f"  {count:>5}  {reason}" for reason, count in reasons.most_common(10)] or ["  none"]

    # Metric distributions
    lines += ["", "Metrics per task type (median / p10 / p90, clips with a value):"]
    for task_type in types:
        lines.append(f"  {task_type}")
        for name in REPORT_METRICS:
            values = [r.metrics[name] for r in by_type[task_type] if r.metrics.get(name) is not None]
            spread = _spread(values)
            if spread is None:
                continue
            median, p10, p90 = spread
            lines.append(f"    {name:<20}{_fmt(median):>9}{_fmt(p10):>9}{_fmt(p90):>9}{len(values):>7}")
        digits = sum(1 for r in by_type[task_type] if r.metrics.get("has_digits"))
        if digits:
            lines.append(f"    {'has_digits':<20}{digits:>9} clip(s)")

    # Sentences: worst prompts by CER
    sentence_cer: dict[str, list[float]] = defaultdict(list)
    for row in by_type.get("sentence", []):
        if row.prompt_id and row.metrics.get("cer") is not None:
            sentence_cer[row.prompt_id].append(row.metrics["cer"])
    if sentence_cer:
        lines += ["", "Sentences with the highest median CER (candidates to fix or retire):"]
        worst = sorted(sentence_cer.items(), key=lambda kv: (-statistics.median(kv[1]), kv[0]))[:20]
        for prompt_id, values in worst:
            lines.append(f"  {prompt_id:<10} median CER {statistics.median(values):.2f}  ({len(values)} reading(s))")

    # Per participant
    lines += ["", "Per participant (clips per level; median CER of sentences):"]
    by_person: dict[str, list[ReportRow]] = defaultdict(list)
    for row in rows:
        by_person[row.participant_id].append(row)
    lines.append(f"  {'participant':<12}" + "".join(f"{f'L{lvl}':>6}" for lvl in LEVELS) + f"{NA:>6}{'CER':>8}")
    for person in sorted(by_person):
        group = by_person[person]
        counts = "".join(f"{sum(r.band == lvl for r in group):>6}" for lvl in LEVELS)
        counts += f"{sum(r.band is None for r in group):>6}"
        cers = [r.metrics["cer"] for r in group if r.task_type == "sentence" and r.metrics.get("cer") is not None]
        lines.append(f"  {person:<12}{counts}{_fmt(statistics.median(cers) if cers else None):>8}")
    return "\n".join(lines)


# ======
# SAMPLE
# ======
def write_sample(rows: list[ReportRow], per_group: int, path: Path, seed: int = SAMPLE_SEED) -> int:
    """Up to per_group recordings per task type and level (n/a for task types without a level),
    at random (fixed seed), for listening. wav_path is relative to the audio folder (data/audio).
    Returns rows written."""
    groups: dict[tuple[str, int], list[ReportRow]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: r.recording_id):
        groups[(row.task_type, 0 if row.band is None else row.band)].append(row)  # 0 sorts n/a first
    rng = random.Random(seed)
    chosen: list[ReportRow] = []
    for key in sorted(groups):
        group = groups[key]
        chosen += sorted(rng.sample(group, min(per_group, len(group))), key=lambda r: r.recording_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(SAMPLE_COLUMNS)
        for r in chosen:
            mt = r.metrics
            writer.writerow([
                r.recording_id, r.participant_id, r.task_type, r.prompt_id or "", NA if r.band is None else r.band,
                r.band_reason,
                *(_csv(mt.get(name)) for name in SAMPLE_COLUMNS[6:-1]),
                r.wav_path or "",
            ])  # fmt: skip
    return len(chosen)


def _csv(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
