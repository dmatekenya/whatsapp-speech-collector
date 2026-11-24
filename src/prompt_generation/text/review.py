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
    "candidate_id", "text", "source_category", "source_ref", "origin",
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


def _largest_remainder(weights: dict[str, int], size: int) -> dict[str, int]:
    total = sum(weights.values())
    if not total:
        weights = dict.fromkeys(weights, 1)
        total = len(weights)
    quotas = {domain: divmod(size * weight, total) for domain, weight in weights.items()}
    places = {domain: whole for domain, (whole, _) in quotas.items()}
    priority = sorted(quotas, key=lambda domain: (-quotas[domain][1], domain))
    for domain in priority[:size - sum(places.values())]:
        places[domain] += 1
    return places


def allocate_target_share(targets: dict[str, int], available: dict[str, int],
                          size: int) -> tuple[dict[str, int], dict[str, int]]:
    """Return the original quotas and the allocation after applying capacity caps."""
    target_places = _largest_remainder(targets, size)
    final_places = {domain: min(places, available[domain]) for domain, places in target_places.items()}
    budget = min(size, sum(available.values()))
    while (remaining := budget - sum(final_places.values())) > 0:
        weights = {domain: targets[domain] for domain in targets if final_places[domain] < available[domain]}
        for domain, places in _largest_remainder(weights, remaining).items():
            final_places[domain] = min(final_places[domain] + places, available[domain])
    return target_places, final_places


def _unrecorded(row: dict) -> bool:
    count = row.get("times_recorded")
    if count not in (None, ""):
        return int(count) == 0
    # Older pools only store the boolean derived from the production count.
    return row.get("in_use", "").lower() != "true"


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
                         allocation: str = "target-share", seed: int = 42) -> None:
    if pool.resolve() == out.resolve():
        raise ValueError("The review directory must differ from the pool directory")
    with output_lock(out):
        if (out / "READY").exists():
            raise FileExistsError(f"{out} is already marked READY; choose a new --batch name")
        with output_lock(pool):
            rows = sorted(read_kept(pool / "pool.csv"), key=lambda row: row["pool_id"])
            labels = valid_labels(pool / "labels-model.csv", settings)
        groups = defaultdict(lambda: defaultdict(list))
        counts = Counter()
        for row in rows:
            label = labels.get(row["pool_id"])
            if label and row["status"] != "retired" and _unrecorded(row):
                groups[label["domain"]][label["topic"]].append(row)
                counts[label["domain"], row["origin"]] += 1

        available = {domain: counts[domain, "live"] + counts[domain, "new"] for domain in settings.targets}
        if allocation == "gap":
            domains, _ = gap_counts(rows, labels, settings)
            weights = {row["domain"]: row["gap"] for row in domains}
            target_places = allocate([{**row, "new_available": size} for row in domains], size)
            final_places = allocate([{**row, "new_available": available[row["domain"]]} for row in domains], size)
        else:
            weights = settings.targets
            target_places, final_places = allocate_target_share(weights, available, size)

        rng = random.Random(seed)
        picked = []
        for domain, count in final_places.items():
            picked.extend(_pick_topics(groups[domain], count, rng))
        overlap = {row["pool_id"] for row in rng.sample(picked, (len(picked) + 5) // 10)}
        rng.shuffle(picked)
        manifest = [
            {
                **row,
                "candidate_id": (row.get("prompt_id") or row["pool_id"])
                if row["origin"] == "live" else row["pool_id"],
                "overlap": "yes" if row["pool_id"] in overlap else "no",
            }
            for row in picked
        ]
        _write_csv(out / "manifest.csv", manifest, MANIFEST_COLUMNS)
        model_rows = [labels[row["pool_id"]] for row in picked]
        _write_csv(out / "model-labels.csv", model_rows, LABEL_COLUMNS)
        (out / "READY").write_text("ready\n", encoding="utf-8")

    print(f"Batch {out}: {len(picked)} sentences, {len(overlap)} overlap")
    print("Available candidates by origin")
    print(f"  {'Domain':<17} {'Live':>7} {'New':>7}")
    for domain in available:
        print(f"  {domain:<17} {counts[domain, 'live']:>7} {counts[domain, 'new']:>7}")
    print(f"Allocation ({allocation})")
    print(f"  {'Domain':<17} {'Share':>7} {'Target places':>13} {'Available':>10} {'Final places':>12}")
    total_weight = sum(weights.values())
    for domain in available:
        share = weights[domain] / total_weight if total_weight else 0
        print(f"  {domain:<17} {share:>7.1%} {target_places[domain]:>13} "
              f"{available[domain]:>10} {final_places[domain]:>12}")
