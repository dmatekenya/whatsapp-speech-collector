"""Openverse metadata parsing and pagination, with no network calls."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.prompt_generation.config import HttpSettings, load_config
from src.prompt_generation.filters import apply_metadata_filters, malawi_signal
from src.prompt_generation.http import HttpClient
from src.prompt_generation.sources import build_source
from src.prompt_generation.sources.openverse import OpenverseSource, parse_response

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixtures" / "openverse-search.json").read_text())


@pytest.fixture
def cfg():
    return load_config(HERE.parents[1] / "config" / "prompt-generation.yaml")


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, source, url, params, **kwargs):
        self.calls.append((source, url, params.copy(), kwargs))
        return copy.deepcopy(self.pages[params["page"] - 1])


# =======
# PARSING
# =======


def test_openverse_parser_maps_licences_metadata_and_malawi_signals(cfg):
    candidates = parse_response(FIXTURE, "food Malawi", "food")
    assert [c.licence for c in candidates] == ["CC BY 2.0", "CC BY-NC 4.0", "Public domain",
                                            "CC0", "CC BY-SA 2.0"]
    assert candidates[0].source == "openverse"
    assert candidates[0].source_id == FIXTURE["results"][0]["id"]
    assert candidates[0].source_url == FIXTURE["results"][0]["foreign_landing_url"]
    assert candidates[0].image_url == FIXTURE["results"][0]["url"]
    assert candidates[0].thumb_url == FIXTURE["results"][0]["thumbnail"]
    assert candidates[0].licence_url == FIXTURE["results"][0]["license_url"]
    assert candidates[0].creator == "Example Photographer 1"
    assert candidates[0].tags == ["Malawi", "cooking", "provider:flickr"]
    assert [c.mime_type for c in candidates] == ["image/jpeg", "image/jpeg", "image/webp", "", "image/png"]
    assert candidates[3].width == candidates[3].height == 0
    assert all(c.lat is None and c.lon is None and c.date_taken == "" for c in candidates)
    assert malawi_signal(candidates[0], cfg) == "category"
    assert malawi_signal(candidates[3], cfg) == "none"
    apply_metadata_filters(candidates, cfg)
    assert candidates[1].drop_reason == "licence_non_commercial"
    assert candidates[3].drop_reason == "no_malawi_signal"
    assert candidates[2].status == "kept"


def test_openverse_optional_metadata_and_date(cfg):
    data = copy.deepcopy(FIXTURE)
    row = data["results"][0]
    row.update(tags=None, description="Lunch in Malawi", creator=None, filetype=None,
               date_taken="2024-05-06", title="Nsima in Malawi")
    candidate = parse_response(data, "q", "food")[0]
    assert candidate.date_taken == "2024-05-06" and candidate.description == "Lunch in Malawi"
    assert candidate.creator == "" and candidate.mime_type == ""
    assert malawi_signal(candidate, cfg) == "text"


# ======
# SEARCH
# ======


@pytest.mark.parametrize("token,size", [(None, 20), ("test-token", 50)])
def test_search_paginates_with_fixed_page_size_and_optional_token(cfg, monkeypatch, token, size):
    if token is None:
        monkeypatch.delenv("OPENVERSE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OPENVERSE_TOKEN", token)
    # Return complete pages, then stop on the requested total without fetching page three.
    page = {"page_count": 4, "results": FIXTURE["results"] * (size // 5)}
    http = FakeHttp([page, page])
    result = OpenverseSource(http, cfg).search("food Malawi", "food", size + 1)
    assert len(result) == size + 1 and len(http.calls) == 2
    assert [call[2]["page"] for call in http.calls] == [1, 2]
    for source, url, params, kwargs in http.calls:
        assert source == "openverse" and url == "https://api.openverse.org/v1/images/"
        assert params == {"q": "food Malawi", "page": params["page"], "page_size": size,
                          "excluded_source": "wikimedia", "license_type": "commercial"}
        assert kwargs == ({"headers": {"Authorization": "Bearer test-token"}} if token else {})


@pytest.mark.parametrize("end", [{"page_count": 1}, {"next": None}, {"page_count": 5, "results": []}])
def test_search_stops_at_last_or_empty_page(cfg, monkeypatch, end):
    monkeypatch.delenv("OPENVERSE_TOKEN", raising=False)
    http = FakeHttp([{**FIXTURE, **end}])
    result = OpenverseSource(http, cfg).search("food", "food", 100)
    assert len(http.calls) == 1
    assert len(result) == (0 if end.get("results") == [] else 5)


def test_search_allows_nc_when_configured_and_zero_limit_makes_no_calls(cfg, monkeypatch):
    monkeypatch.delenv("OPENVERSE_TOKEN", raising=False)
    http = FakeHttp([FIXTURE])
    source = build_source("openverse", http, replace(cfg, allow_non_commercial=True))
    assert source.search("food", "food", 0) == []
    assert http.calls == []
    source.search("food", "food", 3)
    assert "license_type" not in http.calls[0][2]
    assert http.calls[0][2]["page_size"] == 3


# ============
# SHARED CACHE
# ============


def test_http_json_cache_and_headers_do_not_leak_to_downloads(tmp_path, monkeypatch):
    client = HttpClient(HttpSettings(pause_seconds=0), tmp_path / "cache")
    calls = []

    class Response:
        def json(self):
            return FIXTURE

        def iter_content(self, chunk_size):
            yield b"thumbnail"

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(client, "_request", request)
    headers = {"Authorization": "Bearer test-token"}
    params = {"q": "food Malawi"}
    assert client.get_json("openverse", "https://api.openverse.org/v1/images/", params,
                           headers=headers) == FIXTURE
    assert client.get_json("openverse", "https://api.openverse.org/v1/images/", params,
                           headers=headers) == FIXTURE
    assert len(calls) == 1 and calls[0][1]["headers"] == headers
    client.download("https://example.org/thumb.jpg", tmp_path / "thumb.jpg")
    assert "headers" not in calls[-1][1] and "Authorization" not in client.session.headers
    assert "test-token" not in next((tmp_path / "cache" / "openverse").iterdir()).read_text()
