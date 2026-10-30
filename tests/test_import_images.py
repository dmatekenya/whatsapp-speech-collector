import csv
import io
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from src.config import PROJECT_ROOT
from src.importers.framework import ImportReport, load_config, run_import
from src.importers.images import ImagePromptImporter, SkippedFolderError
from src.media.images import jpeg_metadata_segments
from src.importers.prompts import DEFAULT_CONFIG
from src.storage import db
from src.storage.files import LocalFileStorage
from src.storage.models import Prompt


# ============================================================
# Helpers
# ============================================================
def _image(seed: int, fmt: str, size: tuple[int, int] = (64, 48)) -> bytes:
    """A small real image; the seed picks the colour, so different seeds differ."""
    mode = "RGBA" if fmt == "PNG" else "RGB"
    colour = (seed * 37 % 256, seed * 91 % 256, seed * 53 % 256) + ((200,) if mode == "RGBA" else ())
    buffer = io.BytesIO()
    Image.new(mode, size, colour).save(buffer, fmt)
    return buffer.getvalue()


def png(seed: int) -> bytes:
    return _image(seed, "PNG")


def jpeg(seed: int) -> bytes:
    return _image(seed, "JPEG")


def webp(seed: int) -> bytes:
    return _image(seed, "WEBP")


def make_folder(root: Path, files: dict[str, bytes]) -> Path:
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def store(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "prepared")


def run(
    folder: Path, store: LocalFileStorage, source: str | None = "ai-generated", **kwargs: bool
) -> ImportReport:
    importer = ImagePromptImporter(load_config(DEFAULT_CONFIG), "images", source, storage=store)
    return run_import(importer, folder, db.get_sync_sessionmaker(), **kwargs)


def prompts() -> list[Prompt]:
    with db.get_sync_sessionmaker()() as session:
        return list(
            session.scalars(select(Prompt).where(Prompt.prompt_type == "image").order_by(Prompt.id))
        )


def stored_files(store: LocalFileStorage) -> list[str]:
    root = store.root
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ============================================================
# Reading the folder
# ============================================================
def test_folder_import_batch_domain_source_and_store(
    clean_db: None, tmp_path: Path, store: LocalFileStorage
) -> None:
    folder = make_folder(
        tmp_path / "batch_2026",
        {
            "top.png": png(1),  # no domain
            "Food Market/a.png": png(2),
            "Food Market/sub/b.jpg": jpeg(3),  # still domain "food_market"
            "beaches/c.webp": webp(4),
            "_drafts/skip.png": png(5),  # folder starting with _
            "beaches/_skip.png": png(6),
            "beaches/.hidden.png": png(7),
            ".DS_Store": b"junk",
            "notes.txt": b"not an image",
        },
    )

    report = run(folder, store)

    assert report.ok, report.render()
    assert report.counts["imported"] == 4
    assert report.counts["ignored: not an image file"] == 1
    assert [w.message for w in report.warnings] == ["notes.txt: not an image file; ignored"]
    assert report.value_counts["domain"] == {"food_market": 2, "beaches": 1, "(blank)": 1}

    by_file = {p.attributes["original_name"]: p for p in prompts()}
    assert sorted(p.prompt_id for p in by_file.values()) == ["I0001", "I0002", "I0003", "I0004"]
    a = by_file["a.png"]
    assert a.domain == "food_market"
    assert a.source == "ai-generated"
    assert a.text is None and a.normalized_text is None
    assert a.attributes == {"batch": "batch_2026", "original_name": "a.png"}
    assert a.source_file == "batch_2026/Food Market/a.png"
    # Every prepared copy is a JPEG, whatever the original format.
    assert a.media_content_type == "image/jpeg"
    assert a.media_path == f"food_market/{a.prompt_id}.jpg"
    assert (a.media_width, a.media_height) == (64, 48)
    assert by_file["b.jpg"].domain == "food_market"
    assert by_file["c.webp"].media_path == f"beaches/{by_file['c.webp'].prompt_id}.jpg"
    top = by_file["top.png"]
    assert top.domain is None and top.media_path == f"_no_domain/{top.prompt_id}.jpg"
    assert stored_files(store) == sorted(p.media_path for p in by_file.values())
    for prompt in by_file.values():
        data = store.read(prompt.media_path)
        assert data != png(2) and data.startswith(b"\xff\xd8")  # prepared, not copied
        assert len(data) == prompt.media_size_bytes
        assert jpeg_metadata_segments(data) == []


