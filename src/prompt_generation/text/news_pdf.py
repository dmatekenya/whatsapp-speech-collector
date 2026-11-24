"""Read newspaper page PDFs: pdftotext text layer first, OCR (poppler + tesseract) as fallback."""

from __future__ import annotations

import concurrent.futures as futures
import datetime as dt
import hashlib
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

# ======
# MODELS
# ======


class ToolError(RuntimeError):
    """An external command (poppler or tesseract) was missing or failed."""


@dataclass(frozen=True)
class PdfSettings:
    min_text_layer_words: int = 100
    ocr_lang: str = "eng"
    ocr_psm: int = 3
    ocr_fallback_dpi: int = 300
    page_skip_keywords: tuple[str, ...] = ()
    ocr_cache_dir: Path = Path("data/prompts/text/ocr-cache")


@dataclass
class PdfPage:
    source_file: str
    page: int | None
    method: str  # "text-layer" | "ocr"
    text: str
    doc_date: dt.date | None
    date_source: str
    ocr_cache_hit: bool = False


@dataclass
class PdfReadResult:
    pages: list[PdfPage] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    non_pdf_files: int = 0


# =====
# TOOLS
# =====


def run_tool(args: Sequence[str], *, extra_env: dict[str, str] | None = None) -> str:
    """Run an external tool with a list of arguments (never through a shell)."""
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        proc = subprocess.run(list(args), capture_output=True, check=True, env=env)
    except FileNotFoundError as exc:
        raise ToolError(f"{args[0]} not found; install poppler and tesseract") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise ToolError(f"{args[0]} failed: {detail}") from exc
    return proc.stdout.decode("utf-8", errors="replace")


def page_count(pdf: Path) -> int:
    info = run_tool(["pdfinfo", str(pdf)])
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    if not match:
        raise ToolError("pdfinfo reported no page count")
    return int(match.group(1))


def pdftotext(pdf: Path) -> str:
    return run_tool(["pdftotext", str(pdf), "-"])


def image_count(pdf: Path) -> int:
    """Count embedded images from `pdfimages -list` (two header lines, then one row per image)."""
    lines = run_tool(["pdfimages", "-list", str(pdf)]).splitlines()
    return len([line for line in lines[2:] if line.strip()])


def extract_page_image(pdf: Path, workdir: Path, dpi: int) -> Path:
    """Return the page image: the single embedded image if there is one, otherwise a rendered page."""
    if image_count(pdf) == 1:
        run_tool(["pdfimages", "-j", str(pdf), str(workdir / "img")])
        images = sorted(workdir.glob("img-*"))
        if len(images) == 1:
            return images[0]
    target = workdir / "page"
    run_tool(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", str(pdf), str(target)])
    return target.with_suffix(".png")


def ocr_image(image: Path, lang: str, psm: int) -> str:
    return run_tool(
        ["tesseract", str(image), "stdout", "-l", lang, "--psm", str(psm)],
        extra_env={"OMP_THREAD_LIMIT": "1"},
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ocr_pdf(pdf: Path, settings: PdfSettings) -> tuple[str, bool]:
    """OCR one PDF, using the cache keyed by the PDF's sha256. Returns (text, cache_hit)."""
    cache_file = settings.ocr_cache_dir / f"{file_sha256(pdf)}.txt"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8"), True
    with tempfile.TemporaryDirectory() as tmp:
        image = extract_page_image(pdf, Path(tmp), settings.ocr_fallback_dpi)
        text = ocr_image(image, settings.ocr_lang, settings.ocr_psm)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    partial = cache_file.with_suffix(".tmp")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, cache_file)
    return text, False


# =====
# DATES
# =====

MONTH_ALT = "(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_RANGE = re.compile(
    rf"({MONTH_ALT})\s+(\d{{1,2}})\s*[-–—]\s*(?:({MONTH_ALT})\s+)?(\d{{1,2}})\s*,?\s*(\d{{4}})",
    re.IGNORECASE,
)
FILENAME_DATE = re.compile(r"(\d{4})_(\d{2})_(\d{2})")
PAGE_NUMBER = re.compile(r"Page\s*(\d+)", re.IGNORECASE)
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}


