"""Calibration for domain scoring: a labelling page, and a report comparing models with
your labels."""

from __future__ import annotations
import random
import csv
import html
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .picture_scoring import FLAG_NAMES, NOT_RELEVANT, UNCLEAR


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ==============
# LABELLING PAGE
# ==============


def build_label_sheet(run_dir: Path, domains: list[dict[str, str]],
                      sample_per_domain: int | None = None, seed: int = 42) -> Path:
    """Write label-sheet.html: thumbnails with a domain dropdown and a CSV export button.

    Open it in a browser from the run folder, label, then click Export and save the file as
    calibration-labels.csv in the same folder. Choices are kept in the browser between visits,
    and labels already in calibration-labels.csv are pre-filled.

    With sample_per_domain, only a fixed random sample per search domain is shown (the same
    seed gives the same sample); images already labelled are always included.
    """
    rows = [r for r in _read_csv(run_dir / "candidates.csv") if r["status"] == "kept"]
    existing = {r["candidate_id"]: r for r in _read_csv(run_dir / "calibration-labels.csv")}

    if sample_per_domain:
        rng = random.Random(seed)
        by_domain: dict[str, list[dict[str, str]]] = defaultdict(list)
        for r in rows:
            by_domain[r["query_domain"]].append(r)
        picked = []
        for key in sorted(by_domain):
            group = by_domain[key]
            labelled = [r for r in group if r["candidate_id"] in existing]
            rest = [r for r in group if r["candidate_id"] not in existing]
            n = max(sample_per_domain - len(labelled), 0)
            picked += labelled + rng.sample(rest, min(n, len(rest)))
        rng.shuffle(picked)  # mix domains so the page order doesn't hint at the answer
        rows = picked

    items = [
        {
            "id": r["candidate_id"],
            "title": r.get("title", ""),
            "thumb": f"thumbs/{r['candidate_id']}.jpg",
            "url": r.get("source_url", ""),
            "label": existing.get(r["candidate_id"], {}).get("label_domain", ""),
            "notes": existing.get(r["candidate_id"], {}).get("notes", ""),
        }
        for r in rows
    ]
    options = [{"key": d["key"], "scope": d["scope"]} for d in domains]
    options.append({"key": UNCLEAR, "scope": "No clear activity or subject"})
    options.append({"key": NOT_RELEVANT, "scope": "Not usable as a prompt for this project"})

    page = _SHEET_TEMPLATE.replace("__RUN__", html.escape(run_dir.name))
    page = page.replace("__ITEMS__", json.dumps(items)).replace("__OPTIONS__", json.dumps(options))
    out = run_dir / "label-sheet.html"
    out.write_text(page, encoding="utf-8")
    return out

