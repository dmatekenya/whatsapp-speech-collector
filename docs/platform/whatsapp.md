# WhatsApp integration

- **Why WhatsApp:** participants already use it daily (little training), and in Malawi people buy
  cheap WhatsApp-only data bundles, so recording costs them little. Everything stays inside
  WhatsApp (no links).
- **User-initiated, plus one welcome.** The bot only replies to participants' messages, so all
  conversation messages fall in WhatsApp's free 24-hour service window. The one exception is the
  registration welcome (section 2): participants haven't written yet, so it is an approved utility
  template, which WhatsApp allows outside the window and charges for (under about US$0.01 per
  message). The 24-hour check still applies to every other message.
- **Provider.** Piloted with Twilio; moving to the Meta WhatsApp Cloud API direct (lower cost,
  voice-note and forwarded flags, no media clean-up). During the move both run side by side and
  replies always go out on the number the participant wrote to. From the pilot (starting
  2026-11-08), participants use the Meta number; the Twilio number remains only as a fallback until
  it is retired.
- **Comparison.** A browser-based recording app is being developed in parallel by a colleague;
  both channels will use the same prompt bank, IDs and QC so they can be compared.

## Implementation

Meta Cloud API is the primary channel; Twilio is the fallback. Provider adapters in `src/channels/` normalize inbound messages and delivery callbacks. Replies use the incoming channel.

Webhooks validate signatures and persist messages before enqueueing processing. Provider message IDs deduplicate retries. Templates can reach new participants outside the reply window; normal replies follow the configured window.
