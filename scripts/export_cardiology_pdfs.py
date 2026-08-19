"""Extract cardiology reference PDFs into chunked JSONL records.

Usage (from the repository root):
    python scripts/export_cardiology_pdfs.py

The extractor tries PyMuPDF first because it is generally more tolerant of
damaged xref/object streams.  If a document or an individual page cannot be
read, it tries pypdf with ``strict=False`` before skipping that page.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Iterator

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - handled by dependency check
    fitz = None

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - handled by dependency check
    PdfReader = None

from langchain_text_splitters import RecursiveCharacterTextSplitter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "data" / "raw"
OUTPUT_FILE = PROJECT_ROOT / "data" / "processed" / "cardiology_knowledge_base.jsonl"
MIN_PAGE_CHARACTERS = 30

SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=150,
    separators=["\n\n", "\n", ". ", " ", ""],
)


def clean_medical_text(text: str) -> str:
    """Normalize extracted text without collapsing meaningful paragraphs."""
    # A soft hyphen is commonly inserted into copied PDF text; remove it before
    # joining line-end hyphenation.
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Keep printable ASCII plus newlines.  This removes NULs, control codes,
    # private-use glyphs, and other extraction artefacts.
    text = "".join(char for char in text if char == "\n" or " " <= char <= "~")
    text = re.sub(r"[^\S\n]+", " ", text)       # spaces/tabs, but not newlines
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_with_pymupdf(pdf_path: Path) -> Iterator[tuple[int, str]]:
    """Yield readable (one-based page number, text) pairs using PyMuPDF."""
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")

    document = fitz.open(pdf_path)
    try:
        if getattr(document, "is_repaired", False):
            logging.warning("%s: PyMuPDF repaired damaged PDF structure", pdf_path.name)
        for index, page in enumerate(document, start=1):
            try:
                yield index, page.get_text("text", sort=True)
            except Exception as exc:
                logging.warning("%s page %d: PyMuPDF skipped unreadable page (%s)", pdf_path.name, index, exc)
                yield index, ""
    finally:
        document.close()


def extract_with_pypdf(pdf_path: Path) -> Iterator[tuple[int, str]]:
    """Fallback reader for PDFs PyMuPDF cannot open; accepts malformed xrefs."""
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reader = PdfReader(str(pdf_path), strict=False)
    for warning in caught:
        logging.warning("%s: pypdf repair warning: %s", pdf_path.name, warning.message)

    for index, page in enumerate(reader.pages, start=1):
        try:
            yield index, page.extract_text() or ""
        except Exception as exc:
            logging.warning("%s page %d: pypdf skipped unreadable page (%s)", pdf_path.name, index, exc)
            yield index, ""


def page_texts(pdf_path: Path) -> Iterator[tuple[int, str]]:
    """Use the most fault-tolerant available backend for a PDF."""
    try:
        yield from extract_with_pymupdf(pdf_path)
        return
    except Exception as exc:
        logging.warning("%s: PyMuPDF failed (%s); trying pypdf strict=False", pdf_path.name, exc)
    yield from extract_with_pypdf(pdf_path)


def book_name(pdf_path: Path) -> str:
    """Create a readable source label while retaining the exact filename."""
    return re.sub(r"[_-]+", " ", pdf_path.stem).strip()


def process_pdf(pdf_path: Path, output_handle) -> tuple[int, int, int]:
    """Write a PDF's chunks and return (readable_pages, chunks, skipped_pages)."""
    readable_pages = chunks_written = skipped_pages = 0
    source = book_name(pdf_path)

    try:
        for page_number, raw_text in page_texts(pdf_path):
            text = clean_medical_text(raw_text)
            if len(text) < MIN_PAGE_CHARACTERS:
                skipped_pages += 1
                logging.info("%s page %d: skipped empty/corrupted TOC/index-like page", pdf_path.name, page_number)
                continue

            readable_pages += 1
            for chunk in SPLITTER.split_text(text):
                output_handle.write(json.dumps({
                    "text": chunk,
                    "metadata": {
                        "source": source,
                        "file_name": pdf_path.name,
                        "page": page_number,
                    },
                }, ensure_ascii=False) + "\n")
                chunks_written += 1
    except Exception as exc:
        logging.error("%s: could not finish extraction (%s)", pdf_path.name, exc)

    return readable_pages, chunks_written, skipped_pages


def main() -> int:
    if fitz is None and PdfReader is None:
        logging.error("Install PyMuPDF and/or pypdf before running this script.")
        return 2

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    pdf_files = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdf_files:
        logging.warning("No PDF files found in %s", INPUT_DIR)
        return 0

    by_source: dict[str, int] = defaultdict(int)
    total_pages = total_chunks = 0
    with OUTPUT_FILE.open("w", encoding="utf-8") as output_handle:
        for pdf_path in pdf_files:
            logging.info("Processing %s", pdf_path.name)
            pages, chunks, skipped = process_pdf(pdf_path, output_handle)
            total_pages += pages
            total_chunks += chunks
            by_source[book_name(pdf_path)] += chunks
            logging.info("Finished %s | extracted pages: %d | skipped pages: %d | chunks: %d", pdf_path.name, pages, skipped, chunks)

    logging.info("Export complete: %d readable pages, %d chunks -> %s", total_pages, total_chunks, OUTPUT_FILE)
    logging.info("Chunks by book source:")
    for source, count in by_source.items():
        logging.info("  %s: %d", source, count)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
