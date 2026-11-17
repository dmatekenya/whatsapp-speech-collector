"""Tests for the picture-candidate harvester. No network: sources and downloads are faked."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.prompt_generation import checks, filters
from src.prompt_generation.config import load_config
from src.prompt_generation.harvest import run_harvest
from src.prompt_generation.models import Candidate
from src.prompt_generation.sources.commons import CommonsSource, parse_response

HERE = Path(__file__).parent
REPO_ROOT = HERE.parents[1]
FIXTURE = json.loads((HERE / "fixtures" / "commons-search.json").read_text())


# ========
# FIXTURES
# ========


@pytest.fixture
def cfg(tmp_path):
    base = load_config(REPO_ROOT / "config" / "prompt-generation.yaml")
    return replace(base, output_root=tmp_path / "harvest", cache_dir=tmp_path / "cache")


def make_candidate(**kwargs) -> Candidate:
    defaults = dict(
        source="commons", source_id="1", query="q", query_domain="food",
        licence="CC BY 4.0", mime_type="image/jpeg", width=1600, height=1200,
        tags=["Markets in Malawi"], image_url="https://example.org/1.jpg",
    )
    defaults.update(kwargs)
    return Candidate(**defaults)


def draw_image(path: Path, seed: int) -> None:
    """A distinct test image per seed; the same seed gives the same picture."""
    img = Image.new("RGB", (256, 256), "white")
    d = ImageDraw.Draw(img)
    for i in range(6):
        x = (seed * 37 + i * 41) % 200
        y = (seed * 53 + i * 29) % 200
        d.rectangle([x, y, x + 50, y + 30], fill=((seed * 70) % 255, (i * 40) % 255, 120))
    img.save(path, "JPEG")


# ======
# CONFIG
# ======


def test_config_has_all_domains_and_manual_total(cfg):
    assert len(cfg.domains) == 12
    assert sum(d.quota for d in cfg.domains) == 6300
    assert cfg.target("food") == 1134  # 756 x 1.5
    assert all(d.queries for d in cfg.domains)


# =======
# PARSING
# =======


def test_commons_parser_reads_metadata_in_rank_order():
    cands = parse_response(FIXTURE, "market Malawi", "work-trade")
    assert [c.source_id for c in cands] == ["1001", "1002", "1003", "1004"]

    cooking = cands[1]
    assert cooking.title == "Woman cooking nsima, Dedza"
    assert cooking.description == "A woman stirring nsima over a fire & children nearby."
    assert cooking.creator == "Example Photographer"
    assert cooking.licence == "CC BY-SA 4.0"
    assert cooking.lat == pytest.approx(-14.37) and cooking.lon == pytest.approx(34.33)
    assert cooking.tags == ["Cooking in Malawi", "Dedza District", "Nsima"]
    assert cooking.thumb_url.endswith("512px-Woman_cooking.jpg")
    assert cooking.query_domain == "work-trade"


def test_commons_search_stops_when_no_continuation():
    class FakeHttp:
        calls = 0

        def get_json(self, source, url, params):
            FakeHttp.calls += 1
            data = json.loads(json.dumps(FIXTURE))
            if FakeHttp.calls > 1:
                data.pop("continue")
                for p in data["query"]["pages"]:
                    p["pageid"] += 100
            return data

    cands = CommonsSource(FakeHttp()).search("market Malawi", "work-trade", limit=100)
    assert FakeHttp.calls == 2
    assert len(cands) == 8


def test_candidate_id_is_stable():
    a = make_candidate(source_id="42", query="x")
    b = make_candidate(source_id="42", query="y", query_domain="health")
    assert a.candidate_id == b.candidate_id
    assert a.candidate_id != make_candidate(source_id="43").candidate_id


# =======
# FILTERS
# =======


@pytest.mark.parametrize(
    "licence, url, allow_nc, expected",
    [
        ("CC BY-SA 4.0", "", False, True),
        ("Public domain", "", False, True),
        ("CC0", "", False, True),
        ("CC BY-NC 2.0", "", False, False),
        ("CC BY-NC 2.0", "", True, True),
        ("", "https://creativecommons.org/licenses/by-nc-sa/2.0/", False, False),
        ("All rights reserved", "", False, False),
        ("", "", False, False),
    ],
)
def test_licence_rules(licence, url, allow_nc, expected):
    assert filters.licence_allowed(licence, url, allow_nc)[0] is expected


def test_malawi_signal_ignores_the_query(cfg):
    assert filters.malawi_signal(make_candidate(lat=-13.9, lon=33.8, tags=[]), cfg) == "geo"
    assert filters.malawi_signal(make_candidate(tags=["Markets in Lilongwe"]), cfg) == "category"
    assert filters.malawi_signal(make_candidate(tags=[], title="Boats at Nkhata Bay"), cfg) == "text"
    no_signal = make_candidate(tags=["Streets in Zambia"], query="street Malawi")
    assert filters.malawi_signal(no_signal, cfg) == "none"


def test_metadata_filters_on_fixture(cfg):
    cands = parse_response(FIXTURE, "market Malawi", "work-trade")
    filters.apply_metadata_filters(cands, cfg)
    status = {c.source_id: (c.status, c.drop_reason) for c in cands}
    assert status["1001"] == ("kept", "")
    assert status["1002"] == ("kept", "")
    assert status["1003"] == ("dropped", "unwanted_keyword")       # a map
    assert status["1004"] == ("dropped", "licence_non_commercial")
    assert cands[1].malawi_signal == "geo"


def test_unwanted_keywords_match_whole_words_only(cfg):
    c = make_candidate(title="Mapping workshop participants")  # "map" inside a word
    filters.apply_metadata_filters([c], cfg)
    assert c.status == "kept"


def test_exact_duplicates_across_queries_keep_first():
    first = make_candidate(source_id="7", query_domain="food")
    again = make_candidate(source_id="7", query_domain="work-trade")
    same_url = make_candidate(source_id="8", image_url=first.image_url)
    filters.drop_exact_duplicates([first, again, same_url])
    assert first.status == "kept"
    assert again.drop_reason.startswith("duplicate_of:")
    assert same_url.drop_reason.startswith("duplicate_of:")


# ======
# CHECKS
# ======


def test_near_duplicates_keep_the_largest(tmp_path):
    small = make_candidate(source_id="1", width=900, height=700, thumb_url="u1")
    large = make_candidate(source_id="2", width=3000, height=2000, thumb_url="u2",
                           image_url="https://example.org/2.jpg")
    other = make_candidate(source_id="3", thumb_url="u3", image_url="https://example.org/3.jpg")
    seeds = {"u1": 5, "u2": 5, "u3": 9}

    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        draw_image(dest, seeds[url])
        return dest

    cands = [small, large, other]
    checks.fetch_thumbnails(cands, tmp_path / "thumbs", fake_download)
    assert all(c.phash and c.thumb_sha256 for c in cands)
    checks.drop_near_duplicates(cands, max_distance=6)
    assert large.status == "kept" and other.status == "kept"
    assert small.drop_reason == f"near_duplicate_of:{large.candidate_id}"


def test_broken_thumbnail_is_dropped(tmp_path):
    c = make_candidate(thumb_url="bad")

    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not an image")
        return dest

    checks.fetch_thumbnails([c], tmp_path / "thumbs", fake_download)
    assert c.drop_reason == "thumbnail_failed"


# ==========
# END TO END
# ==========


def test_run_harvest_writes_csv_and_log(cfg):
    class FixtureSource:
        name = "commons"

        def search(self, query, domain_key, limit):
            return parse_response(FIXTURE, query, domain_key)

    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        draw_image(dest, len(url))
        return dest

    result = run_harvest(cfg, [FixtureSource()], ["food"], "test-run", 10, fake_download)

    rows = list(csv.DictReader(open(result.run_dir / "candidates.csv", encoding="utf-8")))
    kept = [r for r in rows if r["status"] == "kept"]
    # 7 food queries return the same 4 images: 2 survive filters, the repeats are duplicates
    assert len(rows) == 4 * len(cfg.domain("food").queries)
    assert {r["source_id"] for r in kept} == {"1001", "1002"}
    assert all(Path(r["thumb_path"]).exists() for r in kept)

    log = json.loads((result.run_dir / "run-log.json").read_text())
    assert log["summary"][0]["kept"] == 2
    assert log["summary"][0]["target"] == 1134

def test_open_government_licence_is_allowed():
    url = "http://NationalArchives.gov.uk/doc/open-government-licence/version/1/"
    assert filters.licence_allowed("OGL v1.0", url, False)[0] is True
    assert filters.licence_allowed("Google image", "", False)[0] is False


def test_old_photos_dropped_undated_kept(cfg):
    old = make_candidate(source_id="1", date_taken="1958-03-01")
    new = make_candidate(source_id="2", date_taken="2019-06-02 10:15:00",
                         image_url="https://example.org/2.jpg")
    undated = make_candidate(source_id="3", date_taken="",
                             image_url="https://example.org/3.jpg")
    filters.apply_metadata_filters([old, new, undated], cfg)
    assert old.drop_reason == "too_old"
    assert new.status == "kept" and undated.status == "kept"


def test_location_any_keeps_photos_without_malawi_signal(cfg):
    tech = make_candidate(source_id="1", query_domain="technology", tags=["Phones in Kenya"])
    food = make_candidate(source_id="2", query_domain="food", tags=["Food in Kenya"],
                          image_url="https://example.org/2.jpg")
    filters.apply_metadata_filters([tech, food], cfg)
    assert tech.status == "kept" and tech.malawi_signal == "none"
    assert food.drop_reason == "no_malawi_signal"

# ==============
# DIVERSITY CAPS
# ==============


@pytest.mark.parametrize("cap", [0, -1])
def test_diversity_caps_disabled(cap):
    candidates = [make_candidate(creator="Same", date_taken="2025-01-01") for _ in range(4)]
    filters.cap_per_creator_day(candidates, cap)
    filters.cap_per_creator_domain(candidates, cap)
    assert all(c.status == "kept" for c in candidates)


def test_creator_day_cap_keeps_rank_and_skips_dropped_or_missing_metadata():
    candidates = [make_candidate(creator="Alice", date_taken="2025-01-01T12:00:00")
                  for _ in range(4)]
    candidates[0].drop("too_small")
    candidates[2].creator = "ALICE"
    extra = [make_candidate(creator="", date_taken="2025-01-01") for _ in range(3)]
    extra += [make_candidate(creator="Alice", date_taken="2025") for _ in range(3)]
    other_day = make_candidate(creator="Alice", date_taken="2025-01-02")
    other_source = make_candidate(source="openverse", creator="Alice", date_taken="2025-01-01")
    filters.cap_per_creator_day(candidates + extra + [other_day, other_source], 2)
    assert [c.drop_reason for c in candidates] == ["too_small", "", "", "event_cap"]
    assert all(c.status == "kept" for c in extra + [other_day, other_source])


def test_creator_domain_cap_uses_source_and_case_insensitive_creator():
    candidates = [make_candidate(creator="Alice"), make_candidate(creator="ALICE"),
                  make_candidate(creator="Alice", query_domain="transport"),
                  make_candidate(creator="Alice", source="openverse"),
                  make_candidate(creator=""), make_candidate(creator="")]
    filters.cap_per_creator_domain(candidates, 1)
    assert candidates[1].drop_reason == "creator_cap"
    assert all(c.status == "kept" for i, c in enumerate(candidates) if i != 1)


def test_caps_run_after_duplicates_and_before_downloads(cfg):
    class Source:
        name = "commons"

        def search(self, query, domain_key, limit):
            return [make_candidate(source_id=str(i), image_url=f"https://example.org/{i}.jpg",
                                   thumb_url=f"thumb-{i}", creator="Alice",
                                   date_taken="2025-01-01" if i < 4 else "2025-01-02")
                    for i in range(1, 5)]

    downloaded = []

    def download(url, dest):
        downloaded.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        draw_image(dest, len(downloaded) * 7)
        return dest

    cfg = replace(cfg, max_per_creator_day=2, max_per_creator_domain=2)
    result = run_harvest(cfg, [Source()], ["food"], "caps", 10, download)
    assert downloaded == ["thumb-1", "thumb-2"]
    assert result.candidates[2].drop_reason == "event_cap"
    assert result.candidates[3].drop_reason == "creator_cap"
    assert sum(c.drop_reason.startswith("duplicate_of:") for c in result.candidates) > 0


def test_cap_config_defaults_and_explicit_values(tmp_path):
    import yaml

    raw = yaml.safe_load((REPO_ROOT / "config" / "prompt-generation.yaml").read_text())
    raw.update(max_per_creator_day=3, max_per_creator_domain=20)
    path = tmp_path / "harvest-config.yaml"
    path.write_text(yaml.safe_dump(raw))
    explicit = load_config(path)
    assert (explicit.max_per_creator_day, explicit.max_per_creator_domain) == (3, 20)
    raw.pop("max_per_creator_day")
    raw.pop("max_per_creator_domain")
    path = tmp_path / "harvest-config.yaml"
    path.write_text(yaml.safe_dump(raw))
    cfg = load_config(path)
    assert (cfg.max_per_creator_day, cfg.max_per_creator_domain) == (0, 0)
