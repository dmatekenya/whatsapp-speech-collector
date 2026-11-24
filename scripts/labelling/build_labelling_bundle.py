"""Build a blind audio bundle and Label Studio task files for the QC calibration labelling.

Runs on the droplet host with plain Python 3 (standard library only), from the repo folder:

    python3 build_labelling_bundle.py \
        --calibration data/exports/qc-calibration-2026-11-23/clips.csv \
        --pilot data/exports/pilot-sample.csv \
        --name qc-2026-11 --exclude P0020,P0023

Writes:
  data/labelling/bundles/<name>/<random-id>.wav   copies, random names (mounted into Label Studio)
  data/labelling/tasks/<name>-pass1.json          every clip, shuffled
  data/labelling/tasks/<name>-pass2.json          pilot sentence clips only (transcription)
  data/labelling/keys/<name>-key.csv              private: random id -> recording, speaker,
                                                  condition (never upload this to Label Studio)
"""

# ==========================================================================================
# SETTINGS
# ==========================================================================================
import argparse
import csv
import json
import os
import random
import re
import shutil
import uuid
from pathlib import Path

AUDIO_ROOT = Path("data/audio")
LABELLING = Path("data/labelling")


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def recording_id(wav_path: str) -> str:
    match = re.search(r"_R0*(\d+)\.wav$", wav_path)
    return match.group(1) if match else ""


# ==========================================================================================
# COLLECT CLIPS
# ==========================================================================================
def collect(args: argparse.Namespace) -> list[dict]:
    excluded = {p.strip() for p in args.exclude.split(",") if p.strip()}
    clips = []

    staged = [r for r in read_csv(Path(args.calibration)) if r["participant_id"] not in excluded]
    latest: dict[tuple, dict] = {}
    for row in staged:  # keep the latest attempt per speaker, condition and task slot
        key = (row["participant_id"], row["condition"], row["task"], row.get("task_position", ""))
        if key not in latest or int(row["attempt"]) > int(latest[key]["attempt"]):
            latest[key] = row
    for row in latest.values():
        clips.append({"source": "staged", "participant_id": row["participant_id"],
                      "condition": row["condition"], "task": row["task"],
                      "wav_path": row["wav_path"], "recording_id": recording_id(row["wav_path"]),
                      "tier2": ""})

    if args.pilot:
        for row in read_csv(Path(args.pilot)):
            if row["participant_id"] in excluded:
                continue
            clips.append({"source": "pilot", "participant_id": row["participant_id"],
                          "condition": "", "task": row["task"], "wav_path": row["wav_path"],
                          "recording_id": row.get("recording_id") or recording_id(row["wav_path"]),
                          "tier2": row.get("tier2", "")})
    return clips


# ==========================================================================================
# WRITE BUNDLE, TASKS AND KEY
# ==========================================================================================
def build(args: argparse.Namespace) -> None:
    clips = collect(args)
    bundle = LABELLING / "bundles" / args.name
    tasks_dir, keys_dir = LABELLING / "tasks", LABELLING / "keys"
    bundle.mkdir(parents=True, exist_ok=False)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    keys_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(keys_dir, 0o700)

    rng = random.SystemRandom()
    rng.shuffle(clips)
    missing = 0
    pass1, pass2, key_rows = [], [], []
    for clip in clips:
        source = AUDIO_ROOT / clip["wav_path"]
        if not source.exists():
            missing += 1
            continue
        blind_id = uuid.uuid4().hex[:12]
        shutil.copyfile(source, bundle / f"{blind_id}.wav")
        task = {"data": {"audio": f"/data/local-files/?d={args.name}/{blind_id}.wav",
                         "clip": blind_id}}
        pass1.append(task)
        if clip["source"] == "pilot" and clip["task"] == "sentence":
            pass2.append(task)
        key_rows.append({"clip": blind_id, **clip})

    (tasks_dir / f"{args.name}-pass1.json").write_text(json.dumps(pass1, indent=1))
    rng.shuffle(pass2)
    (tasks_dir / f"{args.name}-pass2.json").write_text(json.dumps(pass2, indent=1))

    key_path = keys_dir / f"{args.name}-key.csv"
    fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["clip", "source", "participant_id", "condition", "task",
                                         "recording_id", "tier2", "wav_path"])
        writer.writeheader()
        writer.writerows(key_rows)

    staged = sum(r["source"] == "staged" for r in key_rows)
    print(f"{len(key_rows)} clips ({staged} staged, {len(key_rows) - staged} pilot), "
          f"{missing} missing audio skipped")
    print(f"pass 1 tasks: {len(pass1)}  ->  {tasks_dir / f'{args.name}-pass1.json'}")
    print(f"pass 2 tasks: {len(pass2)}  ->  {tasks_dir / f'{args.name}-pass2.json'}")
    print(f"private key:  {key_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calibration", required=True, help="clips.csv from export_calibration_clips")
    parser.add_argument("--pilot", help="pilot sample CSV (participant_id, recording_id, wav_path, task, tier2)")
    parser.add_argument("--name", required=True, help="bundle name, e.g. qc-2026-11")
    parser.add_argument("--exclude", default="", help="participant IDs to leave out, comma-separated")
    build(parser.parse_args())
