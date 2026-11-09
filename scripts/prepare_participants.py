"""Turn the consent-form Excel export into a participant CSV ready for import.

Reads the Excel file exported from the Tiyankhuleni consent form (sheet "Consent Records") and writes
a CSV with the columns of the participant import (as in data/participants/test-contributors.csv),
keeping only participants who may record. Everything else in the export (mobile money details,
signatures, guardian/assistant data, verification links) is left out.

Usage (on the droplet, from the app folder; plain Python, no extra packages):
    python3 scripts/prepare_participants.py data/participants/<export>.xlsx --output data/participants/pilot.csv
    python3 scripts/prepare_participants.py data/participants/<export>.xlsx --output data/participants/pilot.csv --dry-run

Then import as usual:
    docker compose exec api python -m scripts.import_participants data/participants/pilot.csv --dry-run

Who is included (all must hold):
    Review status = verified; Adult confirmed = Yes; Signer type in --signer-types (default: self,
    assisted); Participation, Public audio and Demographics publication consent = Yes; a WhatsApp
    number; not a duplicate WhatsApp number (first record kept).
Excluded records are listed in <output>-excluded.csv by form reference and reason only (no names or
numbers), for follow-up in the form's admin.
"""

# =======
# Imports
# =======
import argparse
import csv
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


# ====
# Main
# ====
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", type=Path, help="Excel export from the consent form (.xlsx)")
    parser.add_argument("--output", type=Path, required=True, help="CSV to write, e.g. data/participants/pilot.csv")
    parser.add_argument("--signer-types", default=DEFAULT_SIGNER_TYPES,
                        help=f"comma-separated signer types to include (default: {DEFAULT_SIGNER_TYPES})")
    parser.add_argument("--dry-run", action="store_true", help="report counts only; write nothing")
    args = parser.parse_args()

    if args.export.suffix.lower() != ".xlsx":
        sys.exit("The export must be an .xlsx file.")
    rows = read_first_sheet(args.export)
    if not rows:
        sys.exit("The export has no rows.")
    header = [clean(h) for h in rows[0]]
    missing = [c for c in REQUIRED_FORM_COLUMNS if c not in header]
    if missing:
        sys.exit(f"Columns missing from the export: {', '.join(missing)}")
    idx = {name: i for i, name in enumerate(header)}
    allowed_signers = {s.strip().lower() for s in args.signer_types.split(",") if s.strip()}

    def get(row: list[str], col: str) -> str:
        i = idx.get(col)
        return clean(row[i]) if i is not None and i < len(row) else ""

    included: list[dict] = []
    excluded: list[dict] = []
    seen_phones: set[str] = set()

    for row in rows[1:]:
        if not any(clean(v) for v in row):
            continue
        ref = get(row, "Reference") or "(no reference)"
        reasons = []
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
        phone = normalise_phone(get(row, "WhatsApp"))
        if not phone:
            reasons.append("no WhatsApp number")
        elif phone in seen_phones:
            reasons.append("duplicate WhatsApp number")

        if reasons:
            excluded.append({"consent_reference": ref, "reasons": "; ".join(reasons)})
            continue

        seen_phones.add(phone)
        out = {col: "" for col in OUTPUT_COLUMNS}
        for form_col, out_col in COLUMN_MAP.items():
            if form_col in idx:
                out[out_col] = get(row, form_col)
        out["phone_number"] = phone
        out["sex"] = SEX_MAP.get(out["sex"].lower(), out["sex"])
        out["is_test"] = "FALSE"
        included.append(out)

    print(f"Records in export: {len(included) + len(excluded)}")
    print(f"Included: {len(included)}")
    print(f"Excluded: {len(excluded)}")
    for e in excluded:
        print(f"  {e['consent_reference']}: {e['reasons']}")

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
        print(f"Wrote {excl_path} (references and reasons only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
