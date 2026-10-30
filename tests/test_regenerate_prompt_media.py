import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from sqlalchemy import select

from scripts.regenerate_prompt_media import regenerate_prompt_media
from src.media.images import jpeg_metadata_segments
from src.storage import db
from src.storage.files import LocalFileStorage
from src.storage.models import Prompt


# ============================================================
# Helpers
# ============================================================
def png(seed: int, size: tuple[int, int] = (2000, 1000)) -> bytes:
    info = PngInfo()
    info.add_text("Author", f"someone {seed}")
    buffer = io.BytesIO()
    Image.new("RGBA", size, (seed * 40 % 256, 100, 150, 255)).save(buffer, "PNG", pnginfo=info)
    return buffer.getvalue()


def add_copied_prompts(store: LocalFileStorage, originals: Path, count: int = 3) -> dict[str, bytes]:
    """Image prompts as the old importer left them: plain .png copies of the originals."""
    files = {}
    with db.get_sync_sessionmaker()() as session:
        for number in range(1, count + 1):
            data = png(number)
            prompt_id = f"I{number:04d}"
            media_path = f"food/{prompt_id}.png"
            source_file = f"test/food/photo {number}.png"
            store.save(media_path, data)
            original = originals / source_file
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(data)
            files[prompt_id] = data
            session.add(
                Prompt(
                    prompt_id=prompt_id,
                    prompt_type="image",
                    media_path=media_path,
                    media_content_type="image/png",
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    source_file=source_file,
                )
            )
        session.commit()
    return files


def rows() -> list[Prompt]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Prompt).order_by(Prompt.id)))


def stored(store: LocalFileStorage) -> list[str]:
    return sorted(p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file())


@pytest.fixture
def store(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "_prepared")


# ============================================================
# Tests
# ============================================================
def test_dry_run_changes_nothing(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    add_copied_prompts(store, tmp_path / "originals")
    before = stored(store)

    result = regenerate_prompt_media(store, tmp_path / "originals", dry_run=True)

    assert [p.new_path for p in result.planned] == [f"food/I000{n}.jpg" for n in (1, 2, 3)]
    assert stored(store) == before
    assert all(row.media_path.endswith(".png") for row in rows())


def test_regenerates_jpegs_updates_rows_and_removes_pngs(
    clean_db: None, tmp_path: Path, store: LocalFileStorage
) -> None:
    files = add_copied_prompts(store, tmp_path / "originals")

    result = regenerate_prompt_media(store, tmp_path / "originals")

    assert result.regenerated == 3 and result.problems == []
    assert stored(store) == ["food/I0001.jpg", "food/I0002.jpg", "food/I0003.jpg"]
    for row in rows():
        data = store.read(row.media_path)
        assert row.media_content_type == "image/jpeg"
        assert (row.media_width, row.media_height) == (1600, 800)
        assert row.media_size_bytes == len(data)
        assert jpeg_metadata_segments(data) == [] and b"someone" not in data
        # The hash still identifies the original, so re-imports are deduplicated.
        assert row.content_sha256 == hashlib.sha256(files[row.prompt_id]).hexdigest()

    again = regenerate_prompt_media(store, tmp_path / "originals")
    assert again.planned == [] and len(again.already_prepared) == 3


def test_falls_back_to_originals_folder(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    add_copied_prompts(store, tmp_path / "originals")
    store.delete("food/I0002.png")

    result = regenerate_prompt_media(store, tmp_path / "originals")

    taken = {plan.prompt_id: plan.taken_from for plan in result.planned}
    assert taken == {"I0001": "stored copy", "I0002": "original", "I0003": "stored copy"}
    assert stored(store) == ["food/I0001.jpg", "food/I0002.jpg", "food/I0003.jpg"]


def test_missing_original_stops_everything(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    add_copied_prompts(store, tmp_path / "originals")
    store.delete("food/I0002.png")
    (tmp_path / "originals" / "test" / "food" / "photo 2.png").write_bytes(b"edited")
    before = stored(store)

    result = regenerate_prompt_media(store, tmp_path / "originals")

    assert len(result.problems) == 1 and result.problems[0].startswith("I0002")
    assert result.regenerated == 0 and stored(store) == before


def test_failed_write_rolls_back(
    clean_db: None, tmp_path: Path, store: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_copied_prompts(store, tmp_path / "originals")
    before = stored(store)
    real_save = LocalFileStorage.save
    calls = {"n": 0}

    def flaky_save(self: LocalFileStorage, relative_path: str, data: bytes | Path) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_save(self, relative_path, data)

    monkeypatch.setattr(LocalFileStorage, "save", flaky_save)

    with pytest.raises(OSError):
        regenerate_prompt_media(store, tmp_path / "originals")

    assert stored(store) == before
    assert all(row.media_path.endswith(".png") for row in rows())