def parse_printed_date(text: str) -> dt.date | None:
    """Return the first date of the printed range found in the first six non-empty lines."""
    lines = [line.strip() for line in text.splitlines() if line.strip()][:6]
    match = DATE_RANGE.search(" ".join(lines))
    if not match:
        return None
    start_month = MONTHS[match.group(1).lower()]
    start_day = int(match.group(2))
    end_month = MONTHS[match.group(3).lower()] if match.group(3) else start_month
    year = int(match.group(5))
    if start_month > end_month:
        year -= 1  # range crosses a year end, e.g. "December 28 - January 3, 2017"
    try:
        return dt.date(year, start_month, start_day)
    except ValueError:
        return None


def resolve_date(text: str, filename: str) -> tuple[dt.date | None, str]:
    printed = parse_printed_date(text)
    if printed:
        return printed, "printed"
    match = FILENAME_DATE.search(filename)
    if match:
        try:
            return dt.date(*map(int, match.groups())), "filename"
        except ValueError:
            pass
    return None, "none"


def page_number(filename: str) -> int | None:
    match = PAGE_NUMBER.search(filename)
    return int(match.group(1)) if match else None


def letter_word_count(text: str) -> int:
    return len(re.findall(r"[^\W\d_]+", text))


# ==========
# PAGE READS
# ==========


def read_pdf_pages(pdf_dir: Path, settings: PdfSettings, *, ocr_workers: int) -> PdfReadResult:
    """Read every PDF under pdf_dir (recursively). Text layer first; OCR runs in parallel."""
    result = PdfReadResult()
    pending_ocr: list[tuple[Path, str]] = []

    for path in sorted(p for p in pdf_dir.rglob("*") if p.is_file()):
        relpath = str(path.relative_to(pdf_dir))
        if path.suffix.lower() != ".pdf":
            result.non_pdf_files += 1
            continue
        try:
            pages = page_count(path)
            if pages != 1:
                result.skipped.append({"source_file": relpath, "reason": f"multi_page:{pages}"})
                continue
            text = pdftotext(path)
        except ToolError as exc:
            result.failures.append({"source_file": relpath, "error": str(exc)})
            continue
        if letter_word_count(text) >= settings.min_text_layer_words:
            _add_page(result, relpath, path.name, "text-layer", text, settings)
        else:
            pending_ocr.append((path, relpath))

    if pending_ocr:
        with futures.ThreadPoolExecutor(max_workers=max(1, ocr_workers)) as pool:
            jobs = {pool.submit(ocr_pdf, path, settings): (path, relpath) for path, relpath in pending_ocr}
            for job in futures.as_completed(jobs):
                path, relpath = jobs[job]
                try:
                    text, hit = job.result()
                except ToolError as exc:
                    result.failures.append({"source_file": relpath, "error": str(exc)})
                    continue
                _add_page(result, relpath, path.name, "ocr", text, settings, cache_hit=hit)

    result.pages.sort(key=lambda p: p.source_file)
    return result


def _add_page(
    result: PdfReadResult,
    relpath: str,
    filename: str,
    method: str,
    text: str,
    settings: PdfSettings,
    cache_hit: bool = False,
) -> None:
    lowered = text.casefold()
    for keyword in settings.page_skip_keywords:
        if keyword.casefold() in lowered:
            result.skipped.append({"source_file": relpath, "page": page_number(filename), "reason": f"keyword:{keyword}"})
            return
    doc_date, date_source = resolve_date(text, filename)
    result.pages.append(
        PdfPage(
            source_file=relpath,
            page=page_number(filename),
            method=method,
            text=text,
            doc_date=doc_date,
            date_source=date_source,
            ocr_cache_hit=cache_hit,
        )
    )
