"""Sample pilot clips for the QC calibration labelling (Stage 2).

Runs inside the api container on the droplet:

    docker compose exec api python -m scripts.labelling.sample_pilot_clips \
        --out /app/data/exports/pilot-sample-2026-11.csv

Eligible clips: real contributors (not is_test), main collection (not study clips), linked to a
prompt or free-speech invitation, not rejected on arrival (accepted or needs_review), recorded in
WhatsApp (not forwarded or uploaded), 5–60 seconds.

Selection per prompt category (defaults 150 / 75 / 75 / 75):
  sentence              every tier-2 level 1 and 2 clip, the rest split evenly between levels
                        3 and 4 at random (unscored clips only if those run out); at most one
                        clip per sentence where possible.
  image, conversation_starter, free_speech
                        half "worst audio" (highest noise floor, measured from the WAV; lowest
                        speech ratio breaks ties), half at random from the rest.
Within each group no speaker gets more than --max-speaker-share of the clips, unless there are
not enough other speakers. The seed makes the sample reproducible.

Writes the sample CSV (read directly by build_labelling_bundle.py) and, next to it,
<name>-strata.csv with how many eligible clips each group had, for reweighting.
Prints participant and recording IDs only.
"""

# ==========================================================================================
# SETTINGS
# ==========================================================================================
from __future__ import annotations

import argparse
import csv
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import text

from src.audio.levels import read_wav
from src.config import get_settings
from src.storage.db import get_sync_engine

FRAME_MS = 30
DEFAULT_SEED = 20261125
SPONTANEOUS = ("image", "conversation_starter", "free_speech")

CANDIDATES_SQL = text("""
    select r.id as recording_id, r.participant_id, a.prompt_type as task, a.prompt_id,
           r.qc_tier2 as level, r.duration_s, r.active_seconds, r.wav_path
    from recordings r
    join participants p on p.participant_id = r.participant_id and not p.is_test
    join enrollments e on e.id = r.enrollment_id and e.study = 'main'
    join assignments a on a.id = r.assignment_id
    where r.qc_status in ('accepted', 'needs_review')
      and r.audio_origin = 'recorded'
      and r.duration_s between 5 and 60
      and r.wav_path is not null
    order by r.id
""")

OUTPUT_COLUMNS = ["recording_id", "participant_id", "task_type", "prompt_id", "level", "selection",
                  "noise_floor_dbfs", "speech_ratio", "duration_s", "wav_path"]


@dataclass
class Clip:
    recording_id: int
    participant_id: str
    task: str
    prompt_id: str | None
    level: int | None
    duration_s: float
    active_seconds: float | None
    wav_path: str
    noise_floor_dbfs: float | None = None
    selection: str = ""

    @property
    def speech_ratio(self) -> float | None:
        if self.active_seconds is None or not self.duration_s:
            return None
        return self.active_seconds / self.duration_s


