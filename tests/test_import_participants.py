import csv
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from src.importers.framework import ImportReport, run_import
from src.importers.participants import ParticipantImporter
from src.registry.participants import normalize_phone
from src.storage import db
from src.storage.models import Participant

HEADER = ["name", "phone_number", "age", "sex", "district", "first_language", "accent"]
TODAY = date(2026, 10, 17)

# Fake people and numbers only; never the real batch files.
ALICE = ["Alice Banda", "+265 888 72 15 91", "34", "f", "Zomba", "Chichewa", "southern"]
BEN = ["Ben Phiri", "0999 123 456", "41", "M", "Lilongwe", "Chichewa", "central"]
CARO = ["Caro Test", "+1 415 523 8886", "29", "F", "  Blantyre  ", "English", ""]
DAN = ["Dan Mwale", "0888 111 222", "55", "M", "Mzuzu", "Tumbuka", "northern"]


# ============================================================
# Helpers
# ============================================================
def write_csv(tmp_path: Path, name: str, rows: list[list[str]], header: list[str] = HEADER) -> Path:
    path = tmp_path / name
    # CRLF like the real exports.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def run(path: Path, **kwargs: bool) -> ImportReport:
    importer = ParticipantImporter.from_file(path)
    importer.today = TODAY
    return run_import(importer, path, db.get_sync_sessionmaker(), **kwargs)


def participants() -> list[Participant]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Participant).order_by(Participant.id)))


# ============================================================
# Phone numbers
# ============================================================
@pytest.mark.parametrize("raw", ["+265 888 72 15 91", "0888721591", "888721591", "+265888721591"])
def test_malawi_numbers_normalize_to_e164(raw: str) -> None:
    assert normalize_phone(raw, "MW", ["265", "1"]) == "+265888721591"


def test_us_test_number_allowed_with_plus_one() -> None:
    assert normalize_phone("+1 415 523 8886", "MW", ["265", "1"]) == "+14155238886"


@pytest.mark.parametrize(
    "raw",
    [
        "12345",  # too short
        "0888 72 15 9",  # missing a digit
        "(415) 523-8886",  # US number without +1 is read as Malawian: rejected, not guessed
        "not a number",
        "+44 20 7946 0958",  # valid UK number, but +44 isn't allowed
    ],
)
def test_invalid_numbers_rejected(raw: str) -> None:
    with pytest.raises(ValueError) as error:
        normalize_phone(raw, "MW", ["265", "1"])
    assert "4155238886" not in str(error.value) and "88872159" not in str(error.value)


# ============================================================
# Import
# ============================================================
def test_ids_assigned_with_defaults_and_unique_across_files(clean_db: None, tmp_path: Path) -> None:
    first = run(write_csv(tmp_path, "batch_one.csv", [ALICE, BEN]))
    second = run(write_csv(tmp_path, "batch_two.csv", [CARO, DAN]))

    assert first.ok and second.ok, (first.render(), second.render())
    rows = participants()
    assert [p.participant_id for p in rows] == ["P0001", "P0002", "P0003", "P0004"]
    alice, ben, caro, _ = rows
    assert alice.phone_number == "+265888721591"
    assert ben.phone_number == "+265999123456"
    assert caro.phone_number == "+14155238886"
    assert alice.status == "active" and alice.is_test is False
    assert alice.batch == "batch_one" and caro.batch == "batch_two"
    assert alice.enrolled_on == TODAY
    assert alice.source_file == "batch_one.csv"
    assert alice.attributes == {
        "age": 34,
        "sex": "F",
        "district": "Zomba",
        "first_language": "Chichewa",
        "accent": "southern",
    }
    assert caro.attributes["district"] == "Blantyre"
    assert "accent" not in caro.attributes  # blank values aren't stored


def test_reimport_is_idempotent_and_matches_by_phone(clean_db: None, tmp_path: Path) -> None:
    path = write_csv(tmp_path, "batch.csv", [ALICE, BEN])
    run(path)
    before = [(p.participant_id, p.updated_at) for p in participants()]

    again = run(path)

    assert again.ok
    assert again.counts["unchanged"] == 2 and again.counts["new"] == 0
    assert [(p.participant_id, p.updated_at) for p in participants()] == before

    # Same people, numbers written differently, one attribute changed.
    alice = ["Alice Banda", "0888721591", "35", "F", "Zomba", "Chichewa", "southern"]
    changed = run(write_csv(tmp_path, "batch_fix.csv", [alice, BEN]))

    assert changed.counts["updated"] == 1 and changed.counts["unchanged"] == 1
    rows = participants()
    assert [p.participant_id for p in rows] == ["P0001", "P0002"]
    assert rows[0].attributes["age"] == 35
    assert rows[0].batch == "batch" and rows[0].source_file == "batch.csv"  # kept


