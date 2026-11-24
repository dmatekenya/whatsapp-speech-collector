# Conversation starters: AI generation for human review

Goal: 7,000 short, light Chichewa conversation starters that pass the automatic filters, then
reviewed by annotators on the platform. The manual needs 5,250 accepted, so at least 75% of the
7,000 must pass human review. The paid pilot measures that rate before the full run.

## What we learned from Gloria's 382 starters

Good subjects, but most read like exam or debate questions: long, abstract, and they open with
"Kodi n'chifukwa chiyani…" or "Kodi … bwanji …?". About half are about society in general
("boma lingachite chiyani…") rather than the speaker's own life. Also in the file: row 369 is an
instruction line, not a starter; the last 14 rows have no topic; and the environment questions
(rows 289–368) are labelled Zachikhalidwe. Use her file as examples of subjects, not of style.

## Style rules (for the generation prompt)

1. Short: 6–15 words, one main question or invitation. At most one short follow-up ("…ndipo
   chifukwa chiyani?" sparingly).
2. Light and personal: everyday life, memories, routines, likes, places, small stories. The
   speaker should be able to talk for 20–40 seconds without needing knowledge or an opinion on
   policy.
3. Not exam-like: no "Fotokozani ubwino ndi kuipa…", no "Kodi boma lingachite chiyani…", no
   definitions ("…amatanthauza chiyani?"), no yes/no questions.
4. Vary the openings. Mix questions and invitations: Tiuzeni za…, Fotokozani…, Mumakonda…,
   Ndi chiyani…, Kumbukirani nthawi…, Mukanakhala ndi…, Ngati… Limit "Kodi" openings to about a
   quarter of a batch.
5. Safe: no personal identifiers, no illness, money troubles, grief or relationship problems of
   the speaker, no assumptions about marriage, children or income ("…anu" only for things almost
   everyone has). No politics or parties (pending the project's decision). No religion-specific
   assumptions.
6. Natural Malawian Chichewa, everyday words; English loanwords only where people really use them
   (foni, minibasi, sukulu).
7. Each starter must fit its given domain and topic.

## Examples (draft style anchors; to be checked by a native speaker before use)

| Domain | Starter |
|---|---|
| family | Tiuzeni za mnzanu wapamtima ndi zimene mumakonda kuchita limodzi. |
| food | Ndi chakudya chiti chimene mumachikonda kwambiri? Mumachiphika bwanji? |
| health | Mumatani kuti mupumule mukatopa kwambiri? |
| agriculture | Fotokozani tsiku limodzi lotanganidwa pa nthawi yokolola. |
| work-trade | Tiuzeni za ntchito yoyamba imene munagwirapo. |
| education | Tiuzeni za mphunzitsi amene simudzamuiwala. |
| transport | Fotokozani ulendo wina wa pa minibasi umene simudzauiwala. |
| sport-leisure | Mumakonda kuchita chiyani Loweruka masana? |
| nature | Kodi nyengo ya mvula imasintha bwanji zochita zanu? |
| technology | Tiuzeni mmene foni yanu imakuthandizirani tsiku ndi tsiku. |
| culture | Fotokozani ukwati wina umene munapitako ndi zimene zinakusangalatsani. |
| public-services | Fotokozani mmene anthu amatungira madzi m'dera lanu. |

Gloria's lighter ones that fit the style: "Fotokozani mbiri yanu yamaphunziro",
"Kodi ndi makhalidwe ati amene mumanyadira nawo kwambiri pa chikhalidwe cha ku Malawi?" (shortened).

## Quotas (7,000 by the manual's domain shares)

| Domain | Share | Starters |
|---|---|---|
| family | 15% | 1,050 |
| food | 12% | 840 |
| health | 12% | 840 |
| agriculture | 11% | 770 |
| work-trade | 10% | 700 |
| education | 10% | 700 |
| transport | 7% | 490 |
| sport-leisure | 7% | 490 |
| nature | 6% | 420 |
| technology | 4% | 280 |
| culture | 4% | 280 |
| public-services | 2% | 140 |
| **Total** | **100%** | **7,000** |

Within a domain, split evenly across its draft topics (`config/topics-draft.yaml`), plus about
10% with no fixed topic ("anything in this domain") for variety.

## Copilot prompt

> Add `scripts/prompt_generation/generate_starters.py` (reuse the OpenRouter client, cost cap,
> resumable persistence and banner style from `extract_sentences label`).
>
> 1. Config `config/starter-generation.yaml`: model, temperature 0.9, `total: 7000`, domain shares
>    (same as `domain_targets` shares in `config/sentence-extraction.yaml`), `per_call: 20`,
>    style rules and examples (copy them from `docs/prompt-generation/starter-generation-plan.md`),
>    cost cap, `max_tokens`, reasoning off.
> 2. Quotas: per domain by share (largest-remainder rounding); within a domain, even split across
>    its topics in `config/topics-draft.yaml` plus a 10% "any topic" slot.
> 3. Each call asks for `min(per_call, remaining quota)` starters for one domain + topic, returns
>    JSON `[{"text": ...}]`. Include in the prompt the style rules, the 12 examples, and up to 40
>    starters already generated for that topic, with "do not repeat or paraphrase these".
> 4. Filters on each result: 6–15 words; no digits; must not end with "?" AND start with a yes/no
>    pattern ("Kodi mumakonda … ?" alone is OK only with a follow-up); reject phrases listed in
>    config (`banned_phrases`: "boma lingachite", "amatanthauza chiyani", "ubwino ndi kuipa",
>    "chipani", "ndale"); cap "Kodi" openings at 25% per topic AND per domain; dedupe by
>    normalised text and by character 4-gram Jaccard ≥ 0.6 against everything generated so far
>    (all topics). Each rejection records its reason.
> 5. Stopping: a topic stops when its quota is filled, after 5 consecutive calls with no accepted
>    starter, or at `max_calls_per_topic` (config, default 3 × quota / per_call). A topic that stops
>    short keeps its shortfall: report it, never reallocate it silently. Persist accepted and
>    rejected (with reason) to `data/prompts/starters/generated/<run-id>/starters.csv` and
>    `rejected.csv`, resumable by run-id; write `shortfalls.csv` per topic.
> 6. Output columns: `candidate_id` (sha1 of normalised text, 16 chars), `text`, `domain`,
>    `topic`, `word_count`, `model`, `generated_at`, `source_category = ai-generated`.
> 7. `--mock`: a no-cost run with a fake model (canned replies including duplicates, banned
>    phrases, long and short ones) to exercise filters, quotas and stopping end to end.
> 8. `--pilot N` (100–200): spread N starters across all domains and several topics. Print the
>    cost estimate first, then a diagnostic report: generated vs accepted per domain/topic,
>    rejection counts by reason (duplicate, banned phrase, word length, yes/no, Kodi cap), share
>    starting with "Kodi" per domain, and the accepted starters grouped by domain and topic for a
>    Chichewa reviewer.
> 9. Gloria's starters are thematic reference only: never copied into the output.
> 10. Optional later stage, not in the generation loop: a semantic near-duplicate check (e.g.
>    multilingual embeddings) over the final CSV, reporting pairs above a threshold for review.
> 11. Tests: quotas and rounding, last-call size, filters, Kodi cap at both levels, dedupe,
>    stopping rules and shortfalls, resume, mock run. Don't run paid calls; don't read `.env`;
>    don't commit.
>
> Phase 1: implement the above plus tests and a `--mock` run. Phase 2 (I run it): `--pilot 150`,
> review, then the full run.
>
> Then extend `make_labelling_batch` with `--kind starters` reading that CSV (same manifest +
> READY format, 10% overlap; `domain` and `topic` go to `model-labels.csv`, not the manifest).