def test_underscore_and_dot_skipped_at_any_depth(
    clean_db: None, tmp_path: Path, store: LocalFileStorage
) -> None:
    folder = make_folder(
        tmp_path / "images",
        {
            "food/a.png": png(1),
            "food/deep/er/still/b.png": png(2),
            # A copy of the prepared store inside the source tree: never imported.
            "_prepared/food/I0001.png": png(1),
            "_prepared/beaches/I0002.png": png(3),
            "food/_old/c.png": png(4),
            "food/deep/.cache/d.png": png(5),
            "food/deep/er/_e.png": png(6),
            ".git/f.png": png(7),
        },
    )

    report = run(folder, store)

    assert report.counts["imported"] == 2
    assert sorted(p.source_file for p in prompts()) == [
        "images/food/a.png",
        "images/food/deep/er/still/b.png",
    ]
    assert report.warnings == []


def test_prepared_store_skipped_even_without_underscore(
    clean_db: None, tmp_path: Path
) -> None:
    # If the store were moved somewhere without a "_" prefix inside the
    # source tree, its copies still must not be imported.
    folder = make_folder(tmp_path / "images", {"food/a.png": png(1)})
    store = LocalFileStorage(folder / "prepared")

    run(folder, store)
    assert store.exists("food/I0001.jpg")

    again = run(folder, store)
    assert again.rows_read == 1 and again.counts["skipped: already in prompt bank"] == 1
    assert len(prompts()) == 1


@pytest.mark.parametrize("name", ["_prepared", ".hidden", "_drafts"])
def test_skipped_folder_refused_as_batch(
    clean_db: None, tmp_path: Path, store: LocalFileStorage, name: str
) -> None:
    folder = make_folder(tmp_path / name, {"food/a.png": png(1)})

    with pytest.raises(SkippedFolderError):
        run(folder, store)
    assert prompts() == []


def test_store_root_refused_as_batch(clean_db: None, tmp_path: Path) -> None:
    store = LocalFileStorage(tmp_path / "copies")
    make_folder(tmp_path / "copies", {"food/I0001.png": png(1)})

    with pytest.raises(SkippedFolderError):
        run(tmp_path / "copies", store)
    with pytest.raises(SkippedFolderError):
        run(tmp_path / "copies" / "food", store)


