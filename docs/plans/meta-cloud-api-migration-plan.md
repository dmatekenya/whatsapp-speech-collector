# Moving from Twilio to the Meta WhatsApp Cloud API

Last updated: 2026-11-03

Goal: run the speech collector on the Meta WhatsApp Cloud API directly, prove it on Meta's test
number, then move the production number(s) once they are unbanned, and retire Twilio for this
project.

## Why the move is mostly a new adapter

The app was built with a channel layer (`src/channels/base.py`: `InboundMessage`,
`ChannelAdapter`; `src/channels/twilio.py`: `TwilioChannel`). The conversation engine, task bank,
audio storage, QC and database do not need to know which provider carried a message. The work is:

1. a `MetaChannel` that implements the same adapter methods;
2. making a few Twilio-named columns provider-neutral;
3. Meta account setup (tokens, number registration, webhook subscription).

## Decisions

- **2026-11-03 — Replies go out on the channel the message arrived on.** Every reply, prompt and
  retake request uses the `channel` of the inbound message that triggered it, and the channel is
  recorded on each `outbound_messages` row. WhatsApp's 24-hour window belongs to the business
  number the participant wrote to, so replying from the other provider's number would fail or
  confuse participants. This lets Twilio and Meta run side by side, with no cutover switch.
  `WHATSAPP_PROVIDER` is only the default for messages with no triggering inbound message
  (e.g. `scripts/send_test_image.py`, future proactive messages). If a participant writes to both
  numbers, replies follow their latest message.
- **Plan location in the repo:** `docs/meta-migration-plan.md`.

## What changes between Twilio and Meta

| Area | Twilio (now) | Meta Cloud API |
|---|---|---|
| Inbound | One form POST per message, `MessageSid` | JSON POST that can batch several messages and statuses; `wamid` IDs |
| Signature | `X-Twilio-Signature` with `PUBLIC_BASE_URL` | `X-Hub-Signature-256`, HMAC of raw body with app secret (done) |
| Sender | `whatsapp:+265…` | `wa_id` without `+` — normalise to E.164 so the participant lookup is unchanged |
| Swipe-reply | `OriginalRepliedMessageSid` | `context.id` (the `wamid` of the replied-to message) |
| Forwarded | Not exposed | `context.forwarded` / `context.frequently_forwarded` |
| Voice note vs uploaded file | Not exposed | `audio.voice: true` for recorded voice notes |
| Media download | Media URL with Basic auth | `GET /{media-id}` → short-lived URL → `GET` with bearer token |
| Media deletion | Must delete from Twilio (currently broken) | Received media is held by Meta for a limited period; no deletion job needed — confirm in Meta docs |
| Sending | Twilio REST API | `POST /{phone-number-id}/messages` with bearer token |
| Images | Media URL | Link (our signed URLs) or upload once and reuse the media ID |
| Delivery statuses | Separate status callback URL | `statuses` array in the same webhook |
| Buttons | Limited | Reply buttons (up to 3) — e.g. a "Ready" button instead of typing |
| 24-hour window | Same rule | Same rule: free-form replies only within 24 h of the participant's last message, from the number they wrote to |

Two of these directly help data quality: `audio.voice` and the forwarded flags answer the open
to-do on detecting forwarded or uploaded audio.

## Phases

### Phase 0 — Receive-only webhook (done in code, deploy pending)

- `/webhooks/meta`: GET verification, POST with signature check, metadata-only logging, not
  connected to the chatbot. `META_VERIFY_TOKEN`, `META_APP_SECRET`.
- uvicorn access-log filter redacts `hub.verify_token` from logged URLs.
- Deploy to the droplet; callback URL `https://bot.zazoai.com/webhooks/meta`; subscribe to the
  `messages` field; confirm events arrive from the test number.

### Phase 1 — MetaChannel adapter, replies on the arrival channel

- `src/channels/meta.py` implementing the same methods as `TwilioChannel`: parse inbound (one
  `InboundMessage` per message in the batch), download media, send text, send media, parse
  statuses.
- Webhook POST: after the signature check, store each message in `inbound_messages`
  (`channel = 'meta'`, `message_sid = wamid`) and enqueue to the existing `inbound` queue, exactly
  like the Twilio webhook. Statuses update `outbound_messages`.
- Sending: the worker picks the adapter from the triggering inbound message's `channel` (see
  Decisions). `WHATSAPP_PROVIDER=twilio|meta` is only the default when there is no triggering
  message.
