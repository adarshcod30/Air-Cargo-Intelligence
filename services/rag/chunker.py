"""Splitting source documents into citable passages.

A citation that resolves to a 40-page PDF is technically provenance and
practically useless: nobody checks it. Chunking exists so an answer can
point at the paragraph a figure came from.

Passages are page-aware. Cargo statistics are published as tables with the
month in a header row, so a chunk that silently spans a page break loses
the period its numbers belong to - the same class of defect that made the
parser read 1,590 rows as `period=unknown`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from services.common.logging import get_logger

log = get_logger(__name__)

# Roughly four characters per token for English prose and tabular text.
_CHARS_PER_TOKEN = 4
_TARGET_TOKENS = 320
_OVERLAP_TOKENS = 48

_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


@dataclass
class Chunk:
    seq: int
    content: str
    page: int | None
    token_estimate: int


def _normalise(text: str) -> str:
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


def _split_page(text: str, target_chars: int, overlap_chars: int) -> list[str]:
    """Paragraph-first splitting, with a hard wrap for runaway tables."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= target_chars:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            out.append(buf)
            # Carry the tail forward so a figure and its header survive a
            # boundary. Without overlap a table split mid-way produces a
            # chunk of bare numbers with nothing to identify them.
            buf = buf[-overlap_chars:] + "\n\n" + p if overlap_chars else p
        else:
            buf = p
        while len(buf) > target_chars * 1.6:
            out.append(buf[:target_chars])
            buf = buf[target_chars - overlap_chars:]
    if buf.strip():
        out.append(buf)
    return out


def chunk_pdf(path: Path) -> list[Chunk]:
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber unavailable; cannot chunk PDFs")
        return []

    target = _TARGET_TOKENS * _CHARS_PER_TOKEN
    overlap = _OVERLAP_TOKENS * _CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    seq = 0
    try:
        with pdfplumber.open(path) as pdf:
            for pageno, page in enumerate(pdf.pages, start=1):
                text = _normalise(page.extract_text() or "")
                if len(text) < 40:
                    continue
                for piece in _split_page(text, target, overlap):
                    piece = piece.strip()
                    if len(piece) < 40:
                        continue
                    chunks.append(
                        Chunk(seq, piece, pageno, max(1, len(piece) // _CHARS_PER_TOKEN))
                    )
                    seq += 1
    except Exception as exc:
        log.warning(f"could not read {path.name}: {type(exc).__name__}: {exc}")
    return chunks


def chunk_json(path: Path, max_records: int = 60) -> list[Chunk]:
    """Render structured records as readable text.

    Open-data payloads are records, not prose. Flattening a record into
    `field: value` lines gives the embedder something with actual lexical
    content, and gives a reader a passage that means something when it is
    shown as a citation.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []

    records = data
    if isinstance(data, dict):
        for key in ("records", "data", "rows", "result"):
            if isinstance(data.get(key), list):
                records = data[key]
                break
        else:
            records = [data]
    if not isinstance(records, list):
        return []

    chunks: list[Chunk] = []
    for seq, rec in enumerate(records[:max_records]):
        if not isinstance(rec, dict):
            continue
        body = "\n".join(f"{k}: {v}" for k, v in rec.items() if v not in (None, ""))
        if len(body) < 30:
            continue
        chunks.append(Chunk(seq, body, None, max(1, len(body) // _CHARS_PER_TOKEN)))
    return chunks


def chunk_document(path: Path, media_type: str | None = None) -> list[Chunk]:
    if not path.exists():
        return []
    mt = (media_type or "").lower()
    if "pdf" in mt or path.suffix.lower() == ".pdf":
        return chunk_pdf(path)
    if "json" in mt or path.suffix.lower() in {".json", ".jsonl"}:
        return chunk_json(path)
    return []
