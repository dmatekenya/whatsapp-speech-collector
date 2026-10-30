import csv
from pathlib import Path

import pytest
from sqlalchemy import select

from src.importers.framework import FieldError, ImportReport, load_config, run_import
from src.importers.prompts import DEFAULT_CONFIG, PromptImporter, SentenceRules, normalize_domain
from src.storage import db
from src.storage.models import Prompt

HEADER = ["text", "source", "domain"]

# Sentences inside the 8-25 word target, so they import without flags.
S1 = "Moni, muli bwanji lero kuno ku mudzi kwathu?"
S2 = "Ana akusewera panja pafupi ndi sukulu ya pulayimale lero."
S3 = "Mvula yagwa kwambiri lero ndipo alimi ali okondwa kwambiri."
S4 = "Tiwonana mawa m'mawa ku msika wa tauni ya Zomba."
S5 = "Zikomo kwambiri chifukwa cha thandizo lanu lonse pa ntchito iyi."


def words(n: int) -> str:
    return " ".join(["mawu"] * n)


# ============================================================
# Helpers
# ============================================================
@pytest.fixture(scope="module")
def rules() -> SentenceRules:
    return SentenceRules(load_config(DEFAULT_CONFIG).options["prompt_types"]["sentences"])


def write_csv(tmp_path: Path, name: str, rows: list[list[str]], header: list[str] = HEADER) -> Path:
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def run(path: Path, **kwargs: bool) -> ImportReport:
    importer = PromptImporter.from_file("sentences", path)
    return run_import(importer, path, db.get_sync_sessionmaker(), **kwargs)


def prompts() -> list[Prompt]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Prompt).order_by(Prompt.id)))


# ============================================================
# Text rules
# ============================================================
def test_normalized_text(rules: SentenceRules) -> None:
    assert rules.normalize("Moni, muli bwanji?") == "moni, muli bwanji"
    assert rules.normalize("  MONI,   muli bwanji ") == "moni, muli bwanji"
    assert rules.normalize("Ndi’ye amene.") == rules.normalize("ndi'ye amene")
    assert rules.normalize('Iye anati "inde"!') == 'iye anati "inde'


def test_domain_normalized() -> None:
    assert normalize_domain("  Health Care ") == "health_care"
    assert normalize_domain("AGRICULTURE") == "agriculture"


def test_sentence_rules(rules: SentenceRules) -> None:
    assert rules.clean("Ŵana  akuŵerenga buku.") == "Ŵana akuŵerenga buku."
    assert rules.clean('Iye anati: "Inde, ndi’ye!"') == 'Iye anati: "Inde, ndi’ye!"'
    with pytest.raises(ValueError, match="contains digits"):
        rules.clean("Ana 3 akusewera panja.")
    with pytest.raises(ValueError, match="aren't allowed: '#' '@'"):
        rules.clean("Tumizani @ ku #nambala")


def test_hard_word_limits(rules: SentenceRules) -> None:
    assert rules.clean(words(3)) and rules.clean(words(50))
    with pytest.raises(FieldError, match=r"has 2 word\(s\) \(min 3\)") as error:
        rules.clean(words(2))
    assert error.value.reasons == ["fewer than 3 words"]
    with pytest.raises(FieldError, match=r"has 51 words \(max 50\)") as error:
        rules.clean(words(51))
    assert error.value.reasons == ["more than 50 words"]


def test_length_flag_outside_target(rules: SentenceRules) -> None:
    assert rules.length_flag(words(3)) == "short"
    assert rules.length_flag(words(7)) == "short"
    assert rules.length_flag(words(8)) is None
    assert rules.length_flag(words(25)) is None
    assert rules.length_flag(words(26)) == "long"
    assert rules.length_flag(words(50)) == "long"


@pytest.mark.parametrize(
    ("text", "joined"),
    [
        ("Kodi munamvapo?\n\nAmayi amaikidwa.", "Kodi munamvapo? Amayi amaikidwa."),
        ("Moni bambo.\r\nTiwonana mawa.", "Moni bambo. Tiwonana mawa."),
        ("Moni bambo!\rZikomo kwambiri.", "Moni bambo! Zikomo kwambiri."),
        ("Moni bambo\nTiwonana mawa", "Moni bambo. Tiwonana mawa"),  # ". " inserted
        ("Iye anati,\r\n\r\n  moni", "Iye anati. moni"),  # comma replaced, not ",."
        ('Iye anati "inde?"\nZikomo', 'Iye anati "inde?" Zikomo'),  # end before quote
        ("Pali  chinanso?\r\n \r\nPali   chinanso", "Pali chinanso? Pali chinanso"),
    ],
)
def test_line_breaks_joined(rules: SentenceRules, text: str, joined: str) -> None:
    assert rules.has_line_breaks(text)
    assert rules.join_lines(text) == joined
    assert rules.clean(text) == joined


