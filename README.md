# WhatsApp Speech Collector

Tiyankhuleni collects Chichewa speech through WhatsApp voice notes. It supports sentence readings, image descriptions, conversation starters and free speech, with participant targets, audio checks, batch transcription and human-review records.

The application uses Python 3.13+, FastAPI, PostgreSQL, Redis/RQ and FFmpeg. Meta Cloud API is the primary WhatsApp channel; Twilio is supported as a fallback. Modal runs batch ASR.

## Documentation

- [Documentation home](docs/index.md)
- [Overall methodology](docs/methodology.md)
- [Prompt generation](docs/prompt-generation/methodology.md)
- [Speech recordings](docs/speech-recordings/methodology.md)
- [Transcription](docs/transcription/methodology.md)
- [Architecture](docs/platform/architecture.md) and [deployment](docs/platform/deployment.md)
- [Build and publish the book](docs/publishing.md)

The book is configured for https://dmatekenya.github.io/whatsapp-speech-collector/ . GitHub Actions checks documentation on pull requests and publishes successful builds from `main` once Pages is enabled.

Participant data, recordings and environment files are excluded from Git and the documentation build. See [CLAUDE.md](CLAUDE.md) for local development conventions.
