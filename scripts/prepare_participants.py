"""Turn the consent-form export into a participant CSV ready for import.

Reads the Excel (.xlsx) or CSV file exported from the Tiyankhuleni consent form and writes
a CSV with the columns of the participant import (as in data/participants/test-contributors.csv),
keeping only participants who may record. Everything else in the export (mobile money details,
signatures, guardian/assistant data, verification links) is left out.

Usage (on the droplet, from the app folder; plain Python, no extra packages):
    python3 scripts/prepare_participants.py data/participants/<export>.xlsx --output data/participants/pilot.csv
    python3 scripts/prepare_participants.py data/participants/<export>.xlsx --output data/participants/pilot.csv --dry-run

Then import as usual:
    docker compose exec api python -m scripts.import_participants data/participants/pilot.csv --dry-run

Two layouts are recognised from the header row:
    - form: the consent form's own export (Reference, Participant name, WhatsApp, ..., consents);
    - simple: Name,Phone Number,Age,Sex,District,First Language,Accent (a hand-made list). It has no
      consent columns, so include only people whose consent is verified.

Who is included from a form export (all must hold):
    Review status = verified; Adult confirmed = Yes; Signer type in --signer-types (default: self,
    assisted); Participation, Public audio and Demographics publication consent = Yes; a WhatsApp
    number; not a duplicate WhatsApp number (first record kept).
In both layouts: anyone whose age (or the top of their age group) is under 18 is excluded, numbers
are normalised to +265..., and numbers that aren't Malawian mobiles (+265 + 9 digits) are flagged.
Excluded records are listed in <output>-excluded.csv by form reference and reason only (no names or
numbers), for follow-up in the form's admin.
"""

# =======
# Imports
# =======
import argparse
import csv
import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# ========
# Settings
# ========
OUTPUT_COLUMNS = [
    "name", "phone_number", "age", "sex", "district", "first_language", "accent", "is_test",
    "age_group", "consent_reference",
    "primary_device_type", "primary_device_brand", "primary_device_model",
]

# Form column -> output column
COLUMN_MAP = {
    "Participant name": "name",
    "WhatsApp": "phone_number",
    "Age group": "age_group",
    "Sex": "sex",
    "District": "district",
    "First language": "first_language",
    "Dialect": "accent",
    "Reference": "consent_reference",
    # Device questions, once the form asks them (ignored if absent)
    "Primary device type": "primary_device_type",
    "Primary device brand": "primary_device_brand",
    "Primary device model": "primary_device_model",
}

REQUIRED_FORM_COLUMNS = [
    "Reference", "Participant name", "WhatsApp", "Sex", "District", "First language", "Dialect",
    "Signer type", "Adult confirmed", "Participation consent", "Public audio consent",
    "Demographics publication consent", "Review status",
]

# Simple participant list (temporary layout): Name,Phone Number,Age,Sex,District,First Language,Accent
SIMPLE_COLUMN_MAP = {
    "Name": "name",
    "Phone Number": "phone_number",
    "Sex": "sex",
    "District": "district",
    "First Language": "first_language",
    "Accent": "accent",
    "Primary device type": "primary_device_type",
    "Primary device brand": "primary_device_brand",
    "Primary device model": "primary_device_model",
}
REQUIRED_SIMPLE_COLUMNS = ["Name", "Phone Number", "Sex", "District", "First Language", "Accent"]
AGE_COLUMN = {"form": "Age group", "simple": "Age"}
PHONE_COLUMN = {"form": "WhatsApp", "simple": "Phone Number"}

CONSENT_COLUMNS = ["Participation consent", "Public audio consent", "Demographics publication consent"]
SEX_MAP = {"female": "F", "f": "F", "male": "M", "m": "M"}
DEFAULT_SIGNER_TYPES = "self,assisted"
COUNTRY_CODE = "265"   # Malawi, for numbers written without a country code

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


