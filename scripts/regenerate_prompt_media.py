"""Regenerate prepared copies for existing image prompts (I0001.png -> I0001.jpg).

    uv run python -m scripts.regenerate_prompt_media --dry-run
    uv run python -m scripts.regenerate_prompt_media

For each image prompt, the original is taken from the stored copy if it's
still the unprocessed original (its SHA-256 matches content_sha256), else from
<originals root>/<source_file>. It's prepared as the importer does now
(oriented, metadata stripped, RGB JPEG, long side <= 1600 px, < 5 MB), and the
row's path, content type, size and dimensions are updated. All rows are
locked and updated in one transaction; old copies are deleted only after the
commit. Any problem stops everything before a file is written. Prompts that
are already prepared are skipped, so it's safe to re-run.
"""

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from src.config import PROJECT_ROOT
from src.importers.framework import load_config
from src.importers.images import ImagePromptImporter
from src.importers.prompts import DEFAULT_CONFIG
from src.logging_config import setup_logging
from src.media.images import ImageProcessingError, PreparedImage, jpeg_metadata_segments
from src.storage.db import get_sync_sessionmaker
from src.storage.files import FileStorage, get_prompt_media_storage
from src.storage.models import Prompt

logger = logging.getLogger("regenerate_prompt_media")

ORIGINALS_ROOT = PROJECT_ROOT / "data" / "prompts" / "images"


@dataclass
class Planned:
    prompt_id: str
    old_path: str
    new_path: str
    taken_from: str  # "stored copy" or "original"
    original_bytes: bytes
    prepared: PreparedImage


@dataclass
class RegenerateResult:
    planned: list[Planned] = field(default_factory=list)
    already_prepared: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    regenerated: int = 0


# ============================================================
# Regeneration
# ============================================================
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_prepared(prompt: Prompt, storage: FileStorage) -> bool:
    path = prompt.media_path or ""
    if not path.endswith(".jpg") or prompt.media_width is None or not storage.exists(path):
        return False
    data = storage.read(path)
    try:
        return _sha256(data) != prompt.content_sha256 and jpeg_metadata_segments(data) == []
    except ValueError:
        return False


def _original(prompt: Prompt, storage: FileStorage, originals_root: Path) -> tuple[str, bytes] | None:
    """The unprocessed original, verified against content_sha256."""
    if prompt.media_path and storage.exists(prompt.media_path):
        data = storage.read(prompt.media_path)
        if _sha256(data) == prompt.content_sha256:
            return "stored copy", data
    candidate = originals_root / prompt.source_file
    if candidate.is_file():
        data = candidate.read_bytes()
        if _sha256(data) == prompt.content_sha256:
            return "original", data
    return None


def regenerate_prompt_media(
    storage: FileStorage, originals_root: Path, dry_run: bool = False
) -> RegenerateResult:
    result = RegenerateResult()
    importer = ImagePromptImporter(load_config(DEFAULT_CONFIG), "images", storage=storage)

    with get_sync_sessionmaker()() as session:
        prompts = session.scalars(
            select(Prompt)
            .where(Prompt.prompt_type == "image", Prompt.media_path.is_not(None))
            .order_by(Prompt.id)
            .with_for_update()
        ).all()

        for prompt in prompts:
            if _is_prepared(prompt, storage):
                result.already_prepared.append(prompt.prompt_id)
                continue
            found = _original(prompt, storage, originals_root)
            if found is None:
                result.problems.append(
                    f"{prompt.prompt_id}: original not found (stored copy and "
                    f"{prompt.source_file} missing or changed)"
                )
                continue
            taken_from, data = found
            try:
                prepared = importer.prepare(data)
            except ImageProcessingError as exc:
                result.problems.append(f"{prompt.prompt_id}: {exc}")
                continue
            folder = PurePosixPath(prompt.media_path).parent
            new_path = str(folder / f"{prompt.prompt_id}.{prepared.extension}")
            result.planned.append(
                Planned(prompt.prompt_id, prompt.media_path, new_path, taken_from, data, prepared)
            )

        if result.problems or dry_run:
            session.rollback()
            return result

        by_id = {prompt.prompt_id: prompt for prompt in prompts}
        written: list[Planned] = []
        try:
            for plan in result.planned:
                storage.save(plan.new_path, plan.prepared.data)
                written.append(plan)
                prompt = by_id[plan.prompt_id]
                prompt.media_path = plan.new_path
                prompt.media_content_type = plan.prepared.content_type
                prompt.media_size_bytes = len(plan.prepared.data)
                prompt.media_width = plan.prepared.width
                prompt.media_height = plan.prepared.height
            session.commit()
        except BaseException:
            session.rollback()
            for plan in written:
                if plan.new_path == plan.old_path:
                    storage.save(plan.old_path, plan.original_bytes)  # put the original back
                else:
                    storage.delete(plan.new_path)
            raise

    for plan in result.planned:
        if plan.old_path != plan.new_path:
            storage.delete(plan.old_path)
    result.regenerated = len(result.planned)
    return result


# ============================================================
# CLI
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--originals",
        type=Path,
        default=ORIGINALS_ROOT,
        help="folder holding the batch folders (source_file is relative to it)",
    )
    parser.add_argument("--dry-run", action="store_true", help="check and list, change nothing")
    args = parser.parse_args()

    setup_logging()
    result = regenerate_prompt_media(get_prompt_media_storage(), args.originals, dry_run=args.dry_run)
    prefix = "[dry run] " if args.dry_run else ""
    for plan in result.planned:
        logger.info(
            "%s%s: %s -> %s (%s x %s, %.0f KB, from %s)",
            prefix,
            plan.prompt_id,
            plan.old_path,
            plan.new_path,
            plan.prepared.width,
            plan.prepared.height,
            len(plan.prepared.data) / 1024,
            plan.taken_from,
        )
    for problem in result.problems:
        logger.error("%s", problem)
    logger.info(
        "%s%s to regenerate, %s already prepared, %s problem(s)",
        prefix,
        len(result.planned),
        len(result.already_prepared),
        len(result.problems),
    )
    if result.problems:
        logger.error("Nothing changed; fix the problems above and re-run")
        sys.exit(1)
    if not args.dry_run:
        logger.info("Regenerated %s prepared copies; old copies removed", result.regenerated)


if __name__ == "__main__":
    main()