def test_ids_copy_written_and_original_untouched(clean_db: None, tmp_path: Path) -> None:
    path = write_csv(tmp_path, "test_participants.csv", [ALICE, BEN])
    original = path.read_bytes()

    report = run(path, write_ids=True)

    assert path.read_bytes() == original
    copy = tmp_path / "test_participants_imported.csv"
    assert any(str(copy) in note for note in report.notes)
    with copy.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["participant_id"] for row in rows] == ["P0001", "P0002"]
    assert rows[0]["name"] == "Alice Banda"

    # Re-importing the copy matches by participant_id: nothing new.
    again = run(copy)
    assert again.counts["unchanged"] == 2 and again.counts["new"] == 0
    assert len(participants()) == 2


def test_validation_errors_reported_without_pii(clean_db: None, tmp_path: Path) -> None:
    rows = [
        ["Young Person", "0888 300 400", "17", "F", "Zomba", "Chichewa", ""],
        ["Odd Age", "0888 300 401", "abc", "M", "Zomba", "Chichewa", ""],
        ["Bad Sex", "0888 300 402", "30", "X", "Zomba", "Chichewa", ""],
        ["Bad Phone", "0888 72 15 9", "30", "F", "Zomba", "Chichewa", ""],
        ["", "0888 300 404", "30", "F", "Zomba", "Chichewa", ""],
    ]

    report = run(write_csv(tmp_path, "bad.csv", rows))

    issues = {(issue.row, issue.column) for issue in report.errors}
    assert issues == {
        (2, "age"),
        (3, "age"),
        (4, "sex"),
        (5, "phone_number"),
        (6, "name"),
    }
    rendered = report.render()
    assert "must be at least 18" in rendered
    assert "nothing imported" in rendered
    for secret in ("Young Person", "Bad Phone", "300 400", "88872159"):
        assert secret not in rendered
    assert participants() == []


def test_one_invalid_row_rolls_back_whole_file(clean_db: None, tmp_path: Path) -> None:
    bad_ben = ["Ben Phiri", "0999 123 456", "41", "M", "Lilongwe", "Chichewa", "central"]
    bad_ben[2] = "200"

    report = run(write_csv(tmp_path, "mixed.csv", [ALICE, bad_ben, CARO]))

    assert not report.ok
    assert participants() == []
    # No IDs were consumed: the next good import still starts at P0001.
    run(write_csv(tmp_path, "good.csv", [ALICE]))
    assert [p.participant_id for p in participants()] == ["P0001"]


def test_dry_run_writes_nothing(clean_db: None, tmp_path: Path) -> None:
    path = write_csv(tmp_path, "batch.csv", [ALICE, BEN])

    report = run(path, dry_run=True, write_ids=True)

    assert report.ok and report.counts["new"] == 2
    assert "would assign 2 new ID(s), starting at P0001" in report.render()
    assert participants() == []
    assert not (tmp_path / "batch_imported.csv").exists()


def test_duplicates_and_unknown_ids_in_file_are_errors(clean_db: None, tmp_path: Path) -> None:
    same_phone = ["Alice Again", "0888721591", "34", "F", "Zomba", "Chichewa", ""]
    report = run(write_csv(tmp_path, "dupes.csv", [ALICE, same_phone]))
    assert [(i.row, i.column) for i in report.errors] == [(3, "phone_number")]

    header = ["participant_id", *HEADER]
    report = run(write_csv(tmp_path, "ids.csv", [["P0099", *ALICE]], header=header))
    assert [(i.row, i.column) for i in report.errors] == [(2, "participant_id")]
    assert participants() == []


def test_unknown_columns_flagged_and_kept(clean_db: None, tmp_path: Path) -> None:
    header = [*HEADER, "Village "]
    report = run(write_csv(tmp_path, "extra.csv", [[*ALICE, "Chinamwali"]], header=header))

    assert report.ok
    assert [(w.row, w.column) for w in report.warnings] == [(1, "village")]
    assert participants()[0].attributes["extra"] == {"village": "Chinamwali"}


def test_missing_required_column_is_an_error(clean_db: None, tmp_path: Path) -> None:
    header = [column for column in HEADER if column != "phone_number"]
    rows = [[value for column, value in zip(HEADER, ALICE) if column != "phone_number"]]

    report = run(write_csv(tmp_path, "no_phone.csv", rows, header=header))

    assert any(i.row == 1 and i.column == "phone_number" for i in report.errors)
    assert participants() == []
