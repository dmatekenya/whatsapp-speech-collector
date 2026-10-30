"""One-off: move prepared prompt media from an old root to Settings.prompt_media_root.

    uv run python -m scripts.move_prompt_media --dry-run
    uv run python -m scripts.move_prompt_media
    uv run python -m scripts.move_prompt_media --from data/prompt_images

Paths in the database are relative to the media root, so they stay the same
unless a file is found somewhere else (they're updated then). All prompt rows
with media are locked for the whole move and the new copies are checked
against content_sha256 before committing; old copies are deleted only after
the commit. If anything fails, the new copies are removed and nothing changes.
Safe to re-run: files already at the new root are skipped.
"""

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from src.config import PROJECT_ROOT, get_settings
from src.logging_config import setup_logging
from src.storage.db import get_sync_sessionmaker
from src.storage.files import LocalFileStorage
from src.storage.models import Prompt

logger = logging.getLogger("move_prompt_media")

OLD_ROOT = PROJECT_ROOT / "data" / "prompt_images"
# Created by macOS Finder; safe to delete so the old folder can be removed.
JUNK_FILES = {".DS_Store"}


@dataclass
class MoveResult:
    to_move: list[str] = field(default_factory=list)
    already_moved: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    leftover: list[str] = field(default_factory=list)  # unreferenced files left behind
    moved: int = 0
    old_root_removed: bool = False


# ============================================================
# Move
# ============================================================
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remove_empty_dirs(root: Path, result: MoveResult) -> None:
    """Delete junk files, then empty folders bottom-up, then root itself if empty."""
    for path in root.rglob("*"):
        if path.is_file() and path.name in JUNK_FILES:
            path.unlink()
    folders = [path for path in root.rglob("*") if path.is_dir()]
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    result.leftover = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    if not any(root.iterdir()):
        root.rmdir()
        result.old_root_removed = True


def move_prompt_media(old_root: Path, new_root: Path, dry_run: bool = False) -> MoveResult:
    result = MoveResult()
    if old_root.resolve() == new_root.resolve():
        result.problems.append("old and new roots are the same folder")
        return result
    old, new = LocalFileStorage(old_root), LocalFileStorage(new_root)

    with get_sync_sessionmaker()() as session:
        prompts = session.scalars(
            select(Prompt).where(Prompt.media_path.is_not(None)).order_by(Prompt.id).with_for_update()
        ).all()

        for prompt in prompts:
            path = prompt.media_path
            in_old, in_new = old.exists(path), new.exists(path)
            if in_new and not in_old:
                result.already_moved.append(path)
            elif in_old and in_new:
                result.problems.append(f"{prompt.prompt_id}: {path} exists in both roots")
            elif not in_old:
                result.problems.append(f"{prompt.prompt_id}: {path} not found in either root")
            elif prompt.content_sha256 and _sha256(old.absolute_path(path)) != prompt.content_sha256:
                result.problems.append(f"{prompt.prompt_id}: {path} doesn't match its stored hash")
            else:
                result.to_move.append(path)

        if result.problems or dry_run:
            session.rollback()
            return result

        copied: list[str] = []
        try:
            for path in result.to_move:
                new.save(path, old.absolute_path(path))
                copied.append(path)
                if _sha256(new.absolute_path(path)) != _sha256(old.absolute_path(path)):
                    raise RuntimeError(f"copy of {path} doesn't match the original")
            # Paths are relative to the media root, so they carry over as they
            # are. (A path that needed changing would be updated here.)
            session.commit()
        except BaseException:
            session.rollback()
            for path in copied:
                new.delete(path)
            raise

    for path in result.to_move:
        old.delete(path)
    result.moved = len(result.to_move)
    if old_root.exists():
        _remove_empty_dirs(old_root, result)
    return result


# ============================================================
# CLI
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="old_root", type=Path, default=OLD_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="check and list, move nothing")
    args = parser.parse_args()

    setup_logging()
    old_root = args.old_root if args.old_root.is_absolute() else PROJECT_ROOT / args.old_root
    new_root = get_settings().prompt_media_root
    if not old_root.is_dir():
        logger.info("%s doesn't exist; nothing to move", old_root)
        return

    result = move_prompt_media(old_root, new_root, dry_run=args.dry_run)
    prefix = "[dry run] " if args.dry_run else ""
    logger.info("%sfrom %s", prefix, old_root)
    logger.info("%sto   %s", prefix, new_root)
    logger.info(
        "%s%s file(s) to move, %s already at the new root",
        prefix,
        len(result.to_move),
        len(result.already_moved),
    )
    for problem in result.problems:
        logger.error("%s", problem)
    if result.problems:
        logger.error("Nothing moved; fix the problems above and re-run")
        sys.exit(1)
    if not args.dry_run:
        logger.info("Moved %s file(s); database paths unchanged (relative to the root)", result.moved)
        if result.old_root_removed:
            logger.info("Removed %s", old_root)
        elif result.leftover:
            logger.warning(
                "Kept %s: %s file(s) not referenced by any prompt: %s",
                old_root,
                len(result.leftover),
                ", ".join(result.leftover[:20]),
            )


if __name__ == "__main__":
    main()
