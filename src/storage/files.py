"""File storage behind one interface: local disk now, DigitalOcean Spaces later.

Nothing else should touch these folders directly.
"""

import os
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol

from src.config import get_settings


# ============================================================
# Storage interface
# ============================================================
class FileStorage(Protocol):
    def save(self, relative_path: str, data: bytes | Path) -> int:
        """Store bytes or a source file at relative_path; return the size in bytes."""
        ...

    def exists(self, relative_path: str) -> bool: ...

    def read(self, relative_path: str) -> bytes: ...

    def delete(self, relative_path: str) -> None:
        """Remove relative_path if it exists."""
        ...

    def move(self, source: str, target: str) -> None:
        """Move a file, creating folders as needed. Refuses to overwrite."""
        ...

    def absolute_path(self, relative_path: str) -> Path: ...


# ============================================================
# Local disk
# ============================================================
class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def absolute_path(self, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid relative path: {relative_path!r}")
        return self.root.joinpath(*relative.parts)

    def exists(self, relative_path: str) -> bool:
        return self.absolute_path(relative_path).is_file()

    def read(self, relative_path: str) -> bytes:
        return self.absolute_path(relative_path).read_bytes()

    def save(self, relative_path: str, data: bytes | Path) -> int:
        target = self.absolute_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then rename: readers never
        # see a half-written file, and a crash leaves only a hidden .tmp file.
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                if isinstance(data, Path):
                    with data.open("rb") as source:
                        shutil.copyfileobj(source, out)
                else:
                    out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target.stat().st_size

    def delete(self, relative_path: str) -> None:
        self.absolute_path(relative_path).unlink(missing_ok=True)

    def move(self, source: str, target: str) -> None:
        source_path = self.absolute_path(source)
        target_path = self.absolute_path(target)
        if target_path.exists():
            raise FileExistsError(f"{target} already exists")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Same filesystem under one root, so the rename is atomic.
        os.rename(source_path, target_path)


@lru_cache
def get_prompt_media_storage() -> FileStorage:
    return LocalFileStorage(get_settings().prompt_media_root)
