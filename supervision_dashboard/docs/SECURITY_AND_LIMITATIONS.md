# Online supervision security and limitations

- Every recording, audio stream, contributor page and reviewer submission is checked against the authenticated user's contributor assignments. Administrators have full catalog access.
- Production audio is proxied on demand over verified HTTPS; no source media is included in this package or stored in public files.
- The DigitalOcean collector remains the official source of recording QC status. Supervisor decisions are only official once DigitalOcean's audited write gateway returns an idempotent confirmation.
- New write-back uses separate owner-approved PostgreSQL write credentials and a dedicated bearer token. Existing read-only sync remains independent.
- Review `approve` => QC `accepted`; `reject` => `rejected`. Owner must verify the collector's QC rules before enabling.
- Sync checks occur every 5 seconds (configurable in System Settings) only while a signed-in browser page is visible. Background cron runs at Hostinger's supported schedule.
- Original recordings, WhatsApp collector, private keys and media files are never included in this release.
- WhatsApp feedback sending remains disabled until authorized messaging, consent and provider callbacks are separately integrated.
- All deployment steps require testing with owner-approved records, backup and rollback.
