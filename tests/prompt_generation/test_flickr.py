"""Flickr modes, pagination, date partitions and unassigned harvest rows, without network."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.prompt_generation import filters
from src.prompt_generation.config import FlickrConfig, load_config
from src.prompt_generation.harvest import format_summary, run_harvest
from src.prompt_generation.sources import build_source
from src.prompt_generation.sources.flickr import EXTRAS, FlickrSource, parse_response


@pytest.fixture
def cfg(tmp_path):
    cfg = load_config(Path(__file__).parents[2] / "config" / "prompt-generation.yaml")
    return replace(cfg, output_root=tmp_path / "harvest", cache_dir=tmp_path / "cache",
                   flickr=FlickrConfig())


def photo(identifier, uploaded=0, **kwargs):
    return {"id": str(identifier), "owner": "123@N01", "ownername": "Example Photographer",
            "title": "Cooking nsima", "description": {"_content": "<p>Lunch &amp; family</p>"},
            "license": "4", "tags": "malawi cooking", "datetaken": "2024-01-02 12:00:00",
            "latitude": "-13.96", "longitude": "33.77", "dateupload": str(uploaded),
            "url_l": f"https://example.org/{identifier}_b.jpg", "width_l": "1600", "height_l": "1200",
            "url_z": f"https://example.org/{identifier}_z.jpg", **kwargs}


class FakeHttp:
    def __init__(self, photos):
        self.photos = photos
        self.calls = []

    def get_json(self, source, url, params):
        self.calls.append(params.copy())
        assert source == "flickr" and url == "https://www.flickr.com/services/rest/"
        if params["method"] == "flickr.photos.licenses.getInfo":
            return {"stat": "ok", "licenses": {"license": [
                {"id": "0", "name": "All Rights Reserved", "url": ""},
                {"id": "4", "name": "Attribution License", "url": "https://creativecommons.org/licenses/by/2.0/"},
                {"id": "1", "name": "Attribution-NonCommercial-ShareAlike License",
                 "url": "https://creativecommons.org/licenses/by-nc-sa/2.0/"},
            ]}}
        rows = [p for p in self.photos
                if params.get("min_upload_date", 0) <= int(p["dateupload"]) <= params.get("max_upload_date", 9999999999)]
        rows.sort(key=lambda p: int(p["dateupload"]), reverse=True)
        start = (params["page"] - 1) * params["per_page"]
        return {"stat": "ok", "photos": {"page": params["page"], "total": str(len(rows)),
                "photo": copy.deepcopy(rows[start:start + params["per_page"]])}}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("FLICKR_API_KEY", "test-key")


# ======
# CONFIG
# ======


def test_flickr_modes_default_and_validation(cfg):
    assert cfg.flickr.modes == ["geo-sweep", "text"]
    assert FlickrConfig().modes == ["geo-sweep", "text"]
    with pytest.raises(ValueError):
        FlickrConfig(modes=["invalid"])
    with pytest.raises(ValueError):
        FlickrConfig(modes=["text", "text"])


def test_missing_api_key_fails_clearly(cfg, monkeypatch):
    monkeypatch.delenv("FLICKR_API_KEY")
    with pytest.raises(RuntimeError, match="FLICKR_API_KEY"):
        build_source("flickr", FakeHttp([]), cfg)


# =========
# GEO SWEEP
# =========


def test_sweep_pages_until_total_reached(cfg):
    http = FakeHttp([photo(i) for i in range(501)])
    candidates = FlickrSource(http, cfg).geo_sweep()
    calls = [p for p in http.calls if p["method"] == "flickr.photos.search"]
    assert len(candidates) == 501 and len({c.source_id for c in candidates}) == 501
    assert [p["page"] for p in calls] == [1, 2, 3]
    for params in calls:
        assert params["bbox"] == "32.6,-17.2,35.95,-9.3"
        assert params["has_geo"] == 1 and "text" not in params
        assert params["license"] == "4" and params["media"] == "photos"
        assert params["content_type"] == 1 and params["sort"] == "date-posted-desc"
        assert params["per_page"] == 250 and params["extras"] == EXTRAS
    assert all(c.query_domain == "unassigned" and c.query == "" for c in candidates)
    assert candidates[0].creator == "Example Photographer"
    assert candidates[0].description == "Lunch & family"
    assert candidates[0].mime_type == "image/jpeg" and candidates[0].width == 1600


def test_sweep_splits_upload_ranges_above_4000(cfg, monkeypatch):
    monkeypatch.setattr("src.prompt_generation.sources.flickr._utc_midnight", lambda: 3)
    photos = [photo(f"{second}-{i}", second) for second, count in enumerate([1000, 1001, 1000, 1500])
              for i in range(count)]
    http = FakeHttp(photos)
    candidates = FlickrSource(http, cfg).geo_sweep()
    assert len(candidates) == 4501 and len({c.source_id for c in candidates}) == 4501
    calls = [p for p in http.calls if p["method"] == "flickr.photos.search"]
    assert {(p["min_upload_date"], p["max_upload_date"]) for p in calls} == {(0, 3), (2, 3), (0, 1)}
    assert sum(p["min_upload_date"] == 0 and p["max_upload_date"] == 3 for p in calls) == 1
    assert candidates[0].source_id.startswith("3-") and candidates[-1].source_id.startswith("0-")


def test_unsplittable_upload_second_raises_instead_of_truncating(cfg, monkeypatch):
    monkeypatch.setattr("src.prompt_generation.sources.flickr.time.time", lambda: 0)
    source = FlickrSource(FakeHttp([photo(i) for i in range(4001)]), cfg)
    with pytest.raises(RuntimeError, match="one upload second"):
        source.geo_sweep()


# =========
# TEXT MODE
# =========


def test_text_mode_keeps_query_and_has_no_bbox(cfg):
    http = FakeHttp([photo(1, latitude="0", longitude="0", tags="cooking", title="Market in Malawi")])
    candidates = FlickrSource(http, cfg).search('nsima "family meal"', "food", 1)
    params = http.calls[-1]
    assert params["text"] == 'nsima "family meal"'
    assert not ({"bbox", "has_geo", "min_upload_date", "max_upload_date"} & params.keys())
    assert params["per_page"] == 250 and params["extras"] == EXTRAS
    assert candidates[0].query_domain == "food"
    assert filters.malawi_signal(candidates[0], cfg) == "text"


def test_modes_can_be_enabled_separately_and_nc_config_is_respected(cfg):
    http = FakeHttp([photo(1)])
    text = FlickrSource(http, replace(cfg, flickr=FlickrConfig(modes=["text"]), allow_non_commercial=True))
    assert text.geo_sweep() == [] and http.calls == []
    text.search("food Malawi", "food", 1)
    assert http.calls[-1]["license"] == "4,1"
    http = FakeHttp([])
    sweep = FlickrSource(http, replace(cfg, flickr=FlickrConfig(modes=["geo-sweep"])))
    assert not sweep.text_enabled and sweep.search("q", "food", 10) == [] and http.calls == []


# ===================
# HARVEST INTEGRATION
# ===================


def test_sweep_runs_once_and_unassigned_rows_have_no_target(cfg, monkeypatch):
    http = FakeHttp([photo(1)])
    source = FlickrSource(http, cfg)
    monkeypatch.setattr("src.prompt_generation.harvest.checks.fetch_thumbnails", lambda *args: None)
    monkeypatch.setattr("src.prompt_generation.harvest.checks.drop_near_duplicates", lambda *args: None)
    result = run_harvest(cfg, [source], ["food", "transport"], "sweep", 1, lambda *args: None)
    sweep_calls = [p for p in http.calls if "bbox" in p]
    text_calls = [p for p in http.calls if "text" in p]
    assert len(sweep_calls) == 1
    assert len(text_calls) == len(cfg.domain("food").queries) + len(cfg.domain("transport").queries)
    unassigned = result.candidates[0]
    assert unassigned.query_domain == "unassigned" and unassigned.malawi_signal == "geo"
    assert unassigned.status == "kept"
    rows = result.summary()
    assert any(r["domain"] == "unassigned" and r["kept"] == 1 for r in rows)
    log = json.loads((result.run_dir / "run-log.json").read_text())
    assert next(r for r in log["summary"] if r["domain"] == "unassigned")["target"] is None
    summary = format_summary(result, cfg)
    assert "unassigned" in summary and "-" in next(s for s in summary.splitlines() if s.startswith("unassigned"))


def test_unassigned_still_requires_geo_inside_malawi(cfg):
    from src.prompt_generation.models import Candidate

    rows = [Candidate(source="flickr", source_id=str(i), query="", query_domain="unassigned",
                      licence="CC BY 2.0", tags=["Malawi"], lat=lat, lon=lon)
            for i, (lat, lon) in enumerate([(-13.9, 33.7), (None, None), (0, 0)])]
    filters.apply_metadata_filters(rows, cfg)
    assert rows[0].status == "kept" and rows[0].malawi_signal == "geo"
    assert all(c.drop_reason == "no_malawi_signal" for c in rows[1:])


# =======
# PARSING
# =======


def test_parser_uses_ownername_and_clears_zero_coordinates():
    data = {"photos": {"photo": [photo(1, ownername="Jane Banda", latitude="0", longitude="0")]}}
    candidate = parse_response(data, "Malawi", "food", {})[0]
    assert candidate.creator == "Jane Banda"
    assert candidate.lat is None and candidate.lon is None
    assert candidate.to_row()["lat"] == candidate.to_row()["lon"] == ""


@pytest.mark.parametrize("value", [0, "0", 0.0, "0.0", None, ""])
def test_parser_normalizes_empty_and_zero_coordinates(value):
    data = {"photos": {"photo": [photo(1, latitude=value, longitude=value)]}}
    candidate = parse_response(data, "q", "food", {})[0]
    assert candidate.lat is None and candidate.lon is None


def test_parser_handles_missing_coordinates_and_ownername():
    item = photo(1)
    for key in ("latitude", "longitude", "ownername"):
        item.pop(key)
    candidate = parse_response({"photos": {"photo": [item]}}, "q", "food", {})[0]
    assert candidate.lat is None and candidate.lon is None
    assert candidate.creator == "123@N01"


def test_parser_keeps_real_coordinates_and_falls_back_from_empty_ownername():
    candidate = parse_response({"photos": {"photo": [photo(1, ownername="")]}}, "q", "food", {})[0]
    assert candidate.creator == "123@N01"
    assert candidate.lat == pytest.approx(-13.96) and candidate.lon == pytest.approx(33.77)


# ================
# ESTIMATED TOTALS
# ================


@pytest.mark.parametrize("count", [150, 250])
def test_estimated_total_stops_on_short_or_empty_page(cfg, caplog, count):
    class EstimatedHttp(FakeHttp):
        def get_json(self, source, url, params):
            data = super().get_json(source, url, params)
            if "photos" in data:
                data["photos"]["total"] = "600"
            return data

    http = EstimatedHttp([photo(i) for i in range(count)])
    with caplog.at_level("INFO"):
        result = FlickrSource(http, cfg).geo_sweep()
    assert len(result) == count
    search_calls = [p for p in http.calls if p["method"] == "flickr.photos.search"]
    assert len(search_calls) == (1 if count < 250 else 2)
    messages = [r.message for r in caplog.records if r.message.startswith("Flickr sweep:")]
    assert messages == [f"Flickr sweep: fetched {count} photos (Flickr estimated 600)"]


def test_sweep_cache_dates_are_stable_within_utc_day(cfg, monkeypatch):
    from datetime import datetime, timezone

    midnight = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp())
    http = FakeHttp([])
    source = FlickrSource(http, cfg)
    for offset in (1, 86399):
        monkeypatch.setattr("src.prompt_generation.sources.flickr.time.time", lambda: midnight + offset)
        source.geo_sweep()
    searches = [p for p in http.calls if p["method"] == "flickr.photos.search"]
    assert len(searches) == 2
    assert searches[0] == searches[1]
    assert searches[0]["max_upload_date"] == midnight


def test_sweep_error_is_logged_and_other_sources_continue(cfg, monkeypatch, caplog):
    class BrokenSource:
        name = "broken"
        text_enabled = False

        def geo_sweep(self):
            raise RuntimeError("sweep unavailable")

    healthy = FlickrSource(FakeHttp([photo(1)]), replace(cfg, flickr=FlickrConfig(modes=["geo-sweep"])))
    monkeypatch.setattr("src.prompt_generation.harvest.checks.fetch_thumbnails", lambda *args: None)
    monkeypatch.setattr("src.prompt_generation.harvest.checks.drop_near_duplicates", lambda *args: None)
    with caplog.at_level("ERROR"):
        result = run_harvest(cfg, [BrokenSource(), healthy], ["food"], "recover", 1, lambda *args: None)
    assert len(result.candidates) == 1 and result.candidates[0].status == "kept"
    assert "broken geo-sweep failed: sweep unavailable" in caplog.text
    assert (result.run_dir / "candidates.csv").exists()


def test_split_ranges_also_stop_on_empty_pages(cfg, monkeypatch):
    monkeypatch.setattr("src.prompt_generation.sources.flickr._utc_midnight", lambda: 3)

    class EstimatedRangesHttp(FakeHttp):
        def get_json(self, source, url, params):
            data = super().get_json(source, url, params)
            if "photos" in data:
                bounds = (params["min_upload_date"], params["max_upload_date"])
                data["photos"]["total"] = "5000" if bounds == (0, 3) else "600"
            return data

    http = EstimatedRangesHttp([photo(i, uploaded=0) for i in range(250)])
    result = FlickrSource(http, cfg).geo_sweep()
    assert len(result) == 250
    searches = [p for p in http.calls if p["method"] == "flickr.photos.search"]
    assert [(p["min_upload_date"], p["max_upload_date"], p["page"]) for p in searches] == [
        (0, 3, 1), (2, 3, 1), (0, 1, 1), (0, 1, 2),
    ]
