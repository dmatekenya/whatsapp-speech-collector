"""Parse a WordPress WXR export into cleaned post documents (published posts only)."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import ftfy

NAMESPACES = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

# ======
# MODELS
# ======


@dataclass
class WxrPost:
    post_id: str
    title: str
    post_date: date | None
    categories: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)


# =====
# CLEAN
# =====

_CAPTION = re.compile(r"\[caption[^\]]*\].*?\[/caption\]", re.IGNORECASE | re.DOTALL)
_ELEMENTS = re.compile(r"<(figure|figcaption|table)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_PARAGRAPH_BREAK = re.compile(r"</?(?:p|div|h[1-6]|li|blockquote|br)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def clean_body(raw_html: str) -> list[str]:
    """Return the body as paragraphs of plain text."""
    text = ftfy.fix_text(raw_html or "")
    text = _CAPTION.sub(" ", text)
    text = _ELEMENTS.sub(" ", text)
    text = _COMMENT.sub(" ", text)
    text = _PARAGRAPH_BREAK.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = text.replace("&nbsp;", " ")
    text = html.unescape(text).replace("\xa0", " ")
    paragraphs = []
    for chunk in re.split(r"\n\s*\n", text):
        paragraph = " ".join(chunk.split())
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _parse_post_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").date()
    except ValueError:
        return None


def parse_wxr(path: Path) -> tuple[list[WxrPost], dict[str, int]]:
    """Return published posts and counts of items that were skipped, by reason."""
    root = ET.parse(path).getroot()
    posts: list[WxrPost] = []
    skipped: dict[str, int] = {"not_post": 0, "not_published": 0}
    for item in root.iter("item"):
        if item.findtext("wp:post_type", default="", namespaces=NAMESPACES) != "post":
            skipped["not_post"] += 1
            continue
        if item.findtext("wp:status", default="", namespaces=NAMESPACES) != "publish":
            skipped["not_published"] += 1
            continue
        categories = [
            (cat.text or "").strip()
            for cat in item.findall("category")
            if cat.get("domain") == "category" and cat.text
        ]
        posts.append(
            WxrPost(
                post_id=item.findtext("wp:post_id", default="", namespaces=NAMESPACES).strip(),
                title=ftfy.fix_text(item.findtext("title") or "").strip(),
                post_date=_parse_post_date(
                    item.findtext("wp:post_date", default="", namespaces=NAMESPACES)
                ),
                categories=categories,
                paragraphs=clean_body(item.findtext("content:encoded", default="", namespaces=NAMESPACES)),
            )
        )
    return posts, skipped
