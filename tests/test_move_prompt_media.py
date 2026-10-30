import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from scripts.move_prompt_media import move_prompt_media
from src.storage import db
from src.storage.files import LocalFileStorage
from src.storage.models import Prompt


# ============================================================
# Helpers
# ============================================================
def image(seed: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + f"image {seed}".encode() * 10


def add_prompts(old_root: Path, files: dict[str, bytes]) -> None:
    """Prompt rows plus their files under old_root, as the importer left them."""
    with db.get_sync_sessionmaker()() as session:
        for number, (media_path, data) in enumerate(files.items(), start=1):
            path = old_root / media_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            session.add(
                Prompt(
                    prompt_id=f"I{number:04d}",
                    prompt_type="image",
                    media_path=media_path,
                    media_content_type="image/png",
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    source_file=f"test/{media_path}",
                )
            )
        session.commit()


def media_paths() -> list[str]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Prompt.media_path).order_by(Prompt.id)))


FILES = {"food/I0001.png": image(1), "beaches/I0002.png": image(2), "_no_domain/I0003.png": image(3)}


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "data" / "prompt_images", tmp_path / "data" / "prompts" / "images" / "_prepared"


# ============================================================
# Tests
# ============================================================
def test_dry_run_moves_nothing(clean_db: None, roots: tuple[Path, Path]) -> None:
    old_root, new_root = roots
    add_prompts(old_root, FILES)

    result = move_prompt_media(old_root, new_root, dry_run=True)

    assert sorted(result.to_move) == sorted(FILES) and result.problems == []
    assert all((old_root / path).is_file() for path in FILES)
    assert not new_root.exists()


def test_moves_files_keeps_relative_paths_and_removes_old_root(
    clean_db: None, roots: tuple[Path, Path]
) -> None:
    old_root, new_root = roots
    add_prompts(old_root, FILES)
    (old_root / ".DS_Store").write_bytes(b"finder")
    (old_root / "food" / ".DS_Store").write_bytes(b"finder")
    before = media_paths()

    result = move_prompt_media(old_root, new_root)

    assert result.moved == 3 and result.old_root_removed
    assert not old_root.exists()
    assert media_paths() == before  # relative to the root, so unchanged
    store = LocalFileStorage(new_root)
    for path, data in FILES.items():
        assert store.read(path) == data

    # Re-running finds everything already at the new root.
    again = move_prompt_media(old_root, new_root)
    assert sorted(again.already_moved) == sorted(FILES)
    assert again.moved == 0 and again.problems == []


def test_rerun_after_partial_state_is_safe(clean_db: None, roots: tuple[Path, Path]) -> None:
    old_root, new_root = roots
    add_prompts(old_root, FILES)
    # One file was already moved by hand.
    moved = new_root / "food" / "I0001.png"
    moved.parent.mkdir(parents=True)
    (old_root / "food" / "I0001.png").rename(moved)

    result = move_prompt_media(old_root, new_root)

    assert result.already_moved == ["food/I0001.png"] and result.moved == 2
    assert result.old_root_removed


def test_unreferenced_files_are_kept(clean_db: None, roots: tuple[Path, Path]) -> None:
    old_root, new_root = roots
    add_prompts(old_root, FILES)
    (old_root / "food" / "notes.txt").write_text("keep me")

    result = move_prompt_media(old_root, new_root)

    assert result.moved == 3 and not result.old_root_removed
    assert result.leftover == ["food/notes.txt"]
    assert (old_root / "food" / "notes.txt").read_text() == "keep me"


@pytest.mark.parametrize("problem", ["missing", "changed", "both"])
def test_problems_block_the_whole_move(
    clean_db: None, roots: tuple[Path, Path], problem: str
) -> None:
    old_root, new_root = roots
    add_prompts(old_root, FILES)
    if problem == "missing":
        (old_root / "beaches" / "I0002.png").unlink()
    elif problem == "changed":
        (old_root / "beaches" / "I0002.png").write_bytes(b"edited")
    else:
        LocalFileStorage(new_root).save("beaches/I0002.png", b"other")

    result = move_prompt_media(old_root, new_root)

    assert len(result.problems) == 1 and "I0002" in result.problems[0]
    assert result.moved == 0
    assert (old_root / "food" / "I0001.png").is_file()
    assert not (new_root / "food" / "I0001.png").exists()


def test_failed_copy_removes_new_copies_and_keeps_old(
    clean_db: None, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    old_root, new_root = roots
    add_prompts(old_root, FILES)
    real_save = LocalFileStorage.save
    calls = {"n": 0}

    def flaky_save(self: LocalFileStorage, relative_path: str, data: bytes | Path) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_save(self, relative_path, data)

    monkeypatch.setattr(LocalFileStorage, "save", flaky_save)

    with pytest.raises(OSError, match="disk full"):
        move_prompt_media(old_root, new_root)

    assert all((old_root / path).read_bytes() == data for path, data in FILES.items())
    assert [p for p in new_root.rglob("*") if p.is_file()] == []