# ==========================================================================================
# AUDIO
# ==========================================================================================
def noise_floor_dbfs(path: Path) -> float | None:
    """10th percentile of 30 ms frame energy, in dBFS (the background level between words)."""
    try:
        wav = read_wav(path)
    except (OSError, ValueError):
        return None
    frame = int(wav.sample_rate * FRAME_MS / 1000)
    samples = np.asarray(wav.samples, dtype=np.float64) / wav.full_scale
    n = len(samples) // frame
    if n == 0:
        return None
    power = (samples[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
    return float(np.percentile(10 * np.log10(np.maximum(power, 1e-12)), 10))


# ==========================================================================================
# SELECTION
# ==========================================================================================
def take(pool: list[Clip], n: int, cap: int, counts: Counter, label: str,
         chosen_ids: set[int], one_per_prompt: set[str] | None = None) -> list[Clip]:
    """Take up to n clips from pool in order, respecting the per-speaker cap; relax the cap
    (and the one-per-prompt rule) only if the pool runs out."""
    picked: list[Clip] = []
    for strict in (True, False):
        for clip in pool:
            if len(picked) >= n:
                return picked
            if clip.recording_id in chosen_ids:
                continue
            if strict and counts[clip.participant_id] >= cap:
                continue
            if strict and one_per_prompt is not None and clip.prompt_id in one_per_prompt:
                continue
            clip.selection = label
            picked.append(clip)
            chosen_ids.add(clip.recording_id)
            counts[clip.participant_id] += 1
            if one_per_prompt is not None and clip.prompt_id:
                one_per_prompt.add(clip.prompt_id)
    return picked


def select_sentences(clips: list[Clip], n: int, share: float, rng: random.Random) -> list[Clip]:
    by_level: dict[int | None, list[Clip]] = defaultdict(list)
    for clip in clips:
        by_level[clip.level].append(clip)
    for group in by_level.values():
        rng.shuffle(group)
    cap = max(1, math.ceil(n * share))
    counts: Counter = Counter()
    chosen_ids: set[int] = set()
    prompts: set[str] = set()
    chosen: list[Clip] = []
    for level in (1, 2):  # the rare cases that set the CER cut-offs: take them all
        chosen += take(by_level[level], n - len(chosen), cap, counts, f"level-{level}",
                       chosen_ids, prompts)
    remaining = n - len(chosen)
    half = remaining // 2
    chosen += take(by_level[3], half, cap, counts, "level-3-random", chosen_ids, prompts)
    chosen += take(by_level[4], n - len(chosen), cap, counts, "level-4-random", chosen_ids, prompts)
    chosen += take(by_level[3], n - len(chosen), cap, counts, "level-3-random", chosen_ids, prompts)
    chosen += take(by_level[None], n - len(chosen), cap, counts, "unscored-random", chosen_ids, prompts)
    return chosen


def select_spontaneous(clips: list[Clip], n: int, share: float, worst_share: float,
                       rng: random.Random) -> list[Clip]:
    cap = max(1, math.ceil(n * share))
    counts: Counter = Counter()
    chosen_ids: set[int] = set()
    measured = [c for c in clips if c.noise_floor_dbfs is not None]
    worst = sorted(measured, key=lambda c: (-c.noise_floor_dbfs,
                                            c.speech_ratio if c.speech_ratio is not None else 1.0))
    n_worst = round(n * worst_share)
    chosen = take(worst, n_worst, cap, counts, "worst-noise", chosen_ids)
    rest = list(clips)
    rng.shuffle(rest)
    chosen += take(rest, n - len(chosen), cap, counts, "random", chosen_ids)
    return chosen


# ==========================================================================================
# MAIN
# ==========================================================================================
def load_candidates(exclude: set[str]) -> list[Clip]:
    with get_sync_engine().connect() as conn:
        rows = conn.execute(CANDIDATES_SQL).mappings().all()
    return [Clip(recording_id=r["recording_id"], participant_id=r["participant_id"], task=r["task"],
                 prompt_id=r["prompt_id"], level=r["level"], duration_s=float(r["duration_s"] or 0),
                 active_seconds=r["active_seconds"], wav_path=r["wav_path"])
            for r in rows if r["participant_id"] not in exclude]


def write_outputs(chosen: list[Clip], candidates: list[Clip], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", newline="", encoding="utf-8") as handle:  # never overwrite a sample
        writer = csv.writer(handle)
        writer.writerow(OUTPUT_COLUMNS)
        for c in sorted(chosen, key=lambda c: (c.task, c.selection, c.recording_id)):
            ratio = c.speech_ratio
            writer.writerow([c.recording_id, c.participant_id, c.task, c.prompt_id or "",
                             "n/a" if c.level is None else c.level, c.selection,
                             "" if c.noise_floor_dbfs is None else round(c.noise_floor_dbfs, 1),
                             "" if ratio is None else round(ratio, 3), round(c.duration_s, 1),
                             c.wav_path])
    strata = out.with_name(out.stem + "-strata.csv")
    eligible = Counter((c.task, "n/a" if c.level is None else c.level) for c in candidates)
    sampled = Counter((c.task, "n/a" if c.level is None else c.level) for c in chosen)
    with strata.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["task_type", "level", "eligible", "sampled"])
        for key in sorted(eligible, key=str):
            writer.writerow([*key, eligible[key], sampled.get(key, 0)])
    return strata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="output CSV (must not exist)")
    parser.add_argument("--sentences", type=int, default=150)
    parser.add_argument("--pictures", type=int, default=75)
    parser.add_argument("--starters", type=int, default=75)
    parser.add_argument("--free-speech", type=int, default=75)
    parser.add_argument("--worst-share", type=float, default=0.5,
                        help="share of each spontaneous group chosen as worst audio (default 0.5)")
    parser.add_argument("--max-speaker-share", type=float, default=0.15,
                        help="most clips one speaker may have in a group (default 0.15)")
    parser.add_argument("--exclude", default="", help="participant IDs to leave out, comma-separated")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    exclude = {p.strip().upper() for p in args.exclude.split(",") if p.strip()}
    candidates = load_candidates(exclude)
    audio_root = get_settings().audio_root
    by_task: dict[str, list[Clip]] = defaultdict(list)
    for clip in candidates:
        by_task[clip.task].append(clip)

    targets = {"sentence": args.sentences, "image": args.pictures,
               "conversation_starter": args.starters, "free_speech": args.free_speech}
    chosen: list[Clip] = []
    chosen += select_sentences(by_task["sentence"], targets["sentence"], args.max_speaker_share, rng)
    for task in SPONTANEOUS:
        for clip in by_task[task]:
            clip.noise_floor_dbfs = noise_floor_dbfs(audio_root / clip.wav_path)
        chosen += select_spontaneous(by_task[task], targets[task], args.max_speaker_share,
                                     args.worst_share, rng)

    strata = write_outputs(chosen, candidates, args.out)
    print(f"eligible clips: {len(candidates)}; sampled: {len(chosen)} (seed {args.seed})")
    summary = Counter((c.task, c.selection) for c in chosen)
    for task in ("sentence", *SPONTANEOUS):
        got = sum(v for (t, _), v in summary.items() if t == task)
        parts = ", ".join(f"{sel} {v}" for (t, sel), v in sorted(summary.items()) if t == task)
        print(f"  {task:<22} {got:>4} of {targets[task]:<4} (available {len(by_task[task])}): {parts}")
    speakers = Counter(c.participant_id for c in chosen)
    print(f"speakers: {len(speakers)}; most clips from one speaker: {max(speakers.values(), default=0)}")
    print(f"sample: {args.out}\nstrata: {strata}")


if __name__ == "__main__":
    main()
