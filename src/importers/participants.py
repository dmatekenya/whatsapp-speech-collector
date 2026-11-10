from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.config import PROJECT_ROOT
from src.importers.framework import (
    BatchImporter,
    FieldSpec,
    ImportConfig,
    ImportReport,
    ParsedRow,
    load_config,
    next_id,
    peek_next_id,
)
from src.registry.participants import normalize_phone
from src.storage.models import Participant

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "participants.yaml"

# Fixed fields a re-import may update (participant_id and source_file never change).
UPDATABLE_FIELDS = ("name", "phone_number", "status", "is_test", "batch", "enrolled_on")


class ParticipantImporter(BatchImporter):
    title = "PARTICIPANTS"

    def __init__(
        self, config: ImportConfig, source_file: str, today: date | None = None, batch: str | None = None
    ) -> None:
        super().__init__(config)
        self.source_file = source_file
        # Batch for rows without a batch value; the file name without extension if not given.
        self.batch = batch or Path(source_file).stem
        # Participants this import created (filled by apply; empty on a dry run).
        self.new_ids: list[str] = []
        self.today = today or date.today()
        self.id_options = config.options["id"]
        self.id_column = self.id_options["column"]
        phone = config.options["phone"]
        self.default_region = phone["default_region"]
        self.allowed_country_codes = [str(code) for code in phone["allowed_country_codes"]]
        self.parsers["phone"] = self._parse_phone

    @classmethod
    def from_file(
        cls, csv_path: Path, config_path: Path = DEFAULT_CONFIG, batch: str | None = None
    ) -> "ParticipantImporter":
        return cls(load_config(config_path), source_file=csv_path.name, batch=batch)

    # ==========
    # VALIDATION
    # ==========
    def _parse_phone(self, value: str, spec: FieldSpec) -> str:
        return normalize_phone(value, self.default_region, self.allowed_country_codes)

    def validate_file(self, rows: list[ParsedRow], report: ImportReport) -> None:
        for column in (self.id_column, "phone_number"):
            seen: dict[Any, list[int]] = defaultdict(list)
            for row in rows:
                if column in row.values:
                    seen[row.values[column]].append(row.number)
            for numbers in seen.values():
                for number in numbers[1:]:
                    report.error(number, column, f"same value as row {numbers[0]}")

    # ========
    # DATABASE
    # ========
    def lock(self, session: Session) -> None:
        # Blocks other writers (another import) but not the worker's reads.
        session.execute(text("LOCK TABLE participants IN SHARE ROW EXCLUSIVE MODE"))

    def _defaults(self, field: str) -> Any:
        spec = self.config.fields[field]
        if field == "batch":
            return self.batch
        if field == "enrolled_on":
            return self.today
        return spec.default

    def apply(
        self, session: Session, rows: list[ParsedRow], report: ImportReport, dry_run: bool
    ) -> dict[int, str]:
        given_ids = [row.values[self.id_column] for row in rows if self.id_column in row.values]
        phones = [row.values["phone_number"] for row in rows]
        by_id = {
            p.participant_id: p
            for p in session.scalars(
                select(Participant).where(Participant.participant_id.in_(given_ids))
            )
        }
        by_phone = {
            p.phone_number: p
            for p in session.scalars(select(Participant).where(Participant.phone_number.in_(phones)))
        }

        matched: list[tuple[ParsedRow, Participant]] = []
        new_rows: list[ParsedRow] = []
        for row in rows:
            given_id = row.values.get(self.id_column)
            owner = by_phone.get(row.values["phone_number"])
            if given_id:
                participant = by_id.get(given_id)
                if participant is None:
                    report.error(
                        row.number,
                        self.id_column,
                        f"{given_id} doesn't exist; leave it blank for new participants",
                    )
                    continue
                if owner is not None and owner is not participant:
                    report.error(
                        row.number, "phone_number", f"already registered to {owner.participant_id}"
                    )
                    continue
                matched.append((row, participant))
            elif owner is not None:
                matched.append((row, owner))
            else:
                new_rows.append(row)
        if not report.ok:
            return {}

        ids: dict[int, str] = {}
        for row, participant in matched:
            changed = self._update(participant, row)
            report.counts["updated" if changed else "unchanged"] += 1
            ids[row.number] = participant.participant_id

        report.counts["new"] += len(new_rows)
        if dry_run:
            if new_rows:
                first = peek_next_id(session, self.id_options["sequence"], *self._format())
                report.notes.append(f"would assign {len(new_rows)} new ID(s), starting at {first}")
            return ids

        for row in new_rows:
            participant_id = next_id(session, self.id_options["sequence"], *self._format())
            values = {field: row.values.get(field, self._defaults(field)) for field in UPDATABLE_FIELDS}
            session.add(
                Participant(
                    participant_id=participant_id,
                    attributes=row.attributes_with_extra(),
                    source_file=self.source_file,
                    **values,
                )
            )
            ids[row.number] = participant_id
            self.new_ids.append(participant_id)
        session.flush()
        return ids

    def _format(self) -> tuple[str, int]:
        return self.id_options["prefix"], int(self.id_options["width"])

    def _update(self, participant: Participant, row: ParsedRow) -> bool:
        """Apply non-blank values from the row; return whether anything changed."""
        changed = False
        for field in UPDATABLE_FIELDS:
            if field in row.values and getattr(participant, field) != row.values[field]:
                setattr(participant, field, row.values[field])
                changed = True
        attributes = dict(participant.attributes or {})
        new_attributes = row.attributes_with_extra()
        if "extra" in new_attributes:
            new_attributes["extra"] = {**attributes.get("extra", {}), **new_attributes["extra"]}
        merged = {**attributes, **new_attributes}
        if merged != attributes:
            participant.attributes = merged
            changed = True
        return changed