_SHEET_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Domain labels: __RUN__</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #f6f6f4; color: #222; }
  header { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #ddd;
           padding: 10px 16px; display: flex; gap: 16px; align-items: center; z-index: 1; }
  header h1 { font-size: 16px; margin: 0; }
  #progress { color: #555; font-size: 14px; }
  button { font-size: 14px; padding: 6px 12px; cursor: pointer; }
  details { font-size: 13px; }
  main { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
         gap: 12px; padding: 16px; }
  .card { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 8px;
          display: flex; flex-direction: column; gap: 6px; }
  .card.done { border-color: #3a7d44; }
  .card img { width: 100%; height: 200px; object-fit: contain; background: #eee; cursor: zoom-in; }
  .card .title { font-size: 12px; color: #444; min-height: 2.4em; }
  .card select, .card input { font-size: 13px; width: 100%; box-sizing: border-box; }
  #zoom { position: fixed; inset: 0; background: rgba(0,0,0,.85); display: none;
          align-items: center; justify-content: center; z-index: 2; }
  #zoom img { max-width: 95vw; max-height: 95vh; }
</style></head>
<body>
<header>
  <h1>Domain labels: __RUN__</h1>
  <span id="progress"></span>
  <button id="export">Export calibration-labels.csv</button>
  <details><summary>Domains</summary><ul id="legend"></ul></details>
</header>
<main id="grid"></main>
<div id="zoom"><img alt=""></div>
<script>
const ITEMS = __ITEMS__;
const OPTIONS = __OPTIONS__;
const STORE = "domain-labels:__RUN__";
let saved = {};
try { saved = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { saved = {}; }
const state = {};
ITEMS.forEach(it => {
  const s = saved[it.id] || {};
  state[it.id] = { label: s.label ?? it.label, notes: s.notes ?? it.notes };
});
function persist() { try { localStorage.setItem(STORE, JSON.stringify(state)); } catch (e) {} }
function progress() {
  const n = Object.values(state).filter(s => s.label).length;
  document.getElementById("progress").textContent = n + " of " + ITEMS.length + " labelled";
}
const legend = document.getElementById("legend");
OPTIONS.forEach(o => {
  const li = document.createElement("li");
  li.textContent = o.key + ": " + o.scope;
  legend.appendChild(li);
});
const grid = document.getElementById("grid");
ITEMS.forEach(it => {
  const card = document.createElement("div");
  card.className = "card" + (state[it.id].label ? " done" : "");
  const img = document.createElement("img");
  img.src = it.thumb; img.alt = it.title; img.loading = "lazy";
  img.onclick = () => { const z = document.getElementById("zoom");
    z.querySelector("img").src = it.thumb; z.style.display = "flex"; };
  const title = document.createElement("div");
  title.className = "title";
  const a = document.createElement("a");
  a.href = it.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = it.title || it.id;
  title.appendChild(a);
  const sel = document.createElement("select");
  sel.appendChild(new Option("(choose domain)", ""));
  OPTIONS.forEach(o => sel.appendChild(new Option(o.key, o.key)));
  sel.value = state[it.id].label || "";
  sel.onchange = () => { state[it.id].label = sel.value;
    card.className = "card" + (sel.value ? " done" : ""); persist(); progress(); };
  const notes = document.createElement("input");
  notes.placeholder = "notes (optional)"; notes.value = state[it.id].notes || "";
  notes.oninput = () => { state[it.id].notes = notes.value; persist(); };
  card.append(img, title, sel, notes);
  grid.appendChild(card);
});
document.getElementById("zoom").onclick = e => { e.currentTarget.style.display = "none"; };
function csvCell(v) { v = String(v ?? ""); return /[",\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
document.getElementById("export").onclick = () => {
  const lines = ["candidate_id,label_domain,notes"];
  ITEMS.forEach(it => lines.push([it.id, state[it.id].label, state[it.id].notes].map(csvCell).join(",")));
  const blob = new Blob([lines.join("\\n") + "\\n"], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob); link.download = "calibration-labels.csv"; link.click();
};
progress();
</script>
</body></html>
"""


# ======
# REPORT
# ======


@dataclass
class ModelResult:
    model: str
    include_metadata: str
    labelled: int = 0
    scored: int = 0
    correct: int = 0
    top2: int = 0
    confident: int = 0
    confident_correct: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    confusions: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)

    @property
    def accuracy(self) -> float:
        return self.correct / self.scored if self.scored else 0.0

    @property
    def top2_accuracy(self) -> float:
        return self.top2 / self.scored if self.scored else 0.0

    @property
    def coverage(self) -> float:
        return self.scored / self.labelled if self.labelled else 0.0

    @property
    def confident_accuracy(self) -> float:
        return self.confident_correct / self.confident if self.confident else 0.0


def evaluate(run_dir: Path, confident_threshold: float = 0.8) -> tuple[dict, list[ModelResult]]:
    """Compare each (model, metadata setting) with the labels; also the query-domain baseline."""
    labels = {
        r["candidate_id"]: r["label_domain"]
        for r in _read_csv(run_dir / "calibration-labels.csv") if r.get("label_domain")
    }
    candidates = {r["candidate_id"]: r for r in _read_csv(run_dir / "candidates.csv")}

    base_correct = sum(
        1 for cid, lab in labels.items() if candidates.get(cid, {}).get("query_domain") == lab
    )
    baseline = {
        "labelled": len(labels),
        "query_domain_accuracy": base_correct / len(labels) if labels else 0.0,
        "label_counts": Counter(labels.values()),
    }

    latest: dict[tuple[str, str, str], dict[str, str]] = {}
    costs: dict[tuple[str, str], float] = defaultdict(float)
    tokens: dict[tuple[str, str], int] = defaultdict(int)
    for r in _read_csv(run_dir / "domain-scores.csv"):
        key = (r["model"], r["include_metadata"])
        costs[key] += float(r.get("cost_usd") or 0)
        tokens[key] += int(r.get("prompt_tokens") or 0)
        if r["status"] == "ok":
            latest[(r["model"], r["include_metadata"], r["candidate_id"])] = r

    results: dict[tuple[str, str], ModelResult] = {}
    for key in costs:
        results[key] = ModelResult(model=key[0], include_metadata=key[1], labelled=len(labels),
                                   cost_usd=costs[key], tokens_in=tokens[key])
    for (model, meta, cid), r in latest.items():
        res = results[(model, meta)]
        res.flags.update(name for name in FLAG_NAMES if r.get(f"flag_{name}") == "true")
        if cid not in labels:
            continue
        lab, pred = labels[cid], r["domain"]
        res.scored += 1
        if pred == lab:
            res.correct += 1
        else:
            res.confusions[(lab, pred)] += 1
        if lab in (pred, r.get("second_choice")):
            res.top2 += 1
        if float(r.get("confidence") or 0) >= confident_threshold:
            res.confident += 1
            res.confident_correct += int(pred == lab)

    ordered = sorted(results.values(), key=lambda m: (-m.accuracy, m.cost_usd))
    return baseline, ordered


def format_report(baseline: dict, results: list[ModelResult]) -> str:
    lines = [
        "# Domain scoring evaluation",
        "",
        f"Labelled images: {baseline['labelled']}. "
        f"Query-domain baseline (domain of the search that found the photo): "
        f"{baseline['query_domain_accuracy']:.0%} correct.",
        "",
        "| Model | Metadata | Scored | Accuracy | Top-2 | Conf. >= 0.8 (share, accuracy) | Cost (USD) |",
        "|---|---|---:|---:|---:|---|---:|",
    ]
    for m in results:
        share = m.confident / m.scored if m.scored else 0
        lines.append(
            f"| {m.model} | {m.include_metadata} | {m.scored}/{m.labelled} | {m.accuracy:.0%} | "
            f"{m.top2_accuracy:.0%} | {share:.0%}, {m.confident_accuracy:.0%} | {m.cost_usd:.4f} |"
        )
    lines += ["", "## Flags per model", "",
              "Counts use the latest successful score per candidate and metadata setting, "
              "including unlabelled images. Blank legacy flags are not counted.", "",
              "| Model | Metadata | Sensitive | Graphic or sexual | Visible text | Poor quality |",
              "|---|---|---:|---:|---:|---:|"]
    for m in results:
        counts = " | ".join(str(m.flags[name]) for name in FLAG_NAMES)
        lines.append(f"| {m.model} | {m.include_metadata} | {counts} |")
    lines += ["", "## Most common confusions (your label -> model's answer)", ""]
    for m in results:
        top = ", ".join(f"{a} -> {b} ({n})" for (a, b), n in m.confusions.most_common(5)) or "none"
        lines.append(f"- **{m.model}** (metadata {m.include_metadata}): {top}")
    lines += ["", "## Labels per domain", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(baseline["label_counts"].items())]
    return "\n".join(lines) + "\n"
