# Platform architecture

The collector uses FastAPI, PostgreSQL, Redis/RQ, local file storage and a Modal GPU transcription service.

```{mermaid}
flowchart LR
  W[WhatsApp: Meta / Twilio] --> A[FastAPI webhooks]
  A --> P[(PostgreSQL)]
  A --> Q[Redis / RQ]
  Q --> J[Workers and conversation engine]
  J --> P
  J --> F[Original audio and 16 kHz WAV]
  J --> W
  F --> B[ASR batch / Modal]
  B --> T[Transcripts and QC results]
  T --> P
```

## Responsibilities

- `src/api/`: signature checks, inbound persistence, status callbacks and prompt media.
- `src/conversation/`: participant state, transactional reply creation and delivery recovery.
- `src/tasks/`: prompt coverage, assignments, retakes, targets and sessions.
- `src/workers/`: participant lookup, audio storage and background processing.
- `src/asr/` and `src/qc/`: batch transcription, versioned metrics and reports.
- `src/storage/` and Alembic migrations: records, integrity constraints and file references.

Inbound messages are stored before queueing; recovery scripts can requeue pending work. Per-participant advisory locks serialize conversation changes. Outbound records retain state snapshots for recovery after delivery failures.

Production uses Docker Compose behind Caddy on a DigitalOcean droplet. Local development uses Homebrew PostgreSQL and Redis with `uv run`. See {doc}`/platform/deployment` and {doc}`/platform/database`.
