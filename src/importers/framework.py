"""Shared CSV batch import: YAML-configured validation, one transaction per file.

An importer subclasses BatchImporter, adds any custom field types to
``parsers``, and implements ``apply`` to match rows against the database and
write them. ``run_import`` does the rest:

1. read the CSV (whitespace stripped from every value and header),
2. check the header (missing required columns are errors, unknown columns are
   flagged and kept in ``attributes["extra"]``),
3. parse every row against the config,
4. call ``apply`` inside one transaction, and commit only if this isn't a dry
   run and nothing blocks the import,
5. optionally write ``<name>_imported.csv`` with the assigned IDs, and write
   rows with errors to ``<name>_rejected.csv``.

Row errors block the whole file unless the importer sets ``allows_partial``
(and the run isn't ``strict``); then only the valid rows are imported, still
in one transaction. Header and database errors always block the import.
"""

import csv
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

MAX_ISSUES_SHOWN = 50

# Added to <name>_rejected.csv; ignored (not flagged) when such a file is
# fixed and imported again.
REJECTION_COLUMN = "rejection_reason"

# The CSV row number people see in a spreadsheet: the header is row 1.
FIRST_DATA_ROW = 2


# ============================================================
# Config
# ============================================================
@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str = "text"
    required: bool = False
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None
    case_insensitive: bool = False
    # Never echo the value in the report (names, phone numbers).
    pii: bool = False
    # Show a count per value in the report, so typos stand out.
    report_counts: bool = False


@dataclass(frozen=True)
class ImportConfig:
    fields: dict[str, FieldSpec]  # real table columns
    attributes: dict[str, FieldSpec]  # stored in the JSONB "attributes" column
    options: dict[str, Any]  # importer-specific settings

    @property
    def columns(self) -> dict[str, FieldSpec]:
        return {**self.fields, **self.attributes}


def parse_specs(section: dict[str, Any] | None) -> dict[str, FieldSpec]:
    specs = {}
    for name, raw in (section or {}).items():
        raw = dict(raw or {})
        if "choices" in raw:
            raw["choices"] = tuple(str(choice) for choice in raw["choices"])
        specs[name] = FieldSpec(name=name, **raw)
    return specs


def load_config(path: Path) -> ImportConfig:
    data = yaml.safe_load(path.read_text()) or {}
    fields = parse_specs(data.pop("fields", None))
    attributes = parse_specs(data.pop("attributes", None))
    overlap = fields.keys() & attributes.keys()
    if overlap:
        raise ValueError(f"{path.name}: columns defined as both field and attribute: {overlap}")
    return ImportConfig(fields=fields, attributes=attributes, options=data)


# ============================================================
# Field parsers
# ============================================================
# A parser turns a stripped, non-empty CSV value into a typed value, or raises
# ValueError with a message that doesn't repeat the value (the report adds it
# unless the field is PII).
Parser = Callable[[str, FieldSpec], Any]


class FieldError(ValueError):
    """A ValueError carrying one short reason per broken rule, for the report summary."""

    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = reasons or [message]

_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def parse_text(value: str, spec: FieldSpec) -> str:
    return normalize_whitespace(value)


def parse_int(value: str, spec: FieldSpec) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ValueError("must be a whole number") from None
    if spec.min is not None and number < spec.min:
        raise ValueError(f"must be at least {spec.min:g}")
    if spec.max is not None and number > spec.max:
        raise ValueError(f"must be at most {spec.max:g}")
    return number


def parse_bool(value: str, spec: FieldSpec) -> bool:
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError("must be true/false, yes/no or 1/0")


def parse_date(value: str, spec: FieldSpec) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a date as YYYY-MM-DD") from None


def parse_choice(value: str, spec: FieldSpec) -> str:
    choices = spec.choices or ()
    for choice in choices:
        if value == choice or (spec.case_insensitive and value.lower() == choice.lower()):
            return choice
    raise ValueError(f"must be one of {', '.join(choices)}")


BASE_PARSERS: dict[str, Parser] = {
    "text": parse_text,
    "int": parse_int,
    "bool": parse_bool,
    "date": parse_date,
    "choice": parse_choice,
}