def test_source_is_optional(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    run(make_folder(tmp_path / "b", {"x/a.png": png(1)}), store, source=None)
    assert prompts()[0].source is None


# ============================================================
# Duplicates and re-import
# ============================================================
def test_identical_files_imported_once(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    folder = make_folder(
        tmp_path / "batch",
        {"food/a.png": png(1), "food/copy of a.png": png(1), "beaches/b.png": png(2)},
    )

    report = run(folder, store, write_ids=True)

    assert report.counts["imported"] == 2
    assert report.counts["skipped: duplicate in folder"] == 1
    assert [str(w) for w in report.warnings] == ["food/copy of a.png: identical to food/a.png; skipped"]
    with (tmp_path / "batch_imported.csv").open(newline="") as handle:
        manifest = {row["file"]: row["prompt_id"] for row in csv.DictReader(handle)}
    assert manifest["food/copy of a.png"] == manifest["food/a.png"]

    again = run(folder, store)
    assert again.counts["imported"] == 0
    assert again.counts["skipped: already in prompt bank"] == 2

    # The same image renamed in another batch is still a duplicate.
    other = make_folder(tmp_path / "batch_two", {"misc/renamed.png": png(2), "misc/new.png": png(3)})
    third = run(other, store)
    assert third.counts["skipped: already in prompt bank"] == 1 and third.counts["imported"] == 1
    assert len(prompts()) == 3 and len(stored_files(store)) == 3


# ============================================================
# Validation, rejected files and rollback
# ============================================================
def test_unprocessable_images_rejected_never_copied(
    clean_db: None, tmp_path: Path, store: LocalFileStorage
) -> None:
    truncated = jpeg(5)[:200]
    folder = make_folder(
        tmp_path / "batch",
        {
            "food/good.png": png(1),
            "food/fake.png": b"this is text, not an image",
            "food/empty.jpg": b"",
            "food/truncated.jpg": truncated,
        },
    )

    report = run(folder, store)

    assert report.counts["imported"] == 1 and report.counts["rejected"] == 3
    assert report.rejection_summary() == {
        "file: can't be processed as an image": 2,
        "file: empty file": 1,
    }
    rendered = report.render()
    assert "food/fake.png, file: can't be decoded as an image" in rendered
    with (tmp_path / "batch_rejected.csv").open(newline="") as handle:
        rejected = {row["file"]: row["rejection_reason"] for row in csv.DictReader(handle)}
    assert rejected == {
        "food/empty.jpg": "file: empty file",
        "food/fake.png": "file: can't be processed as an image",
        "food/truncated.jpg": "file: can't be processed as an image",
    }
    # Only the good image was written, as a prepared JPEG.
    assert stored_files(store) == [prompts()[0].media_path]
    assert prompts()[0].media_path.endswith(".jpg")


def test_large_original_is_resized_under_the_limit(
    clean_db: None, tmp_path: Path, store: LocalFileStorage
) -> None:
    # Noise doesn't compress: a >5 MB PNG, which must still import.
    noise = Image.effect_noise((2400, 1800), 120).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, "PNG")
    assert len(buffer.getvalue()) > 5 * 1024 * 1024
    folder = make_folder(tmp_path / "batch", {"food/big.png": buffer.getvalue()})

    report = run(folder, store)

    assert report.counts["imported"] == 1, report.render()
    (prompt,) = prompts()
    assert (prompt.media_width, prompt.media_height) == (1600, 1200)
    assert prompt.media_size_bytes < 5 * 1024 * 1024


def test_dry_run_writes_nothing(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    folder = make_folder(tmp_path / "batch", {"food/a.png": png(1), "food/bad.png": b"text"})

    report = run(folder, store, dry_run=True, write_ids=True)

    assert report.counts["would import"] == 1 and report.counts["rejected"] == 1
    assert "starting at I0001" in report.render()
    assert prompts() == [] and stored_files(store) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["batch"]


def test_strict_mode_imports_nothing(clean_db: None, tmp_path: Path, store: LocalFileStorage) -> None:
    folder = make_folder(tmp_path / "batch", {"food/a.png": png(1), "food/bad.png": b"text"})

    report = run(folder, store, strict=True)

    assert not report.committed and prompts() == [] and stored_files(store) == []


def test_copied_files_removed_when_transaction_fails(
    clean_db: None, tmp_path: Path, store: LocalFileStorage
) -> None:
    # A prompt already holds I0002, so the second insert violates the unique ID.
    with db.get_sync_sessionmaker()() as session:
        session.add(
            Prompt(prompt_id="I0002", prompt_type="image", source_file="x", content_sha256="x")
        )
        session.commit()
    folder = make_folder(tmp_path / "batch", {"food/a.png": png(1), "food/b.png": png(2)})

    report = run(folder, store)

    assert not report.committed
    assert "database rejected the import" in report.render()
    assert [p.prompt_id for p in prompts()] == ["I0002"]
    assert stored_files(store) == []


# ============================================================
# CLI
# ============================================================
def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.import_prompts", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_images_dry_run(clean_db: None, tmp_path: Path) -> None:
    folder = make_folder(tmp_path / "batch", {"food/a.png": png(1)})

    result = _cli("images", str(folder), "--source", "ai-generated", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "PROMPTS (images) DRY RUN: batch" in result.stdout
    assert "would assign 1 new ID(s), starting at I0001" in result.stdout


def test_cli_rejects_wrong_inputs(clean_db: None, tmp_path: Path) -> None:
    csv_file = tmp_path / "s.csv"
    csv_file.write_text("text,source,domain\n")

    source_on_csv = _cli("sentences", str(csv_file), "--source", "x", "--dry-run")
    assert source_on_csv.returncode == 2 and "--source only applies to image prompts" in source_on_csv.stderr

    file_for_images = _cli("images", str(csv_file), "--dry-run")
    assert file_for_images.returncode == 2 and "imports a folder" in file_for_images.stderr

    prepared = make_folder(tmp_path / "_prepared", {"food/I0001.png": png(1)})
    prepared_as_batch = _cli("images", str(prepared), "--dry-run")
    assert prepared_as_batch.returncode == 2 and "is skipped by the image importer" in prepared_as_batch.stderr
