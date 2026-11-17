"""Report sentence collection gaps and prepare blinded review batches."""

from __future__ import annotations

import random
from collections import Counter, defaultdict, deque
from pathlib import Path

from src.prompt_generation.text.domains import read_kept
from src.prompt_generation.text.labels import LABEL_COLUMNS, LabelSettings, valid_labels
from src.prompt_generation.text.pool import output_lock
from src.prompt_generation.text.sentences import _write_csv

GAP_COLUMNS = ["domain", "target", "live_active", "new_available", "gap"]
TOPIC_COLUMNS = ["domain", "topic", "live_active", "new_available"]
MANIFEST_COLUMNS = [
    "candidate_id", "text", "source_category", "source_ref", "origin", "read_only",
    "overlap", "word_count", "est_read_s",
]


def gap_counts(rows: list[dict], labels: dict[str, dict], settings: LabelSettings) -> tuple[list[dict], list[dict]]:
    counts = Counter()
    for row in rows:
        label = labels.get(row["pool_id"])
        if label and row["status"] in {"active", "available"}:
            counts[row["origin"], label["domain"], label["topic"]] += 1

    domains, topics = [], []
    for domain, target in settings.targets.items():
        domain_topics = [
            {
                "domain": domain, "topic": topic,
                "live_active": counts["live", domain, topic],
                "new_available": counts["new", domain, topic],
            }
            for topic in (*settings.topics[domain], "other")
        ]
        live = sum(row["live_active"] for row in domain_topics)
        domains.append({
            "domain": domain, "target": target, "live_active": live, "gap": max(target - live, 0),
            "new_available": sum(row["new_available"] for row in domain_topics),
        })
        topics.extend(domain_topics)
    return domains, topics


def write_gaps(pool: Path, settings: LabelSettings) -> None:
    with output_lock(pool):
        rows = read_kept(pool / "pool.csv")
        labels = valid_labels(pool / "labels-model.csv", settings)
        domains, topics = gap_counts(rows, labels, settings)
        _write_csv(pool / "gaps-by-topic.csv", topics, TOPIC_COLUMNS)
        _write_csv(pool / "gaps.csv", domains, GAP_COLUMNS)
    unclassified = Counter(row["origin"] for row in rows
                           if row["status"] != "retired" and row["pool_id"] not in labels)
    print(f"Gaps for pool {pool.name}: {sum(row['gap'] for row in domains)} sentences")
    print(f"  Without a valid current label: {unclassified['live']} active live, {unclassified['new']} new")


def allocate(domains: list[dict], size: int) -> dict[str, int]:
    """Apply availability caps and minimums to proportional domain quotas."""
    available = {row["domain"]: row["new_available"] for row in domains}
    gaps = {row["domain"]: row["gap"] for row in domains}
    budget = min(size, sum(available.values()))
    minimum = {key: min(50, available[key]) for key in available}
    priority = sorted(available, key=lambda key: (-gaps[key], key))
    if sum(minimum.values()) > budget:
        # With a small budget, spread the achievable minimum across domains.
        allocation = dict.fromkeys(available, 0)
        for level in range(50):
            for key in priority:
                if minimum[key] > level and budget:
                    allocation[key] += 1
                    budget -= 1
        return allocation

    # Pin domains whose proportional share falls below their minimum, then
    # recompute the shares for the remaining domains.
    weighted = {key for key in available if available[key]}
    allocation = dict.fromkeys(available, 0)
    remaining = budget
    while weighted:
        total_gap = sum(gaps[key] for key in weighted)
        if not total_gap:
            for key in weighted:
                allocation[key] = minimum[key]
            break
        below_minimum = {
            key for key in weighted if remaining * gaps[key] < minimum[key] * total_gap
        }
        if not below_minimum:
            for key in weighted:
                allocation[key] = min(available[key], remaining * gaps[key] // total_gap)
            break
        for key in below_minimum:
            allocation[key] = minimum[key]
            remaining -= minimum[key]
        weighted -= below_minimum
    used = sum(allocation.values())
    while used < budget:
        key = max((key for key in priority if allocation[key] < available[key]),
                  key=lambda key: (max(gaps[key] - allocation[key], 0), available[key] - allocation[key]))
        allocation[key] += 1
        used += 1
    return allocation


def _pick_topics(topics: dict[str, list[dict]], size: int, rng: random.Random) -> list[dict]:
    topic_order = sorted(topics)
    rng.shuffle(topic_order)
    queues = deque()
    for topic in topic_order:
        group = topics[topic]
        rng.shuffle(group)
        queues.append(group)
    picked = []
    for _ in range(size):
        group = queues.popleft()
        picked.append(group.pop())
        if group:
            queues.append(group)
    return picked


def write_sentence_batch(pool: Path, out: Path, settings: LabelSettings, *, size: int,
                         live_sample: int = 0, seed: int = 42) -> None:
    if pool.resolve() == out.resolve():
        raise ValueError("The review directory must differ from the pool directory")
    with output_lock(out):
        if (out / "READY").exists():
            raise FileExistsError(f"{out} is already marked READY; choose a new --batch name")
        with output_lock(pool):
            rows = sorted(read_kept(pool / "pool.csv"), key=lambda row: row["pool_id"])
            labels = valid_labels(pool / "labels-model.csv", settings)
        domains, _ = gap_counts(rows, labels, settings)
        allocation = allocate(domains, size)
        groups = defaultdict(lambda: defaultdict(list))
        live = []
        for row in rows:
            label = labels.get(row["pool_id"])
            if row["origin"] == "new" and row["status"] == "available" and label:
                groups[label["domain"]][label["topic"]].append(row)
            elif row["origin"] == "live" and row["status"] == "active":
                live.append(row)

        rng = random.Random(seed)
        picked = []
        for domain, count in allocation.items():
            picked.extend(_pick_topics(groups[domain], count, rng))
        picked.extend(rng.sample(live, min(live_sample, len(live))))
        overlap = {row["pool_id"] for row in rng.sample(picked, (len(picked) + 5) // 10)}
        rng.shuffle(picked)
        manifest = [
            {
                **row, "candidate_id": row["pool_id"],
                "read_only": "yes" if row["origin"] == "live" else "no",
                "overlap": "yes" if row["pool_id"] in overlap else "no",
            }
            for row in picked
        ]
        _write_csv(out / "manifest.csv", manifest, MANIFEST_COLUMNS)
        model_rows = [labels[row["pool_id"]] for row in picked if row["pool_id"] in labels]
        _write_csv(out / "model-labels.csv", model_rows, LABEL_COLUMNS)
        (out / "READY").write_text("ready\n", encoding="utf-8")

    print(f"Batch {out}: {sum(allocation.values())} new + {min(live_sample, len(live))} live sentences, "
          f"{len(overlap)} overlap")
    print(f"  {'Domain':<17} {'Target':>7} {'Gap':>7} {'Available':>10} {'Sampled':>8}")
    for row in domains:
        domain = row["domain"]
        print(f"  {domain:<17} {row['target']:>7} {row['gap']:>7} "
              f"{row['new_available']:>10} {allocation[domain]:>8}")