# ============================================================
# Report
# ============================================================
@dataclass
class Issue:
    row: int | None
    column: str | None
    message: str
    label: str | None = None  # shown instead of "row N", e.g. an image's path

    def __str__(self) -> str:
        where = []
        if self.label is not None:
            where.append(self.label)
        elif self.row is not None:
            where.append(f"row {self.row}")
        if self.column:
            where.append(self.column)
        return f"{', '.join(where)}: {self.message}" if where else self.message


@dataclass
class ImportReport:
    file_name: str
    title: str
    dry_run: bool
    partial: bool = False  # valid rows import even if others are rejected
    rows_read: int = 0
    errors: list[Issue] = field(default_factory=list)  # every error, for the list
    fatal: list[Issue] = field(default_factory=list)  # file, header and database errors
    rejected: dict[int, list[str]] = field(default_factory=dict)  # row -> reasons
    warnings: list[Issue] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    value_counts: dict[str, Counter[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    committed: bool = False
    labels: dict[int, str] = field(default_factory=dict)  # row -> label for issues

    @property
    def ok(self) -> bool:
        """Whether the import can go ahead (or went ahead)."""
        return not self.fatal and (self.partial or not self.rejected)

    def error(
        self,
        row: int | None,
        column: str | None,
        message: str,
        reasons: list[str] | None = None,
    ) -> None:
        issue = Issue(row, column, message, self.labels.get(row) if row is not None else None)
        self.errors.append(issue)
        if row is None or row < FIRST_DATA_ROW:
            self.fatal.append(issue)
            return
        prefix = f"{column}: " if column else ""
        self.rejected.setdefault(row, []).extend(f"{prefix}{r}" for r in reasons or [message])

    def rejection_summary(self) -> Counter[str]:
        return Counter(reason for reasons in self.rejected.values() for reason in reasons)

    def warn(self, row: int | None, column: str | None, message: str) -> None:
        label = self.labels.get(row) if row is not None else None
        self.warnings.append(Issue(row, column, message, label))

    def render(self) -> str:
        mode = "DRY RUN" if self.dry_run else "IMPORT"
        lines = [f"{self.title} {mode}: {self.file_name}", f"  rows read: {self.rows_read}"]
        for name, count in self.counts.items():
            lines.append(f"  {name}: {count}")
        for column, counter in self.value_counts.items():
            lines.append(f"  per {column}:")
            width = max((len(value) for value in counter), default=0)
            for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
                lines.append(f"    {value:<{width}}  {count}")
        summary = self.rejection_summary()
        if summary:
            lines.append("  rejection reasons:")
            width = len(str(max(summary.values())))
            for reason, count in summary.most_common():
                lines.append(f"    {count:>{width}}  {reason}")
        for label, issues in (("ERRORS", self.errors), ("WARNINGS", self.warnings)):
            if not issues:
                continue
            lines.append(f"  {label} ({len(issues)}):")
            lines.extend(f"    {issue}" for issue in issues[:MAX_ISSUES_SHOWN])
            if len(issues) > MAX_ISSUES_SHOWN:
                lines.append(f"    ... and {len(issues) - MAX_ISSUES_SHOWN} more")
        lines.extend(f"  {note}" for note in self.notes)
        rejected = len(self.rejected)
        if self.fatal:
            lines.append("  RESULT: nothing imported (fix the errors and re-run)")
        elif rejected and not self.partial:
            lines.append(
                f"  RESULT: nothing imported; {rejected} row(s) have errors"
                " (all-or-nothing: fix them and re-run)"
            )
        elif self.dry_run:
            outcome = f"; {rejected} row(s) would be rejected" if rejected else ""
            lines.append(f"  RESULT: valid rows would be imported{outcome}; nothing written (dry run)")
        elif self.committed:
            outcome = f"; {rejected} row(s) rejected" if rejected else ""
            lines.append(f"  RESULT: imported{outcome}")
        return "\n".join(lines)


# ============================================================
# Rows and importers
# ============================================================
@dataclass
class ParsedRow:
    number: int  # spreadsheet row number (header is row 1)
    raw: dict[str, str]  # every column, stripped, as in the file
    values: dict[str, Any] = field(default_factory=dict)  # non-blank fixed fields
    attributes: dict[str, Any] = field(default_factory=dict)  # non-blank attributes
    extra: dict[str, str] = field(default_factory=dict)  # non-blank unknown columns
    skip: bool = False  # set by validate_file, e.g. a duplicate within the file

    def attributes_with_extra(self) -> dict[str, Any]:
        merged = dict(self.attributes)
        if self.extra:
            merged["extra"] = dict(self.extra)
        return merged


class BatchImporter(ABC):
    title: str
    id_column: str
    # Import valid rows and reject the rest (unless run with strict=True).
    allows_partial: bool = False

    def __init__(self, config: ImportConfig) -> None:
        self.config = config
        self.parsers: dict[str, Parser] = dict(BASE_PARSERS)

    def read(self, path: Path) -> tuple[list[str], list[dict[str, str]]]:
        """Header and records to import. Default: a CSV file."""
        return read_csv(path)

    def label(self, record: dict[str, str]) -> str | None:
        """How the report refers to a record; None means "row N"."""
        return None

    def output_path(self, path: Path, kind: str) -> Path:
        """Where to write <name>_imported.csv / <name>_rejected.csv."""
        return path.with_name(f"{path.stem}_{kind}{path.suffix}")

    def discard(self) -> None:
        """Undo side effects outside the database (e.g. copied files) after a rollback."""

    def validate_file(self, rows: list[ParsedRow], report: ImportReport) -> None:
        """Checks across the rows that passed field validation. No database access."""

    def lock(self, session: Session) -> None:
        """Take any table locks needed so concurrent imports can't interleave."""

    @abstractmethod
    def apply(
        self, session: Session, rows: list[ParsedRow], report: ImportReport, dry_run: bool
    ) -> dict[int, str]:
        """Match rows against the database and write them; return row number -> ID.

        Report conflicts with report.error(); the caller rolls back if there
        are any. In a dry run, don't consume sequence values.
        """


# ============================================================
# Helpers for importers
# ============================================================
def next_id(session: Session, sequence: str, prefix: str, width: int) -> str:
    number = session.scalar(text("SELECT nextval(:sequence)"), {"sequence": sequence})
    return f"{prefix}{number:0{width}d}"


def peek_next_id(session: Session, sequence: str, prefix: str, width: int) -> str:
    """The ID the next nextval() would give, without consuming it."""
    if not sequence.replace("_", "").isalnum():
        raise ValueError(f"Invalid sequence name {sequence!r}")
    last_value, is_called = session.execute(
        text(f"SELECT last_value, is_called FROM {sequence}")  # noqa: S608
    ).one()
    number = last_value + 1 if is_called else last_value
    return f"{prefix}{number:0{width}d}"


# ============================================================
# Runner
# ============================================================
def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    # utf-8-sig drops the BOM Excel adds; newline="" handles CRLF files.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = [column.strip().lower() for column in next(reader)]
        except StopIteration:
            return [], []
        records = []
        for values in reader:
            values = [value.strip() for value in values]
            values += [""] * (len(header) - len(values))
            records.append(dict(zip(header, values, strict=False)))
        return header, records


def _check_header(header: list[str], config: ImportConfig, report: ImportReport) -> list[str]:
    duplicates = sorted(column for column, count in Counter(header).items() if count > 1)
    for column in duplicates:
        report.error(1, column, "column appears more than once in the header")
    for spec in config.columns.values():
        if spec.required and spec.name not in header:
            report.error(1, spec.name, "required column is missing")
    unknown = [
        column
        for column in header
        if column and column not in config.columns and column != REJECTION_COLUMN
    ]
    for column in unknown:
        report.warn(1, column, 'unknown column; values kept in attributes["extra"]')
    return unknown


def _parse_row(
    number: int,
    record: dict[str, str],
    importer: BatchImporter,
    unknown: list[str],
    report: ImportReport,
) -> ParsedRow:
    row = ParsedRow(number=number, raw=record)
    for spec in importer.config.columns.values():
        value = record.get(spec.name, "")
        if not value:
            if spec.required:
                report.error(number, spec.name, "required value is missing")
            continue
        parser = importer.parsers.get(spec.type)
        if parser is None:
            raise ValueError(f"No parser for field type {spec.type!r} ({spec.name})")
        try:
            parsed = parser(value, spec)
        except ValueError as exc:
            message = str(exc) if spec.pii else f"{exc} (got {value!r})"
            reasons = exc.reasons if isinstance(exc, FieldError) else [str(exc)]
            report.error(number, spec.name, message, reasons=reasons)
            continue
        target = row.values if spec.name in importer.config.fields else row.attributes
        target[spec.name] = parsed
    row.extra = {column: record[column] for column in unknown if record.get(column)}
    return row


def write_ids_copy(
    path: Path,
    output: Path,
    header: list[str],
    rows: list[ParsedRow],
    id_column: str,
    ids: dict[int, str],
) -> Path:
    """Write a copy of the input (e.g. <name>_imported.csv) with IDs filled in."""
    if output.resolve() == path.resolve():
        raise ValueError("Refusing to overwrite the original file")
    columns = [id_column] + [column for column in header if column != id_column]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row.raw, id_column: ids.get(row.number, row.raw.get(id_column, ""))})
    return output


