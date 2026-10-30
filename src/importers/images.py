"""Image prompts: import a folder of images as prompts (I0001, ...).

Every image file under the batch folder (recursively) is one prompt; files
and folders whose names start with "_" or "." are skipped. The batch is the
folder's name and the domain is the first subfolder below it. Each image is
prepared (src/media/images.py: oriented, metadata stripped, resized, JPEG)
into the prompt media store as <domain>/I0001.jpg; an image that can't be
prepared is rejected, never copied as is. Identical originals (same SHA-256)
are imported once.
"""

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.importers.framework import (
    BatchImporter,
    FieldError,
    FieldSpec,
    ImportConfig,
    ImportReport,
    ParsedRow,
    next_id,
    parse_specs,
    peek_next_id,
)
from src.importers.prompts import normalize_domain
from src.media.images import ImageProcessingError, PreparedImage, prepare_image
from src.storage.files import FileStorage, get_prompt_media_storage
from src.storage.models import Prompt

HEADER = ["file", "domain", "source"]

# ============================================================
# Helpers
# ============================================================
def _skipped(name: str) -> bool:
    return name.startswith(("_", "."))


class SkippedFolderError(ValueError):
    """The folder passed as a batch is one the importer always skips."""


# ============================================================
# Importer
# ============================================================
class ImagePromptImporter(BatchImporter):
    id_column = "prompt_id"
    allows_partial = True

    def __init__(
        self,
        config: ImportConfig,
        type_key: str,
        source: str | None = None,
        storage: FileStorage | None = None,
    ) -> None:
        type_options = config.options["prompt_types"][type_key]
        super().__init__(
            ImportConfig(
                fields=parse_specs(type_options.get("fields")),
                attributes=parse_specs(type_options.get("attributes")),
                options=config.options,
            )
        )
        self.type_options = type_options
        self.prompt_type = type_options["prompt_type"]
        self.title = f"PROMPTS ({type_key})"
        self.source = source
        self.extensions = {f".{ext.lower().lstrip('.')}" for ext in type_options["extensions"]}
        self.max_source_bytes = int(float(type_options["max_source_mb"]) * 1024 * 1024)
        prepared = type_options["prepared"]
        self.jpeg_quality = int(prepared["jpeg_quality"])
        self.max_long_side = int(prepared["max_long_side"])
        self.max_prepared_bytes = int(float(prepared["max_mb"]) * 1024 * 1024)
        # Prepared JPEGs wait here between validation and the database write.
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._prepared: dict[str, tuple[Path, PreparedImage]] = {}
        self.storage = storage or get_prompt_media_storage()
        self.parsers["image_file"] = self._parse_image_file
        self.parsers["domain"] = normalize_domain
        self.folder = Path()
        self.ignored: list[str] = []
        self._saved: list[str] = []

    # ============================================================
    # Reading the folder
    # ============================================================
    def read(self, path: Path) -> tuple[list[str], list[dict[str, str]]]:
        if not path.is_dir():
            return [], []
        # The prepared copies must never be read back in as source images,
        # even if the store is moved somewhere without a "_" prefix.
        prepared = self.storage.absolute_path(".").resolve()
        batch = path.resolve()
        if _skipped(path.name) or batch == prepared or prepared in batch.parents:
            raise SkippedFolderError(
                f"{path} is skipped by the image importer (prepared copies, or a name "
                'starting with "_" or "."); pass the batch folder instead'
            )
        self.folder = path
        records = []
        for file in sorted(path.rglob("*")):
            relative = file.relative_to(path)
            # Any folder or file starting with "_" or "." is skipped, at any depth.
            if any(_skipped(part) for part in relative.parts) or not file.is_file():
                continue
            if prepared in file.resolve().parents:
                continue
            if file.suffix.lower() not in self.extensions:
                self.ignored.append(relative.as_posix())
                continue
            records.append(
                {
                    "file": relative.as_posix(),
                    # First subfolder below the batch folder; none at the top level.
                    "domain": relative.parts[0] if len(relative.parts) > 1 else "",
                    "source": self.source or "",
                }
            )
        return HEADER, records

    def label(self, record: dict[str, str]) -> str | None:
        return record["file"]

    def output_path(self, path: Path, kind: str) -> Path:
        # Next to the batch folder: images/test -> images/test_rejected.csv
        return path.parent / f"{path.name}_{kind}.csv"

    # ============================================================
    # Validation
    # ============================================================
    def prepare(self, data: bytes) -> PreparedImage:
        return prepare_image(
            data,
            max_long_side=self.max_long_side,
            quality=self.jpeg_quality,
            max_bytes=self.max_prepared_bytes,
        )

    def _parse_image_file(self, value: str, spec: FieldSpec) -> str:
        file = self.folder / value
        size = file.stat().st_size
        if size == 0:
            raise FieldError("is empty", ["empty file"])
        if size > self.max_source_bytes:
            limit = self.max_source_bytes / 1024 / 1024
            raise FieldError(
                f"is {size / 1024 / 1024:.1f} MB (max {limit:g} MB)", [f"larger than {limit:g} MB"]
            )
        try:
            prepared = self.prepare(file.read_bytes())
        except ImageProcessingError as exc:
            raise FieldError(str(exc), ["can't be processed as an image"]) from None
        # Keep the result so apply() writes exactly what was validated.
        if self._scratch is None:
            self._scratch = tempfile.TemporaryDirectory(prefix="speech-collector-images-")
        scratch_file = Path(self._scratch.name) / f"{len(self._prepared):06d}.jpg"
        scratch_file.write_bytes(prepared.data)
        self._prepared[value] = (scratch_file, prepared)
        return value

    def validate_file(self, rows: list[ParsedRow], report: ImportReport) -> None:
        for relative in self.ignored:
            report.warn(None, None, f"{relative}: not an image file; ignored")
        if self.ignored:
            report.counts["ignored: not an image file"] = len(self.ignored)

        first_file: dict[str, str] = {}
        for row in rows:
            data = (self.folder / row.values["file"]).read_bytes()
            content_sha256 = hashlib.sha256(data).hexdigest()
            row.values["content_sha256"] = content_sha256
            if content_sha256 in first_file:
                row.skip = True
                report.warn(
                    row.number, None, f"identical to {first_file[content_sha256]}; skipped"
                )
            else:
                first_file[content_sha256] = row.values["file"]

    # ============================================================
    # Database
    # ============================================================
    def lock(self, session: Session) -> None:
        session.execute(text("LOCK TABLE prompts IN SHARE ROW EXCLUSIVE MODE"))

    def apply(
        self, session: Session, rows: list[ParsedRow], report: ImportReport, dry_run: bool
    ) -> dict[int, str]:
        hashes = [row.values["content_sha256"] for row in rows]
        existing = {
            p.content_sha256: p
            for p in session.scalars(
                select(Prompt).where(
                    Prompt.prompt_type == self.prompt_type, Prompt.content_sha256.in_(hashes)
                )
            )
        }

        ids: dict[int, str] = {}
        new_rows: list[ParsedRow] = []
        for row in rows:
            if row.skip:
                report.counts["skipped: duplicate in folder"] += 1
            elif row.values["content_sha256"] in existing:
                report.counts["skipped: already in prompt bank"] += 1
                ids[row.number] = existing[row.values["content_sha256"]].prompt_id
            else:
                new_rows.append(row)

        report.counts["would import" if dry_run else "imported"] += len(new_rows)
        prefix, width, sequence = (
            self.type_options["id_prefix"],
            int(self.type_options["id_width"]),
            self.type_options["sequence"],
        )
        if dry_run:
            if new_rows:
                first = peek_next_id(session, sequence, prefix, width)
                report.notes.append(f"would assign {len(new_rows)} new ID(s), starting at {first}")
            return ids

        batch = self.folder.name
        by_hash: dict[str, str] = {}
        for row in new_rows:
            prompt_id = next_id(session, sequence, prefix, width)
            domain = row.values.get("domain")
            if row.values["file"] not in self._prepared:
                # Never fall back to copying the original.
                raise RuntimeError(f"{row.values['file']} was not prepared")
            scratch_file, prepared = self._prepared[row.values["file"]]
            media_path = f"{domain or '_no_domain'}/{prompt_id}.{prepared.extension}"
            self.storage.save(media_path, scratch_file)
            self._saved.append(media_path)
            session.add(
                Prompt(
                    prompt_id=prompt_id,
                    prompt_type=self.prompt_type,
                    media_path=media_path,
                    media_content_type=prepared.content_type,
                    media_size_bytes=len(prepared.data),
                    media_width=prepared.width,
                    media_height=prepared.height,
                    content_sha256=row.values["content_sha256"],
                    source=row.values.get("source"),
                    domain=domain,
                    attributes=self._attributes(batch, row),
                    source_file=f"{batch}/{row.values['file']}",
                )
            )
            ids[row.number] = by_hash[row.values["content_sha256"]] = prompt_id
        session.flush()

        # Duplicates within the folder get the ID of the file they duplicate.
        for row in rows:
            if row.skip and row.values["content_sha256"] in by_hash:
                ids[row.number] = by_hash[row.values["content_sha256"]]
        self._cleanup_scratch()
        return ids

    def _cleanup_scratch(self) -> None:
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None
        self._prepared.clear()

    @staticmethod
    def _attributes(batch: str, row: ParsedRow) -> dict[str, Any]:
        return {"batch": batch, "original_name": Path(row.values["file"]).name}

    def discard(self) -> None:
        # The transaction was rolled back (or this was a dry run): remove the
        # copies made for it.
        for media_path in self._saved:
            self.storage.delete(media_path)
        self._saved.clear()
        self._cleanup_scratch()