def test_all_caps_words(rules: SentenceRules) -> None:
    assert rules.all_caps_words("Kodi munamvapo za HIV ndi AIDS kapena BCG?") == []
    assert rules.all_caps_words("Jakisoni wa DMPA-SC kapena SP ndi TB") == []  # exception, <3 letters
    assert rules.all_caps_words("Mkaka wa Lactogen kapena Nan") == []
    assert rules.all_caps_words("Tsopano NDIKUFUNA kuti M'MAWA, NDIKUFUNA") == ["NDIKUFUNA", "MAWA"]
    assert rules.all_caps_words("ŴANA akuŵerenga") == ["ŴANA"]
    assert rules.all_caps_words("Hiv ndi hiv") == []  # only fully capitalized words count


def test_every_broken_rule_is_reported(rules: SentenceRules) -> None:
    text = "Ana 3 akusewera/akuthamanga.\n" + words(60)
    with pytest.raises(FieldError) as error:
        rules.clean(text)
    assert error.value.reasons == [
        "contains digits",
        "contains characters that aren't allowed: '/'",
        "more than 50 words",
    ]


# ============================================================
# Import
# ============================================================
def test_import_assigns_ids_dedupes_and_counts_domains(clean_db: None, tmp_path: Path) -> None:
    rows = [
        [S1, "dhs", "Health "],
        [S2, "dhs", "health"],
        ["moni,  muli bwanji lero kuno ku mudzi   kwathu", "dhs", "Health"],  # dup of row 2
        [S3, "dhs", "Agriculture"],
    ]

    report = run(write_csv(tmp_path, "batch.csv", rows))

    assert report.ok, report.render()
    assert report.counts["imported"] == 3
    assert report.counts["rejected"] == 0
    assert report.counts["skipped: duplicate in file"] == 1
    assert [(w.row, w.message) for w in report.warnings] == [(4, "duplicate of row 2; skipped")]
    assert report.value_counts["domain"] == {"health": 3, "agriculture": 1}
    assert "per domain:" in report.render()

    stored = prompts()
    assert [p.prompt_id for p in stored] == ["S00001", "S00002", "S00003"]
    first = stored[0]
    assert first.prompt_type == "sentence"
    assert first.text == S1
    assert first.normalized_text == "moni, muli bwanji lero kuno ku mudzi kwathu"
    assert first.domain == "health"
    assert first.source_file == "batch.csv"
    assert first.active is True
    assert first.flags == {}


def test_reimport_and_overlap_never_duplicate(clean_db: None, tmp_path: Path) -> None:
    first = write_csv(tmp_path, "one.csv", [[S1, "dhs", "health"], [S4, "dhs", "health"]])
    run(first)

    again = run(first)
    assert again.counts["skipped: already in prompt bank"] == 2 and again.counts["imported"] == 0

    second = write_csv(tmp_path, "two.csv", [[S1.upper(), "other", "health"], [S5, "dhs", "social"]])
    report = run(second)
    assert report.counts["skipped: already in prompt bank"] == 1 and report.counts["imported"] == 1
    assert [p.prompt_id for p in prompts()] == ["S00001", "S00002", "S00003"]


def test_optional_columns_go_to_attributes(clean_db: None, tmp_path: Path) -> None:
    header = [*HEADER, "license", "notes"]
    run(write_csv(tmp_path, "batch.csv", [[S1, "dhs", "health", "CC-BY-4.0", ""]], header))

    assert prompts()[0].attributes == {"license": "CC-BY-4.0"}


# ============================================================
# Soft length rule
# ============================================================
def test_length_flags_stored_and_reported(clean_db: None, tmp_path: Path) -> None:
    short = "Moni muli bwanji."  # 3 words
    long = words(30)
    rows = [
        [short, "dhs", "health"],
        [S1, "dhs", "health"],
        [long, "dhs", "health"],
        ["Moni bwanji.", "dhs", "health"],  # 2 words: below the hard limit
        [words(51), "dhs", "health"],  # above the hard limit
    ]

    dry = run(write_csv(tmp_path, "lengths.csv", rows), dry_run=True)
    assert dry.counts["flagged length=short"] == 1 and dry.counts["flagged length=long"] == 1

    report = run(write_csv(tmp_path, "lengths.csv", rows))

    assert report.counts["imported"] == 3 and report.counts["rejected"] == 2
    assert {p.text: p.flags for p in prompts()} == {
        short: {"length": "short"},
        S1: {},
        long: {"length": "long"},
    }
    assert all(p.active for p in prompts())
    assert [(w.row, w.message) for w in report.warnings] == [
        (2, "3 words, outside the target 8-25; imported with flag length=short"),
        (4, "30 words, outside the target 8-25; imported with flag length=long"),
    ]
    assert report.rejection_summary() == {
        "text: fewer than 3 words": 1,
        "text: more than 50 words": 1,
    }
    rendered = report.render()
    assert "flagged length=short: 1" in rendered and "flagged length=long: 1" in rendered

    # Re-importing flags nothing new: the prompts already exist.
    again = run(write_csv(tmp_path, "lengths.csv", rows))
    assert again.warnings == [] and "flagged length=short" not in again.counts