def write_rejected_copy(
    path: Path,
    output: Path,
    header: list[str],
    rows: list[ParsedRow],
    rejected: dict[int, list[str]],
) -> Path:
    """Write the rejected rows (e.g. to <name>_rejected.csv) plus the reasons."""
    if output.resolve() == path.resolve():
        raise ValueError("Refusing to overwrite the original file")
    columns = [column for column in header if column != REJECTION_COLUMN] + [REJECTION_COLUMN]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if row.number in rejected:
                writer.writerow({**row.raw, REJECTION_COLUMN: "; ".join(rejected[row.number])})
    return output


def run_import(
    importer: BatchImporter,
    path: Path,
    session_factory: sessionmaker[Session],
    *,
    dry_run: bool = False,
    write_ids: bool = False,
    strict: bool = False,
) -> ImportReport:
    report = ImportReport(
        file_name=path.name,
        title=importer.title,
        dry_run=dry_run,
        partial=importer.allows_partial and not strict,
    )
    header, records = importer.read(path)
    if not header:
        report.error(None, None, "nothing to import (empty file or folder)")
        return report
    unknown = _check_header(header, importer.config, report)

    rows = []
    for index, record in enumerate(records):
        if not any(record.values()):
            continue  # blank line
        number = index + FIRST_DATA_ROW
        label = importer.label(record)
        if label is not None:
            report.labels[number] = label
        rows.append(_parse_row(number, record, importer, unknown, report))
    report.rows_read = len(rows)

    for spec in importer.config.columns.values():
        if spec.report_counts:
            blank = "(blank)" if spec.default is None else f"(blank: default {spec.default})"
            report.value_counts[spec.name] = Counter(
                str((row.values | row.attributes).get(spec.name, blank)) for row in rows
            )

    importer.validate_file([row for row in rows if row.number not in report.rejected], report)
    if report.partial:
        report.counts["rejected"] = len(report.rejected)
    # Only for importers that allow partial imports (prompts): a rejected copy
    # of a participant file would duplicate names and phone numbers.
    if importer.allows_partial and report.rejected and not dry_run and not report.fatal:
        output = write_rejected_copy(
            path, importer.output_path(path, "rejected"), header, rows, report.rejected
        )
        report.notes.append(f"rejected rows written to {output}")
    if not report.ok:
        importer.discard()
        return report

    valid_rows = [row for row in rows if row.number not in report.rejected]
    errors_before = len(report.errors)
    with session_factory() as session:
        try:
            importer.lock(session)
            ids = importer.apply(session, valid_rows, report, dry_run)
            if len(report.errors) > errors_before:
                # Conflicts found against the database always block the import.
                session.rollback()
                importer.discard()
                report.fatal.append(Issue(None, None, "conflicts with existing data"))
                return report
            if dry_run:
                session.rollback()
                importer.discard()
                return report
            session.commit()
            report.committed = True
        except IntegrityError as exc:
            session.rollback()
            importer.discard()
            # Constraint details can contain phone numbers: report only the name.
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            report.error(None, None, f"database rejected the import (constraint {constraint})")
            return report
        except BaseException:
            session.rollback()
            importer.discard()
            raise

    if write_ids:
        output = write_ids_copy(
            path, importer.output_path(path, "imported"), header, rows, importer.id_column, ids
        )
        report.notes.append(f"IDs written to {output}")
    return report
