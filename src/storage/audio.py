import re
from functools import lru_cache

from src.config import get_settings
from src.storage.files import FileStorage, LocalFileStorage

UNASSIGNED_FOLDER = "_unassigned"
UNASSIGNED_PREFIX = "UNASSIGNED"

# Participant IDs end up in folder and file names: allow only short safe
# tokens, and reject anything that looks like a phone number.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PHONE_LIKE = re.compile(r"\d{7,}")

_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


# ============================================================
# Naming
# ============================================================
def _check_participant_id(participant_id: str) -> str:
    if not _SAFE_ID.match(participant_id) or _PHONE_LIKE.search(participant_id):
        raise ValueError("participant_id is not a safe folder/file name")
    return participant_id


def resolve_folder(sender: str, participant_id: str | None) -> str:
    """Folder for a participant's recordings. Never derived from the sender's number."""
    if participant_id:
        return _check_participant_id(participant_id)
    return UNASSIGNED_FOLDER


def recording_filename(participant_id: str | None, recording_id: int, ext: str) -> str:
    """The one place the file name format is defined, e.g. P0001_R000123.wav."""
    prefix = _check_participant_id(participant_id) if participant_id else UNASSIGNED_PREFIX
    return f"{prefix}_R{recording_id:06d}.{ext.lstrip('.')}"


def extension_for(content_type: str | None) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    return _EXTENSIONS.get(base, "bin")


# ============================================================
# Storage
# ============================================================
# The generic implementation lives in src/storage/files.py.
AudioStorage = FileStorage
LocalAudioStorage = LocalFileStorage


@lru_cache
def get_audio_storage() -> AudioStorage:
    return LocalAudioStorage(get_settings().audio_root)
