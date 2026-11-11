# DigitalOcean gateway contract — Ver. 0.0.0.1

All endpoints are server-to-server HTTPS with token authentication.

Existing read-only bearer token:
- `GET /v1/supervision/snapshot` — contributor IDs, recording metadata, production QC counts and officially accepted-duration aggregate.
- `GET /v1/supervision/revision` — small change detector, including QC status digest.
- `GET /v1/supervision/audio/{id}/status` and `GET /v1/supervision/audio/{id}` — authenticated private media, no public media storage.

**Separate owner-enabled write bearer token (disabled by default):**
- `GET /v1/supervision/write-status` — tests dedicated writer and immutable audit-table permission without changing data.
- `POST /v1/supervision/reviews` — accepts operation UUID, recording ID, `approve|reject`, expected previous QC, reviewer ID/display label, reason up to 150 characters. Requires `SUPERVISION_WRITES_ENABLED=1`, `SUPERVISION_WRITE_DSN`, and `SUPERVISION_REVIEW_TOKEN`. It locks the row, checks current QC and eligibility, updates the official status and records an immutable audit in a transaction. A retry with the same UUID returns the previous result; QC conflicts return HTTP 409.

This write contract is NOT safe to enable until the production owner has verified recording state-machine rules, hooks, triggers, and backups. The collector's existing application is not modified by this release.