def test_joined_lines_imported_flagged_and_checked_as_joined(
    clean_db: None, tmp_path: Path
) -> None:
    multi = "Kodi munamvapo za njira iyi\r\n\r\nAmayi amaikidwa timachubu tiwiri pa mkono."
    rows = [
        [multi, "dhs", "health"],
        [S1, "dhs", "health"],
        # The single-line form of row 2 once joined: a duplicate, so skipped.
        ["Kodi munamvapo za njira iyi. Amayi amaikidwa timachubu tiwiri pa mkono.", "dhs", "health"],
        # Each line is short, but joined it's over the hard limit.
        [words(30) + "\n" + words(30), "dhs", "health"],
    ]

    report = run(write_csv(tmp_path, "joined.csv", rows))

    assert report.counts["imported"] == 2
    assert report.counts["flagged joined_lines"] == 1
    assert report.counts["skipped: duplicate in file"] == 1
    assert report.rejection_summary() == {"text: more than 50 words": 1}
    joined, plain = prompts()
    assert joined.text == "Kodi munamvapo za njira iyi. Amayi amaikidwa timachubu tiwiri pa mkono."
    assert joined.flags == {"joined_lines": True}
    assert plain.flags == {}
    warning = next(w for w in report.warnings if w.row == 2)
    assert warning.message.startswith("line breaks joined into one line; imported with flag joined_lines")
    assert "flagged joined_lines: 1" in report.render()


def test_all_caps_imported_flagged_and_reported(clean_db: None, tmp_path: Path) -> None:
    caps = "Tsopano NDIKUFUNA kuti mundiwerengere chiganizo ichi chonse bwino."
    allowed = "Kodi munamvapo za matenda a HIV ndi AIDS kuno kwanu?"
    rows = [[caps, "dhs", "health"], [allowed, "dhs", "health"], [S1, "dhs", "health"]]

    report = run(write_csv(tmp_path, "caps.csv", rows))

    assert report.counts["imported"] == 3 and report.counts["flagged all_caps"] == 1
    assert {p.text: p.flags for p in prompts()} == {caps: {"all_caps": True}, allowed: {}, S1: {}}
    assert [(w.row, w.message) for w in report.warnings] == [
        (2, "all-caps word(s) NDIKUFUNA; imported with flag all_caps")
    ]
    assert "flagged all_caps: 1" in report.render()


def test_flags_combine(clean_db: None, tmp_path: Path) -> None:
    text = "Tsopano NDIKUFUNA\nmundiwerengere"
    report = run(write_csv(tmp_path, "both.csv", [[text, "dhs", "health"]]))

    assert prompts()[0].flags == {"joined_lines": True, "all_caps": True, "length": "short"}
    assert report.counts["flagged joined_lines"] == 1
    assert report.counts["flagged all_caps"] == 1
    assert report.counts["flagged length=short"] == 1


# ============================================================
# Partial import and rejected rows
# ============================================================
BAD_ROWS = [
    [S1, "dhs", "health"],
    ["Ana 3 akusewera panja pafupi ndi sukulu yathu lero.", "dhs", "health"],
    [words(51), "dhs", "health"],
    ["Tumizani uthenga ku #nambala yathu lero masana ano.", "dhs", "health"],
    ["Palibe gwero la chiganizo ichi pano lero kuno.", "", "health"],
    # Joined into one line first, then rejected for the '#'.
    ["Kodi munamvapo za njira #iyi?\n\nAmayi amaikidwa timachubu tiwiri pa mkono.", "dhs", "health"],
    [S4, "dhs", "health"],
]


