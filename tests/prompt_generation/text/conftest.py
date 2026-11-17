"""Fake poppler and tesseract, so tests never run real tools or touch the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.prompt_generation.text import news_pdf


@dataclass
class FakePdf:
    pages: int = 1
    text_layer: str = ""
    images: int = 1
    ocr_text: str = ""


@dataclass
class FakeTools:
    """Describes each fake PDF by file name; records every tool call."""

    pdfs: dict[str, FakePdf] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def add(self, name: str, **kwargs) -> FakePdf:
        self.pdfs[name] = FakePdf(**kwargs)
        return self.pdfs[name]

    def count(self, tool: str) -> int:
        return sum(1 for call in self.calls if call[0] == tool)

    def run(self, args, *, extra_env=None) -> str:
        args = list(args)
        self.calls.append(args)
        tool = args[0]
        if tool == "tesseract":
            # The fake "image" file holds the PDF name, so OCR text can be looked up.
            name = Path(args[1]).read_text(encoding="utf-8")
            return self.pdfs[name].ocr_text
        pdf_name = Path(next(a for a in args if a.endswith(".pdf"))).name
        fake = self.pdfs[pdf_name]
        if tool == "pdfinfo":
            return f"Title:  demo\nPages:          {fake.pages}\n"
        if tool == "pdftotext":
            return fake.text_layer
        if tool == "pdfimages" and args[1] == "-list":
            header = "page   num  type   width height\n-----------------------------------\n"
            rows = "".join(f"   1    {i}  image  100 100\n" for i in range(fake.images))
            return header + rows
        if tool == "pdfimages" and args[1] == "-j":
            out = Path(args[3] + "-000.jpg")
            out.write_text(pdf_name, encoding="utf-8")
            return ""
        if tool == "pdftoppm":
            target = Path(args[-1] + ".png")
            target.write_text(pdf_name, encoding="utf-8")
            return ""
        raise AssertionError(f"unexpected tool call: {args}")


@pytest.fixture
def fake_tools(monkeypatch) -> FakeTools:
    tools = FakeTools()
    monkeypatch.setattr(news_pdf, "run_tool", tools.run)
    return tools


def write_pdf(directory: Path, name: str) -> Path:
    """Create a placeholder PDF. Its bytes differ per name, so each gets its own OCR cache key."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(f"%PDF-1.4 placeholder {name}".encode())
    return path


FIXTURES = Path(__file__).parent / "fixtures"
