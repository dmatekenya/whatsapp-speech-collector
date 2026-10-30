import csv
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from src.config import PROJECT_ROOT
from src.importers.framework import FieldError, ImportReport, load_config, run_import
from src.importers.prompts import DEFAULT_CONFIG, PromptImporter, SentenceRules
from src.storage import db
from src.storage.models import Prompt

HEADER = ["text", "source", "domain"]

Q1 = "Tell me about the last time you visited the market."
Q2 = "What did you eat for breakfast this morning?"
Q3 = "Describe your favourite place in your village."


# ============================================================
# Helpers
# ============================================================
@pytest.fixture(scope="module")
def rules() -> SentenceRules:
    return SentenceRules(load_config(DEFAULT_CONFIG).options["prompt_types"]["conversation_starters"])


def write_csv(tmp_path: Path, name: str, rows: list[list[str]], header: list[str] = HEADER) -> Path:
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def run(path: Path, prompt_type: str = "conversation_starters", **kwargs: bool) -> ImportReport:
    importer = PromptImporter.from_file(prompt_type, path)
    return run_import(importer, path, db.get_sync_sessionmaker(), **kwargs)


def prompts(prompt_type: str = "conversation_starter") -> list[Prompt]:
    with db.get_sync_sessionmaker()() as session:
        return list(
            session.scalars(
                select(Prompt).where(Prompt.prompt_type == prompt_type).order_by(Prompt.id)
            )
        )


def words(n: int) -> str:
    return " ".join(["word"] * n)


# ============================================================
# Rules
# ============================================================
def test_hard_limits_only_no_length_flags(rules: SentenceRules) -> None:
    for n in (3, 5, 30, 40):
        assert rules.clean(words(n))
        assert rules.length_flag(words(n)) is None
    with pytest.raises(FieldError) as error:
        rules.clean(words(2))
    assert error.value.reasons == ["fewer than 3 words"]
    with pytest.raises(FieldError) as error:
        rules.clean(words(41))
    assert error.value.reasons == ["more than 40 words"]


def test_digits_and_any_characters_allowed(rules: SentenceRules) -> None:
    text = "What happened in 2024 at the #1 market (in town) / village?"
    assert rules.clean(text) == text


def test_shared_rules_still_apply(rules: SentenceRules) -> None:
    assert rules.clean("Tell me about your day\nWhat did you do?") == (
        "Tell me about your day. What did you do?"
    )
    assert rules.all_caps_words("Tell me about NDIKUFUNA and HIV") == ["NDIKUFUNA"]
    assert rules.normalize("Tell me about it!") == rules.normalize("tell  me about it")


# ============================================================
# Import
# ============================================================
def test_ids_from_own_sequence(clean_db: None, tmp_path: Path) -> None:
    run(write_csv(tmp_path, "s.csv", [[Q1, "dhs", "health"]]), "sentences")
    report = run(write_csv(tmp_path, "c.csv", [[Q1, "team", "daily_life"], [Q2, "team", ""]]))

    assert report.ok, report.render()
    assert [p.prompt_id for p in prompts()] == ["C0001", "C0002"]
    # Same text as a sentence prompt isn't a duplicate across types.
    assert [p.prompt_id for p in prompts("sentence")] == ["S00001"]
    first, second = prompts()
    assert first.prompt_type == "conversation_starter"
    assert first.domain == "daily_life"
    assert second.domain is None  # domain is optional
    assert first.flags == {} and second.flags == {}


def test_optional_columns_and_required_source(clean_db: None, tmp_path: Path) -> None:
    header = ["text", "source", "domain", "notes"]
    rows = [[Q1, "team", "", "pilot round"], [Q2, "", "", ""]]

    report = run(write_csv(tmp_path, "c.csv", rows, header))

    assert report.counts["imported"] == 1 and report.counts["rejected"] == 1
    assert report.rejection_summary() == {"source: required value is missing": 1}
    assert prompts()[0].attributes == {"notes": "pilot round"}
    assert len(list(csv.DictReader((tmp_path / "c_rejected.csv").open()))) == 1


def test_domain_column_optional_in_header(clean_db: None, tmp_path: Path) -> None:
    report = run(write_csv(tmp_path, "c.csv", [[Q1, "team"]], header=["text", "source"]))

    assert report.ok and report.counts["imported"] == 1
    assert prompts()[0].domain is None


def test_dedupe_flags_and_no_length_counts(clean_db: None, tmp_path: Path) -> None:
    rows = [
        [Q1, "team", "daily_life"],
        ["tell me about the last time you visited the market", "team", ""],  # duplicate
        ["Tell me about your day\r\n\r\nWhat did you do after that?", "team", ""],
        ["What do you think about NDIKUFUNA in 2024?", "team", ""],
        [Q3, "team", ""],
        ["Hi there", "team", ""],  # 2 words
        [words(41), "team", ""],
    ]

    report = run(write_csv(tmp_path, "c.csv", rows))

    assert report.counts["imported"] == 4
    assert report.counts["skipped: duplicate in file"] == 1
    assert report.counts["flagged joined_lines"] == 1
    assert report.counts["flagged all_caps"] == 1
    assert not any(name.startswith("flagged length") for name in report.counts)
    assert report.rejection_summary() == {
        "text: fewer than 3 words": 1,
        "text: more than 40 words": 1,
    }
    flags = {p.text: p.flags for p in prompts()}
    assert flags["Tell me about your day. What did you do after that?"] == {"joined_lines": True}
    assert flags["What do you think about NDIKUFUNA in 2024?"] == {"all_caps": True}

    again = run(write_csv(tmp_path, "c.csv", rows))
    assert again.counts["skipped: already in prompt bank"] == 4 and again.counts["imported"] == 0


def test_cli_dry_run(clean_db: None, tmp_path: Path) -> None:
    path = write_csv(tmp_path, "c.csv", [[Q1, "team", ""], [Q2, "team", ""]])

    result = subprocess.run(
        [sys.executable, "-m", "scripts.import_prompts", "conversation_starters", str(path), "--dry-run"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "PROMPTS (conversation_starters) DRY RUN: c.csv" in result.stdout
    assert "would assign 2 new ID(s), starting at C0001" in result.stdout
    assert prompts() == []