def test_partial_import_skips_invalid_rows_and_writes_rejected_file(
    clean_db: None, tmp_path: Path
) -> None:
    path = write_csv(tmp_path, "bad.csv", BAD_ROWS)
    original = path.read_bytes()

    report = run(path)

    assert report.ok and report.committed
    assert [p.text for p in prompts()] == [S1, S4]
    assert report.counts["imported"] == 2 and report.counts["rejected"] == 5
    # The error list is kept, row by row.
    assert [(i.row, i.column) for i in report.errors] == [
        (3, "text"),
        (4, "text"),
        (5, "text"),
        (6, "source"),
        (7, "text"),
    ]
    assert report.rejection_summary() == {
        "text: contains digits": 1,
        "text: more than 50 words": 1,
        "text: contains characters that aren't allowed: '#'": 2,
        "source: required value is missing": 1,
    }
    rendered = report.render()
    assert "rejection reasons:" in rendered
    assert "RESULT: imported; 5 row(s) rejected" in rendered

    assert path.read_bytes() == original
    rejected_file = tmp_path / "bad_rejected.csv"
    rejected = read_rows(rejected_file)
    assert list(rejected[0]) == ["text", "source", "domain", "rejection_reason"]
    assert [row["rejection_reason"] for row in rejected] == [
        "text: contains digits",
        "text: more than 50 words",
        "text: contains characters that aren't allowed: '#'",
        "source: required value is missing",
        "text: contains characters that aren't allowed: '#'",
    ]
    assert rejected[4]["text"] == BAD_ROWS[5][0]  # kept as in the file, line breaks and all


def test_fixed_rejected_file_can_be_imported(clean_db: None, tmp_path: Path) -> None:
    run(write_csv(tmp_path, "bad.csv", BAD_ROWS))
    rejected_file = tmp_path / "bad_rejected.csv"
    fixed = [
        {**row, "text": row["text"].replace("3", "atatu").replace("#", "")}
        for row in read_rows(rejected_file)
        if row["rejection_reason"] == "text: contains digits" or "'#'" in row["rejection_reason"]
    ]
    with rejected_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fixed[0]))
        writer.writeheader()
        writer.writerows(fixed)

    report = run(rejected_file)

    # rejection_reason isn't flagged as an unknown column or stored.
    assert not any(w.column == "rejection_reason" for w in report.warnings)
    assert report.counts["imported"] == 3 and report.counts["rejected"] == 0
    assert all("extra" not in p.attributes for p in prompts())


def test_strict_mode_imports_nothing(clean_db: None, tmp_path: Path) -> None:
    path = write_csv(tmp_path, "bad.csv", BAD_ROWS)

    report = run(path, strict=True)

    assert not report.ok and not report.committed
    assert prompts() == []
    assert len(report.rejected) == 5
    assert "nothing imported; 5 row(s) have errors" in report.render()
    # The rejected rows are still written, so they can be fixed.
    assert len(read_rows(tmp_path / "bad_rejected.csv")) == 5

    # A clean file imports normally in strict mode.
    good = write_csv(tmp_path, "good.csv", [BAD_ROWS[0], BAD_ROWS[-1]])
    assert run(good, strict=True).committed
    assert [p.prompt_id for p in prompts()] == ["S00001", "S00002"]


def test_strict_mode_allows_flagged_rows(clean_db: None, tmp_path: Path) -> None:
    # Flags are warnings, not errors: they never block a strict import.
    report = run(write_csv(tmp_path, "flagged.csv", [[words(30), "dhs", "health"]]), strict=True)

    assert report.committed and prompts()[0].flags == {"length": "long"}


def test_dry_run_reports_counts_and_writes_no_files(clean_db: None, tmp_path: Path) -> None:
    rows = [*BAD_ROWS, ["moni, muli bwanji lero kuno ku mudzi kwathu", "dhs", "health"]]
    report = run(write_csv(tmp_path, "bad.csv", rows), dry_run=True)

    assert report.counts["would import"] == 2
    assert report.counts["rejected"] == 5
    assert report.counts["skipped: duplicate in file"] == 1
    assert "5 row(s) would be rejected" in report.render()
    assert prompts() == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.csv"]


def test_duplicate_of_a_rejected_row_is_imported(clean_db: None, tmp_path: Path) -> None:
    # The first copy is rejected (missing source); the second, valid copy must
    # not be skipped as its duplicate.
    rows = [[S1, "", "health"], [S1, "dhs", "health"]]

    report = run(write_csv(tmp_path, "dupes.csv", rows))

    assert report.counts["imported"] == 1 and report.counts["rejected"] == 1
    assert [p.text for p in prompts()] == [S1]


def test_dry_run_and_ids_copy(clean_db: None, tmp_path: Path) -> None:
    path = write_csv(tmp_path, "batch.csv", [[S1, "dhs", "health"], [S1.rstrip("?"), "dhs", "health"]])

    dry = run(path, dry_run=True)
    assert dry.ok and prompts() == []
    assert "starting at S00001" in dry.render()

    run(path, write_ids=True)
    with (tmp_path / "batch_imported.csv").open(newline="") as handle:
        copied = list(csv.DictReader(handle))
    # The in-file duplicate gets the ID of the row it duplicates.
    assert [row["prompt_id"] for row in copied] == ["S00001", "S00001"]