# ============
# Excel reader
# ============
def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_first_sheet(path: Path) -> list[list[str]]:
    """Read the first worksheet of an .xlsx file as rows of strings (stdlib only)."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))

        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        first = workbook.find("m:sheets/m:sheet", NS)
        rel_id = first.get(REL_NS)
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == rel_id)
        sheet_path = "xl/" + target.lstrip("/").removeprefix("xl/")
        sheet = ET.fromstring(z.read(sheet_path))

    rows: list[list[str]] = []
    for row in sheet.iter(f"{{{NS['m']}}}row"):
        values: dict[int, str] = {}
        for c in row.findall("m:c", NS):
            kind = c.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            else:
                v = c.find("m:v", NS)
                text = v.text if v is not None and v.text is not None else ""
                if kind == "s" and text:
                    text = shared[int(text)]
            values[_col_index(c.get("r"))] = text
        if values:
            width = max(values) + 1
            rows.append([values.get(i, "") for i in range(width)])
    return rows


def read_csv(path: Path) -> list[list[str]]:
    """Read a CSV export (UTF-8, with or without BOM; comma or semicolon separated)."""
    text = path.read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except (csv.Error, IndexError):
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text, newline=""), dialect)]


# =========
# Cleaning
# =========
def clean(value: str) -> str:
    """Strip spaces and Excel's leading text marker ('), and treat '-' as empty."""
    v = (value or "").strip()
    if v.startswith("'"):
        v = v[1:].strip()
    v = re.sub(r"\s+", " ", v)
    return "" if v in {"-", "—", "–"} else v


def normalise_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    if value.strip().startswith("+") or digits.startswith(COUNTRY_CODE):
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+" + COUNTRY_CODE + digits[1:]
    if len(digits) == 9:
        return "+" + COUNTRY_CODE + digits
    return "+" + digits


def is_yes(value: str) -> bool:
    return clean(value).lower() in {"yes", "y", "true"}


# =========
# Age checks
# =========
def age_info(value: str) -> tuple[str, str, int | None]:
    """Return (age, age_group, upper_bound). A range like "25–29 years" goes to age_group;
    a plain number goes to age. upper_bound is the highest possible age (None if unknown)."""
    v = clean(value)
    if not v:
        return "", "", None
    if re.fullmatch(r"\d+(\.0)?", v):
        n = int(float(v))
        return str(n), "", n
    numbers = [int(x) for x in re.findall(r"\d+", v)]
    return "", v, (max(numbers) if numbers else None)


# ====
# Main
# ====
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", type=Path, help="export from the consent form (.xlsx or .csv)")
    parser.add_argument("--output", type=Path, required=True, help="CSV to write, e.g. data/participants/pilot.csv")
    parser.add_argument("--signer-types", default=DEFAULT_SIGNER_TYPES,
                        help=f"comma-separated signer types to include (default: {DEFAULT_SIGNER_TYPES})")
    parser.add_argument("--dry-run", action="store_true", help="report counts only; write nothing")
    args = parser.parse_args()

    suffix = args.export.suffix.lower()
    if suffix == ".xlsx":
        rows = read_first_sheet(args.export)
    elif suffix == ".csv":
        rows = read_csv(args.export)
    else:
        sys.exit("The export must be an .xlsx or .csv file.")
    if not rows:
        sys.exit("The export has no rows.")
    header = [clean(h) for h in rows[0]]

    # Which layout is this?
    if all(c in header for c in REQUIRED_FORM_COLUMNS):
        layout, column_map = "form", COLUMN_MAP
    elif all(c in header for c in REQUIRED_SIMPLE_COLUMNS):
        layout, column_map = "simple", SIMPLE_COLUMN_MAP
    else:
        missing_form = [c for c in REQUIRED_FORM_COLUMNS if c not in header]
        missing_simple = [c for c in REQUIRED_SIMPLE_COLUMNS if c not in header]
        sys.exit("Unrecognised columns.\n"
                 f"  Form export is missing: {', '.join(missing_form)}\n"
                 f"  Simple list is missing: {', '.join(missing_simple)}")
    print(f"Layout: {layout}" + ("  (no consent columns: include only people with verified consent)"
                                  if layout == "simple" else ""))

    idx = {name: i for i, name in enumerate(header)}
    allowed_signers = {s.strip().lower() for s in args.signer_types.split(",") if s.strip()}

    def get(row: list[str], col: str) -> str:
        i = idx.get(col)
        return clean(row[i]) if i is not None and i < len(row) else ""

    included: list[dict] = []
    excluded: list[dict] = []
    warnings: list[str] = []
    seen_phones: dict[str, str] = {}

    for line_no, row in enumerate(rows[1:], start=2):
        if not any(clean(v) for v in row):
            continue
        ref = (get(row, "Reference") if layout == "form" else "") or f"row {line_no}"
        reasons = []

        if layout == "form":
            if get(row, "Review status").lower() != "verified":
                reasons.append("not verified")
            if not is_yes(get(row, "Adult confirmed")):
                reasons.append("adult not confirmed")
            signer = get(row, "Signer type").lower()
            if signer not in allowed_signers:
                reasons.append(f"signer type '{signer or 'blank'}'")
            for col in CONSENT_COLUMNS:
                if not is_yes(get(row, col)):
                    reasons.append(f"no {col.lower()}")

        age, age_group, upper = age_info(get(row, AGE_COLUMN[layout]))
        if upper is not None and upper < 18:
            reasons.append("under 18")

        phone = normalise_phone(get(row, PHONE_COLUMN[layout]))
        if not phone:
            reasons.append("no WhatsApp number")
        elif phone in seen_phones:
            reasons.append(f"same WhatsApp number as {seen_phones[phone]}")

        if reasons:
            excluded.append({"consent_reference": ref, "reasons": "; ".join(reasons)})
            continue

        seen_phones[phone] = ref
        if not re.fullmatch(r"\+265\d{9}", phone):
            warnings.append(f"{ref}: phone number is not a Malawian mobile number (+265 and 9 digits); check it")

        out = {col: "" for col in OUTPUT_COLUMNS}
        for src_col, out_col in column_map.items():
            if src_col in idx:
                out[out_col] = get(row, src_col)
        out["phone_number"] = phone
        out["age"], out["age_group"] = age, age_group
        out["sex"] = SEX_MAP.get(out["sex"].lower(), out["sex"])
        out["is_test"] = "FALSE"
        included.append(out)

    print(f"Records in export: {len(included) + len(excluded)}")
    print(f"Included: {len(included)}")
    print(f"Excluded: {len(excluded)}")
    for e in excluded:
        print(f"  {e['consent_reference']}: {e['reasons']}")
    if warnings:
        print(f"Warnings: {len(warnings)}")
        for w in warnings:
            print(f"  {w}")

    if args.dry_run:
        print("DRY RUN: nothing written.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(included)
    print(f"Wrote {args.output}")

    if excluded:
        excl_path = args.output.with_name(args.output.stem + "-excluded.csv")
        with excl_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["consent_reference", "reasons"])
            writer.writeheader()
            writer.writerows(excluded)
        print(f"Wrote {excl_path} (references or row numbers and reasons only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())