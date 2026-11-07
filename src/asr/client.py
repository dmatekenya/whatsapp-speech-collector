"""The call to the deployed Modal app, behind a small interface so tests can use a fake."""

from collections.abc import Iterable, Iterator
from typing import Any, Protocol

Chunk = list[tuple[int, bytes]]  # (recording_id, WAV bytes)


class Transcriber(Protocol):
    def transcribe_chunks(self, model_key: str, chunks: Iterable[Chunk]) -> Iterator[dict[str, Any] | BaseException]:
        """One result per chunk, in the order given; a failed call yields its exception.

        A result is {"model": {"key", "repo", "revision"}, "gpu": str,
        "clips": [{"recording_id", "text", "avg_logprob", "audio_seconds", "error"}]}.
        """
        ...


class ModalTranscriber:
    """scripts/modal_asr_app.py, deployed: looked up by name, so no GPU code runs here.

    The modal client reads MODAL_TOKEN_ID / MODAL_TOKEN_SECRET from the
    environment (on the droplet, from .env through docker compose), or
    ~/.modal.toml on a laptop set up with `modal setup`.
    """

    def __init__(self, app_name: str, class_name: str) -> None:
        self.app_name = app_name
        self.class_name = class_name

    def transcribe_chunks(self, model_key: str, chunks: Iterable[Chunk]) -> Iterator[dict[str, Any] | BaseException]:
        import modal  # only needed when actually calling Modal

        transcriber = modal.Cls.from_name(self.app_name, self.class_name)(model_key=model_key)
        yield from transcriber.transcribe.map(chunks, return_exceptions=True)
