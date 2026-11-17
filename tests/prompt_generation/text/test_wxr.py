"""WXR parsing: published posts only, body cleaning and garbled-text repair."""

from __future__ import annotations

from src.prompt_generation.text.wxr import clean_body, parse_wxr

from .conftest import FIXTURES

WXR = FIXTURES / "wxr-sample.xml"


def test_only_published_posts_are_kept():
    posts, skipped = parse_wxr(WXR)

    assert [p.post_id for p in posts] == ["101", "102"]
    assert skipped == {"not_post": 1, "not_published": 1}


def test_post_metadata_and_categories():
    posts, _ = parse_wxr(WXR)
    first = posts[0]

    assert first.title == "Alimi alandira feteleza"
    assert first.post_date.isoformat() == "2023-01-05"
    assert first.categories == ["Nkhani"]


def test_body_drops_captions_figures_tables_and_comments():
    posts, _ = parse_wxr(WXR)
    text = " ".join(posts[0].paragraphs)

    assert "Mtsogoleri" not in text  # [caption] block
    assert "Chithunzi" not in text  # <figcaption>
    assert "Mtengo" not in text  # <table>
    assert "wp:paragraph" not in text  # HTML comment
    assert "<" not in text


def test_body_unescapes_entities_and_nbsp():
    posts, _ = parse_wxr(WXR)

    assert posts[0].paragraphs == [
        "Alimi a m’dera la Dedza alandira feteleza.",
        "Boma lidzawonjezera thandizo kwa alimi ang‘onoang‘ono.",
        "Ntchito yidzayamba mwezi wa February.",
    ]


def test_garbled_text_is_repaired_by_ftfy():
    posts, _ = parse_wxr(WXR)
    body = posts[1].title + " " + " ".join(posts[1].paragraphs)

    assert "mâ€™ana" not in body and "Ã" not in body
    # ftfy's default uncurls the repaired apostrophe; Stage 2 normalises quotes anyway.
    assert posts[1].title == "Mphunzitsi wa m'ana"
    assert posts[1].paragraphs[0].startswith("Mphunzitsi wa m'ana")


def test_clean_body_splits_paragraphs_on_blank_lines():
    assert clean_body("<p>Choyamba.</p>\n\n<p>Chachiwiri\nmzere.</p>") == [
        "Choyamba.",
        "Chachiwiri mzere.",
    ]
