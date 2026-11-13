# Prompt-generation methodology

Recordings are classified at two levels:

- **Speech type** (`speech_type`): *scripted*, read aloud from a given text, or *spontaneous*, in
  the participant's own words. This is the main split for ASR use (read vs. spontaneous speech).
- **Task type** (`task_type`): how the speech was elicited. Each task type belongs to exactly one
  speech type.

| Speech type | Task type | Shown to participants as | What the participant does | Prompted | Each prompt recorded by |
|---|---|---|---|---|---|
| Scripted | Read sentences (`sentence`) | Reading sentences | Reads a sentence aloud | Yes | 5 speakers |
| Spontaneous | Describe pictures (`image`) | Describing pictures | Describes an image in their own words | Yes | 7 speakers (pilot) |
| Spontaneous | Conversation starters (`conversation_starter`) | Answering questions | Answers an open question freely | Yes | 3 speakers |
| Spontaneous | Free speech (`free_speech`) | Talking about anything | Talks about anything they like ("speak from the heart") | No | — |

- Task types are defined in `config/assignment_settings.yaml` (`task_types`), each marked as
  prompted or not and given its speech type. The speech type is not stored per recording: it is
  derived from the task type, so changing a task type's speech type reclassifies all of its
  recordings. Reports (e.g. the task-bank simulation) show it next to the task type and sum
  minutes per speech type. With the current split the target is 30% scripted, 70% spontaneous.
- **Prompt IDs** are assigned at import, one sequence per type: `S00001` sentences, `I0001` images,
  `C0001` conversation starters. Free speech has no prompts. Imports must run in the same order on
  every environment so IDs match (and match the browser app).
- **Sentence length is controlled at selection, not fixed in transcription.** The aim is that a
  read sentence fits within the clip limit, so the prompt text can serve as the transcript and
  read speech needs as little transcription as possible.
  - Import checks: target 8–25 words (hard limits 3–50); planned character-length check as well,
    since Chichewa words are long and characters predict reading time better than words. Out-of-
    target sentences are flagged, not rejected, and are assigned last.
  - After the pilot, limits are calibrated from real reading times (median duration and seconds
    per character per sentence); sentences whose readings are usually flagged `long_sentence`
    (median over 30 s) or below 5 s are retired. This is a later, report-driven step.
  - Sensitive questionnaire topics are excluded.
- **Images:** organised into theme folders (theme = folder name); prepared copies are JPEG,
  metadata stripped, resized for WhatsApp. No text in images, no recognisable real people.
- **Conversation starters:** open questions that prompt free speech (not read aloud); in Chichewa
  for the pilot onwards.
- **Free speech (unprompted):** the bot invites the participant to talk about anything (their day,
  their village, a story); captures genuinely spontaneous speech. Needs transcription.
- **Coverage (prompted tasks):** a prompt stops being assigned once it has enough speakers
  (accepted + recorded + unexpired sent); least-covered prompts go first; nobody gets the same
  prompt twice.
