# Prompt targets

Source: ZaZo AI Ltd (2026), *Chichewa Speech Project: Prompt Generation and Review Manual*, supplied as `prompt-generation-guide-v2.pdf`. Page numbers below refer to the printed PDF pages. These documents adapt the guide; they do not establish that the described annotation platform is already implemented.

## Speech-hour allocation

The guide targets 500 hours: 30% scripted and 70% spontaneous (guide §4, Table 3, p. 6). Percentages describe audio duration, not proportions of unique prompts.

| Category | Share of spontaneous hours | Share of all hours | Hours |
| --- | --- | --- | --- |
| Sentences | — | 30% | 150 |
| Pictures | 30% | 21% | 105 |
| Conversation starters | 50% | 35% | 175 |
| Free speech, including previously recorded audio | 20% | 14% | 70 |
| Total | — | 100% | 500 |

## Duration and reuse assumptions

Reuse counts accepted recordings from distinct speakers. Retakes, rejections and assignment alone do not count (guide §4.1, Table 4, pp. 6–7).

| Category | Minimum seconds | Maximum seconds | Planning average seconds | Guide assignment range (speakers) | Planning reuse |
| --- | --- | --- | --- | --- | --- |
| Sentences | 5 | 45 | 20 | 3–5 | 3 |
| Pictures | 5 | 60 | 30 | 2–3 | 2 |
| Conversation starters | 5 | 60 | 30 | 3–5 | 4 |

Planning reuse is an expected average for estimating bank size, not a requirement that every prompt receive exactly that many responses. Actual durations and assignment settings may change during collection.

## Unique prompt quantities

```text
Accepted recordings needed = target hours × 3,600 / average accepted recording seconds
Unique prompts needed = ceil(target hours × 3,600 / (average accepted recording seconds × planning reuse))
```

| Category | Hours | Accepted recordings needed | Unique prompts needed |
| --- | --- | --- | --- |
| Sentences | 150 | 27,000 | 9,000 |
| Pictures | 105 | 12,600 | 6,300 |
| Conversation starters | 175 | 21,000 | 5,250 |
| Prompted total | 430 | 60,600 | 20,550 |

These are the guide's estimates (§4.2, Table 5, pp. 7–8), with no additional rejection buffer specified. They represent usable unique prompts; rejected candidates do not fill the bank. The remaining 70 hours of free speech do not need a fixed prompt bank. Re-estimate quantities using observed accepted durations and reuse as collection progresses.

## Indicative domain allocation

These shares are qualitative project planning judgments about everyday relevance, not measured conversational frequencies (guide §4.3, Table 6, pp. 8–9). Counts reproduce the guide, including its whole-number rounding for conversation starters.

| Primary domain | Share | Sentences | Pictures | Conversation starters |
| --- | --- | --- | --- | --- |
| Family, relationships and community life | 15% | 1,350 | 945 | 788 |
| Food, cooking and nutrition | 12% | 1,080 | 756 | 630 |
| Health, hygiene and wellbeing | 12% | 1,080 | 756 | 630 |
| Agriculture, livestock and fishing | 11% | 990 | 693 | 578 |
| Work, trade and personal finance | 10% | 900 | 630 | 525 |
| Education and learning | 10% | 900 | 630 | 525 |
| Transport and travel | 7% | 630 | 441 | 367 |
| Sport, recreation and entertainment | 7% | 630 | 441 | 367 |
| Nature, weather and environment | 6% | 540 | 378 | 315 |
| Technology and communication | 4% | 360 | 252 | 210 |
| Culture, traditions and celebrations | 4% | 360 | 252 | 210 |
| Public services and civic life | 2% | 180 | 126 | 105 |
| Total | 100% | 9,000 | 6,300 | 5,250 |

Spread each domain allocation across several topics. Count each prompt once under its primary domain. Topic-level quantities may be assigned by batch; the guide supplies no annotator-specific quotas or delivery dates.

## Current pilot configuration

`config/assignment_settings.yaml` currently sets speaker ceilings to 5/7/3 for sentences/pictures/conversation starters and total-hour shares to 30%/25%/25%/20%, including free speech. Pending assignments reserve capacity. These values differ from the guide's production assumptions above and have not been changed by this documentation update. Reconcile them before production collection and run the task-bank simulation before collection runs.
