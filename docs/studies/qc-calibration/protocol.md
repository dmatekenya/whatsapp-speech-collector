# QC calibration recording protocol

This protocol describes how the staged clips for Stage 1 of the QC calibration study were recorded.
Speakers recorded controlled Chichewa clips under ten audio conditions through the study mode of the
WhatsApp bot. The requested recording condition is metadata, not a quality label: whether a clip is
usable is decided later by raters ({doc}`labelling methodology <labelling-methodology>`).

## Speakers

- All speakers speak Chichewa and recorded on their own phones.
- Speakers were registered participants, most of them pilot contributors who already knew the
  recording process. They were switched into the study through a study enrollment; their main
  recording was paused until they finished.
- Consent covered the possibility that the clips are included in an openly published
  audio-quality dataset.
- Working files and logs use participant IDs only, never names or phone numbers.

The planned and actual samples are described in the
{doc}`recording methodology <recording-methodology>`.

(table-1-recording-conditions)=
## Table 1. Recording conditions

| Code | Condition | Setup given to speakers | Clips per speaker |
|---|---|---|---:|
| C1 | Good | Quiet room; hold the phone as you normally would; natural voice | 3 |
| C2 | Too quiet | Normal phone position; speak softly | 3 |
| C3 | Arm's length | Hold the phone at arm's length (about 60–70 cm); speak normally | 3 |
| C4 | Background voices | Other people talk nearby while you speak | 3 |
| C5 | Background music | Music audible while you speak | 3 |
| C6 | Noisy surroundings | A non-speech noise source nearby (e.g. fan, traffic, running water) | 3 |
| C7 | Echo | Record in a room where your voice echoes | 3 |
| C8 | Muffled microphone | Keep the microphone partly covered (one agreed method; no shouting) | 3 |
| C9 | Silence in a quiet room | Record without speaking in a quiet setting | 3 |
| C10 | Silence with background speech | Stay silent while other people talk nearby | 3 |
| | **Total** | | **30** |

The optional wind condition in the original plan was not recorded.

## Table 2. Recording tasks

| Task | Per speaker | Duration | Conditions |
|---|---|---|---|
| Fixed sentence (S12577) | 1 per condition | Normal reading, about 20–30 s | C1–C8 |
| Fixed conversation starter (C0482) | 1 answer per condition, same question each time | Aim for 15–45 s, at most 60 s | C1–C8 |
| Free speech | 1 per condition, speaker chooses the topic | Aim for 15–45 s, at most 60 s | C1–C8 |
| Silence | 3 per condition | 10–15 s each | C9–C10 |

The sentence and the conversation starter are the same for every speaker and every condition, so
the condition is the only thing that changes between a speaker's clips. Both prompts are inactive in
the main prompt pool, so they are never sent in the main collection.

## How the bot ran the recordings

1. **Choose a condition.** The bot shows a list of the ten conditions with progress ("2/3 done",
   "N of 10 conditions done"). Each speaker has their own randomised order, and the next condition
   in it is marked as suggested, but speakers may choose any condition, because some need a
   particular place or other people present.
2. **Set up.** The bot shows the setup instruction for the chosen condition, with **Ready** and
   **Change condition** buttons.
3. **Record the tasks.** After **Ready**, the bot sends the condition's tasks one at a time:
   sentence, then conversation starter, then free speech (C1–C8), or three silent clips (C9–C10).
   Every prompt starts with the condition in bold (e.g. *C3 · Arm's length (phone 60–70 cm away) —
   Task 1 of 3*), and response tasks show the target duration.
4. **Check each clip.** After each clip the bot offers **Next** and **Re-record**. If a clip is
   outside the suggested duration, the reply says so; the clip is always kept. Every attempt is
   stored.
5. **Breaks.** If a speaker returns after more than 10 minutes, the bot repeats the setup
   instruction for the condition in progress, with **Continue** and **Change condition**.
6. **Finish.** When all ten conditions are done, the speaker returns to the main collection.

The bot's arrival checks are measured on every clip but never reject a study clip, since several
conditions (C2, C9, C10) are meant to fail them.

## What is stored with each clip

Each clip is stored like any other voice note, in the speaker's folder, and linked to the study
enrollment. The study records, per clip: condition code, task, prompt ID, attempt number, the
suggested condition at the time, and the position at which the condition was actually recorded.
Study clips are excluded from production totals, lifetime minutes, prompt coverage and dataset
exports.

Raters never see the condition or task: clips are exported for labelling under random IDs
({doc}`labelling setup <labelling-setup>`).