- New settings: `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `META_WABA_ID`, `META_GRAPH_VERSION`
  (optional, so a Twilio-only deployment still starts).
- Migration (one Alembic revision, update `docs/architecture/database.md`):
  - `outbound_messages.twilio_sid` → `provider_message_id`, add `channel`
  - `recordings.twilio_media_deleted` → `provider_media_deleted`
  - `recordings`: add `is_voice_note` (bool, nullable) and `forwarded` (text: `no`, `forwarded`,
    `frequently_forwarded`, null for Twilio)
- Swipe-reply binding looks up `provider_message_id`, so it works for both providers.
- Tests with recorded Meta payloads (text, audio voice, audio file, image, reply with context,
  forwarded, statuses incl. `failed` with error codes, batched events), plus routing tests: a
  Meta-triggered reply goes via Meta, a Twilio-triggered one via Twilio.

### Phase 2 — Parity test on the Meta test number

The test number can message only a handful of verified recipient numbers, which is enough for
you plus a few colleagues. Repeat the live test from 25 October:

- [ ] Greeting → progress → instructions → ready → sentence
- [ ] Too-short voice note → retake
- [ ] Swipe-reply to an older sentence binds correctly
- [ ] Images in session, caption variants
- [ ] Conversation starters; switching task type when a target is reached
- [ ] Unregistered number handled, nothing stored
- [ ] Delivery statuses reach `read`
- [ ] New: forwarded voice note and uploaded audio file are flagged
- [ ] Audio saved as `.ogg` + 16 kHz `.wav` in the participant's folder
- [ ] Replies arrive from the number the participant wrote to

Optional while here: a "Ready" reply button.

### Phase 3 — Production setup in Meta

- Business verification for the Meta Business account (if not already done).
- A System User with a **permanent** access token (the dashboard's temporary token expires within
  a day), with `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
- Number: once unbanned, either migrate it from Twilio's WhatsApp Business Account to your own
  (Meta supports moving a number between accounts; two-step verification must be disabled on the
  old side first) or register a fresh number. Set the two-step verification PIN and register the
  number for Cloud API.
- Display name approval and business profile (name, photo, description).
- Subscribe the app to the WhatsApp Business Account (`POST /{waba-id}/subscribed_apps`). Without
  this, the production number sends no webhooks even though the test number did.
- Payment method on the WhatsApp Business Account (needed for any template messages).
- Webhook stays `https://bot.zazoai.com/webhooks/meta`.

### Phase 4 — Cutover

Because replies follow the arrival channel, there is no single switch-over moment:

- Put the production Meta values in the droplet `.env` and redeploy; set
  `WHATSAPP_PROVIDER=meta` as the default.
- Tell participants the number to use (if the number changes). Anyone still writing to the Twilio
  number keeps getting replies there until it is retired.
- Open assignments carry over; a swipe-reply to a Twilio-era prompt sent via Meta will not bind
  (different message IDs) and falls back to the open assignment. If that causes confusion, run
  `scripts/expire_assignments.py` for participants who moved.
- Participants are identified by phone number, so IDs, progress and history carry over unchanged.
- Watch logs and delivery statuses for the first sessions; when Twilio traffic stops, remove the
  webhook URL from the Twilio sender.

### Phase 5 — Retire Twilio

- After 2–4 stable weeks: remove `TwilioChannel`, the Twilio webhook and status routes, and the
  Twilio settings; drop the media-deletion job and `PUBLIC_BASE_URL`-based signature logic.
- Close the Twilio WhatsApp sender to stop charges.

## Keeping the numbers healthy

Bans usually come from quality signals: users blocking or reporting the number, messages to people
who never opted in, or traffic that looks automated and unsolicited. For this project:

- Only reply to participants who wrote first; never message unregistered numbers beyond the single
  "not registered" reply.
- Brief participants before they first message the bot, so the messages are expected.
- Display name and profile should clearly say who is collecting data and why.
- Check quality rating and limits in WhatsApp Manager weekly during the pilot.
- When appealing, explain the research purpose, the opt-in onboarding and the user-initiated flow.

## Open questions

- Why were the numbers banned? The cause decides whether the number is worth migrating or a fresh
  number is safer.
- Which Meta Business account will own production (yours, a project/institution account)?
- Current Meta pricing for service (user-initiated) messages — check before budgeting.